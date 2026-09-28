'use strict';
// Stub renderer shared by the benchmark runner (a copy of the one in run.js,
// minus trace output): redraw requests, fencing and set-size clamping behave
// as in the editor, using the rotated costume rectangle as sprite bounds.
const emit = () => {};
const frame = 0;
const R = v => v;
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

module.exports = {StubRenderer};
