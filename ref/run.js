#!/usr/bin/env node
/*
 * Reference oracle: runs a .sb3 in the real scratch-vm, deterministically,
 * and prints a JSON-lines trace that the native binaries must reproduce.
 *
 * Determinism:
 *   - Date.now is replaced by a virtual clock that advances exactly 1/30 s
 *     per frame and is frozen within a frame.
 *   - The sequencer's work timer is replaced by a tick counter, so each frame
 *     runs at most TICKS ticks (instead of "as many as fit in 25 ms").
 *   - setTimeout is virtualized (used by "say for secs" and friends).
 *
 * Fidelity:
 *   - A stub renderer is attached. Without a renderer scratch-vm never
 *     requests redraws, never fences sprites onto the stage, and silently
 *     ignores "set size". The stub reproduces those behaviours using the
 *     rotated costume rectangle as the sprite's bounds, which is the same
 *     model the native runtime uses.
 *
 * Usage: node run.js project.sb3 [--frames N] [--ticks N] [--answers a,b,c]
 */
'use strict';
process.env.TZ = 'UTC';
// scratch-vm logs warnings to stdout; the trace is written with fs.writeSync
// at the end, so route everything else to stderr.
process.stdout.write = (chunk, enc, cb) => process.stderr.write(chunk, enc, cb);

const fs = require('fs');
const path = require('path');

// ---------------------------------------------------------------------------
// Virtual time (must be installed before scratch-vm is loaded)

const FRAME_MS = 1000 / 30;
let virtualNow = 1700000000000;
const realDateNow = Date.now;
Date.now = () => virtualNow;
const RealDate = Date;
global.Date = class extends RealDate {
    constructor (...args) {
        if (args.length === 0) super(virtualNow); else super(...args);
    }
    static now () { return virtualNow; }
};

const timers = []; // {at, fn, id}
let timerSeq = 1;
const realSetTimeout = global.setTimeout;
const realSetInterval = global.setInterval;
global.setTimeout = (fn, ms, ...a) => {
    const id = timerSeq++;
    timers.push({at: virtualNow + Math.max(0, Number(ms) || 0), fn: () => fn(...a), id});
    return id;
};
global.clearTimeout = id => {
    const i = timers.findIndex(t => t.id === id);
    if (i >= 0) timers.splice(i, 1);
};
// scratch-vm's own stepping interval is never started; ignore other intervals.
global.setInterval = () => 0;
global.clearInterval = () => {};

// ---------------------------------------------------------------------------

const args = process.argv.slice(2);
const file = args[0];
const opt = (name, def) => {
    const i = args.indexOf(name);
    return i >= 0 ? args[i + 1] : def;
};
const MAX_FRAMES = Number(opt('--frames', 30 * 60 * 5));
const TICKS = Number(opt('--ticks', 64));
const answers = (opt('--answers', '') || '').split(',').filter(s => s.length > 0);

// Deterministic randomness: a seeded mulberry32 that the native runtime
// reproduces exactly. Only Scratch blocks may consume it, so scratch-vm's
// internal uid() generator (called on every say and clone) is replaced.
const SEED = Number(opt('--seed', 1)) >>> 0;
let rngState = SEED;
Math.random = function () {
    rngState = (rngState + 0x6D2B79F5) | 0;
    let t = Math.imul(rngState ^ (rngState >>> 15), 1 | rngState);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
};
const vmSrc = path.join(__dirname, 'node_modules/scratch-vm/src');
const uidPath = require.resolve(path.join(vmSrc, 'util/uid.js'));
let uidN = 0;
require.cache[uidPath] = {id: uidPath, filename: uidPath, loaded: true, exports: () => `uid${uidN++}`};
// Minimal browser shims so scratch-vm can load bitmap (PNG/JPEG) costumes in
// Node. Only dimensions matter to the stub renderer, so images are never decoded.
const imageSize = bytes => {
    const b = Buffer.from(bytes);
    if (b.length > 24 && b[0] === 0x89 && b[1] === 0x50) return {width: b.readUInt32BE(16), height: b.readUInt32BE(20)};
    if (b[0] === 0xFF && b[1] === 0xD8) {
        let i = 2;
        while (i + 9 < b.length) {
            if (b[i] !== 0xFF) { i++; continue; }
            const m = b[i + 1];
            if (m >= 0xC0 && m <= 0xC2) return {width: b.readUInt16BE(i + 7), height: b.readUInt16BE(i + 5)};
            i += 2 + b.readUInt16BE(i + 2);
        }
    }
    return {width: 0, height: 0};
};
global.createImageBitmap = async blob => imageSize(new Uint8Array(await blob.arrayBuffer()));
const fakeCanvas = () => ({width: 0, height: 0, getContext: () => ({drawImage () {}}), toDataURL: () => 'data:,'});
global.document = global.document || {createElement: () => fakeCanvas(), hidden: false};

const VM = require(path.join(vmSrc, 'index.js'));
let ScratchStorage = require('scratch-storage');
if (ScratchStorage.ScratchStorage) ScratchStorage = ScratchStorage.ScratchStorage;
if (ScratchStorage.default) ScratchStorage = ScratchStorage.default;

const out = [];
let frame = 0;
const emit = obj => {
    out.push(JSON.stringify(obj));
};

// Number formatting used for trace values: exactly what JS String() does.
const S = v => String(v);
const R = v => Math.round(v * 1e6) / 1e6; // coordinates, to avoid 1e-15 noise

// ---------------------------------------------------------------------------
// Stub renderer

class StubRenderer {
    constructor () {
        this.skins = [];
        this.drawables = [];
    }
    setLayerGroupOrdering () {}
    createDrawable () {
        this.drawables.push({skin: -1, pos: [0, 0], dir: 90, scale: [100, 100], visible: true});
        return this.drawables.length - 1;
    }
    destroyDrawable (id) { this.drawables[id] = null; }
    setDrawableOrder () { return 1; }
    getDrawableOrder () { return 1; }
    _svgSize (svg) {
        const m = /<svg[^>]*?\bwidth="([\d.]+)[a-z]*"[^>]*?\bheight="([\d.]+)[a-z]*"/.exec(svg) ||
                  /<svg[^>]*?\bheight="([\d.]+)[a-z]*"[^>]*?\bwidth="([\d.]+)[a-z]*"/.exec(svg);
        if (!m) return [0, 0];
        if (/<svg[^>]*?\bwidth="[\d.]+[a-z]*"[^>]*?\bheight=/.test(svg)) return [Number(m[1]), Number(m[2])];
        return [Number(m[2]), Number(m[1])];
    }
    createSVGSkin (svg, rc) {
        const [w, h] = this._svgSize(svg);
        this.skins.push({w, h, rc: rc ? rc.slice() : [w / 2, h / 2]});
        return this.skins.length - 1;
    }
    updateSVGSkin (id, svg, rc) {
        const [w, h] = this._svgSize(svg);
        this.skins[id] = {w, h, rc: rc ? rc.slice() : [w / 2, h / 2]};
    }
    createBitmapSkin (canvas, res, rc) {
        // scratch-vm passes the rotation centre already in logical pixels
        const r = res || 1;
        const w = (canvas && canvas.width ? canvas.width : 0) / r;
        const h = (canvas && canvas.height ? canvas.height : 0) / r;
        this.skins.push({w, h, rc: rc ? [rc[0], rc[1]] : [w / 2, h / 2]});
        return this.skins.length - 1;
    }
    updateBitmapSkin (id, canvas, res, rc) {
        this.skins[id] = this.skins[this.createBitmapSkin(canvas, res, rc)];
    }
    createTextSkin () { this.skins.push({w: 0, h: 0, rc: [0, 0]}); return this.skins.length - 1; }
    updateTextSkin () {}
    createPenSkin () { this.skins.push({w: 480, h: 360, rc: [240, 180], pen: true}); return this.skins.length - 1; }
    destroySkin () {}
    getSkinSize (id) { const s = this.skins[id]; return s ? [s.w, s.h] : [0, 0]; }
    getSkinRotationCenter (id) { const s = this.skins[id]; return s ? s.rc.slice() : [0, 0]; }
    getCurrentSkinSize (did) {
        const d = this.drawables[did];
        return d && d.skin >= 0 ? this.getSkinSize(d.skin) : [0, 0];
    }
    getNativeSize () { return [480, 360]; }
    updateDrawableSkinId (did, sid) { if (this.drawables[did]) this.drawables[did].skin = sid; }
    updateDrawablePosition (did, p) { if (this.drawables[did]) this.drawables[did].pos = [p[0], p[1]]; }
    updateDrawableDirectionScale (did, dir, scale) {
        const d = this.drawables[did];
        if (d) { d.dir = dir; d.scale = [scale[0], scale[1]]; }
    }
    updateDrawableVisible (did, v) { if (this.drawables[did]) this.drawables[did].visible = v; }
    updateDrawableEffect () {}
    getBounds (did) {
        const d = this.drawables[did];
        const s = d && this.skins[d.skin];
        if (!s) return {left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0};
        const [cx, cy] = s.rc;
        const corners = [[-cx, cy], [s.w - cx, cy], [-cx, cy - s.h], [s.w - cx, cy - s.h]];
        const th = (90 - d.dir) * Math.PI / 180;
        const c = Math.cos(th);
        const sn = Math.sin(th);
        let l = Infinity; let r = -Infinity; let t = -Infinity; let b = Infinity;
        for (const [px, py] of corners) {
            const x = px * d.scale[0] / 100;
            const y = py * d.scale[1] / 100;
            const rx = (x * c) - (y * sn) + d.pos[0];
            const ry = (x * sn) + (y * c) + d.pos[1];
            l = Math.min(l, rx); r = Math.max(r, rx); t = Math.max(t, ry); b = Math.min(b, ry);
        }
        return {left: l, right: r, top: t, bottom: b, width: r - l, height: t - b};
    }
    getBoundsForBubble (did) { return this.getBounds(did); }
    getFencedPositionOfDrawable (did, position) {
        let x = position[0];
        let y = position[1];
        const d = this.drawables[did];
        if (!d) return [x, y];
        const dx = x - d.pos[0];
        const dy = y - d.pos[1];
        const aabb = this.getBounds(did);
        const inset = Math.floor(Math.min(aabb.width, aabb.height) / 2);
        const sx = 240 - Math.min(15, inset);
        if (aabb.right + dx < -sx) {
            x = Math.ceil(d.pos[0] - (sx + aabb.right));
        } else if (aabb.left + dx > sx) {
            x = Math.floor(d.pos[0] + (sx - aabb.left));
        }
        const sy = 180 - Math.min(15, inset);
        if (aabb.top + dy < -sy) {
            y = Math.ceil(d.pos[1] - (sy + aabb.top));
        } else if (aabb.bottom + dy > sy) {
            y = Math.floor(d.pos[1] + (sy - aabb.bottom));
        }
        return [x, y];
    }
    _overlap (a, b) {
        return a.left < b.right && b.left < a.right && a.bottom < b.top && b.bottom < a.top;
    }
    isTouchingDrawables (did, candidates) {
        const d = this.drawables[did];
        if (!d || !d.visible) return false;
        const a = this.getBounds(did);
        return candidates.some(c => c !== did && this.drawables[c] && this.drawables[c].visible &&
            this._overlap(a, this.getBounds(c)));
    }
    drawableTouching (did, x, y) {
        const d = this.drawables[did];
        if (!d || !d.visible) return false;
        const a = this.getBounds(did);
        return x >= a.left && x <= a.right && y >= a.bottom && y <= a.top;
    }
    isTouchingColor () { return false; }
    pick () { return false; }
    penLine (skin, attrs, x0, y0, x1, y1) {
        emit({t: 'pen', f: frame, op: 'line', c: attrs.color4f.map(v => R(v)), w: R(attrs.diameter),
            p: [R(x0), R(y0), R(x1), R(y1)]});
    }
    penPoint (skin, attrs, x, y) {
        emit({t: 'pen', f: frame, op: 'point', c: attrs.color4f.map(v => R(v)), w: R(attrs.diameter),
            p: [R(x), R(y)]});
    }
    penClear () { emit({t: 'pen', f: frame, op: 'clear'}); }
    penStamp (skin, did) {
        const d = this.drawables[did];
        emit({t: 'pen', f: frame, op: 'stamp', p: [R(d.pos[0]), R(d.pos[1])]});
    }
    draw () {}
}

// ---------------------------------------------------------------------------

const targetState = t => {
    const vars = {};
    const lists = {};
    for (const id of Object.keys(t.variables)) {
        const v = t.variables[id];
        if (v.type === '') vars[v.name] = S(v.value);
        else if (v.type === 'list') lists[v.name] = v.value.map(S);
    }
    const st = {name: t.getName(), stage: t.isStage, clone: !t.isOriginal, vars, lists,
        costume: t.currentCostume};
    if (!t.isStage) {
        Object.assign(st, {x: R(t.x), y: R(t.y), dir: R(t.direction), size: R(t.size),
            visible: t.visible});
    }
    return st;
};

const main = async () => {
    const vm = new VM();
    const storage = new ScratchStorage();
    vm.attachStorage(storage);
    vm.attachRenderer(new StubRenderer());
    vm.attachV2BitmapAdapter({
        resize: (canvas, w, h) => Object.assign(fakeCanvas(), {width: w, height: h}),
        convertDataURIToBinary: () => new Uint8Array(0)
    });
    const rt = vm.runtime;

    rt.on('SAY', (target, type, text) => {
        emit({t: type, f: frame, s: target.getName(), v: S(text)});
    });
    // Questions are answered at the start of the next frame (one per frame),
    // which the native runtime reproduces exactly.
    let answerIdx = 0;
    let questionPending = false;
    rt.on('QUESTION', q => {
        if (q === null) { questionPending = false; return; }
        emit({t: 'ask', f: frame, v: S(q)});
        questionPending = true;
    });

    const data = fs.readFileSync(file);
    await vm.loadProject(data);
    // Library code (immutable.js hash seeding) consumes randomness while
    // loading; the Scratch-visible stream starts here.
    rngState = SEED;

    // Deterministic tick budget: at most TICKS ticks per frame.
    rt.currentStepTime = FRAME_MS;
    rt.sequencer.timer = {
        n: 0,
        start () { this.n = 0; },
        timeElapsed () { return (this.n++) * (0.75 * FRAME_MS / TICKS); }
    };

    const hasEdgeHats = rt.targets.some(t => Object.values(t.blocks._blocks).some(
        b => b.opcode === 'event_whengreaterthan'));

    // timers created while loading (e.g. scratch-vm's canvas pool cleanup) are
    // not part of the project's behaviour
    timers.length = 0;
    vm.greenFlag();
    const flush = async () => {
        for (let i = 0; i < 8; i++) await Promise.resolve();
    };
    await flush();
    for (frame = 0; frame < MAX_FRAMES; frame++) {
        if (questionPending) {
            questionPending = false;
            const a = answerIdx < answers.length ? answers[answerIdx++] : '';
            rt.emit('ANSWER', a);
            await flush();
        }
        // Fire virtual timers that are due.
        timers.sort((a, b) => a.at - b.at || a.id - b.id);
        while (timers.length && timers[0].at <= virtualNow) {
            timers.shift().fn();
            await flush();
        }
        rt._step();
        await flush();
        const idle = rt.threads.length === 0 && timers.length === 0 && !questionPending;
        virtualNow += FRAME_MS;
        if (idle && !hasEdgeHats) { frame++; break; }
    }
    emit({t: 'end', f: frame, targets: rt.executableTargets.slice().reverse().map(targetState)});
    // synchronous: process.exit() can truncate pending async pipe writes
    const buf = Buffer.from(out.join('\n') + '\n');
    let off = 0;
    const pause = new Int32Array(new SharedArrayBuffer(4));
    while (off < buf.length) {
        try {
            off += fs.writeSync(1, buf, off, buf.length - off);
        } catch (e) {
            if (e.code !== 'EAGAIN') throw e;
            Atomics.wait(pause, 0, 0, 5); // pipe full (non-blocking stdout): retry
        }
    }
    process.exit(0);
};

main().catch(e => {
    process.stderr.write(String(e && e.stack || e) + '\n');
    process.exit(2);
});
