#!/usr/bin/env node
/*
 * Benchmark runner for the JavaScript implementations.
 *
 *   node bench.js project.sb3 --vm scratch|turbowarp [--runs N]
 *
 * Runs the project from the green flag until no threads remain, stepping
 * frames back-to-back (no 30 fps sleeping) in real time, and reports the
 * wall time. Uses the same stub renderer as the oracle so redraw requests
 * end frames exactly as they would in the editor. Math.random is seeded so
 * all implementations do identical work.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const {performance} = require('perf_hooks');

const args = process.argv.slice(2);
const file = args[0];
const opt = (n, d) => { const i = args.indexOf(n); return i >= 0 ? args[i + 1] : d; };
const which = opt('--vm', 'scratch');
const runs = Number(opt('--runs', 3));

let rng = 1;
Math.random = function () {
    rng = (rng + 0x6D2B79F5) | 0;
    let t = Math.imul(rng ^ (rng >>> 15), 1 | rng);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
};

// TurboWarp's runtime checks document.hidden before drawing.
if (which === 'turbowarp' && typeof document === 'undefined') global.document = {hidden: false};

const vmSrc = which === 'turbowarp' ?
    path.join(__dirname, 'turbowarp/scratch-vm/src') :
    path.join(__dirname, 'node_modules/scratch-vm/src');
const VM = require(path.join(vmSrc, 'index.js'));
let ScratchStorage = require(path.join(__dirname, 'node_modules/scratch-storage'));
if (ScratchStorage.ScratchStorage) ScratchStorage = ScratchStorage.ScratchStorage;
if (ScratchStorage.default) ScratchStorage = ScratchStorage.default;

// Reuse the oracle's stub renderer by extracting it from run.js would couple
// the files; the class is small, so load it via a tiny shim module.
const {StubRenderer} = require('./stub-renderer.js');

const once = async () => {
    rng = 1;
    const vm = new VM();
    vm.attachStorage(new ScratchStorage());
    vm.attachRenderer(new StubRenderer());
    if (vm.setCompilerOptions && which === 'turbowarp') vm.setCompilerOptions({enabled: true, warpTimer: false});
    if (vm.setCompilerOptions && which !== 'turbowarp') { /* scratch-vm has no compiler */ }
    const said = [];
    vm.runtime.on('SAY', (t, type, text) => said.push(String(text)));
    await vm.loadProject(fs.readFileSync(file));
    rng = 1;
    vm.runtime.currentStepTime = 1000 / 30;
    if (vm.runtime.frameLoop) { /* TurboWarp: we drive _step ourselves */ }
    vm.greenFlag();
    const t0 = performance.now();
    let frames = 0;
    while (vm.runtime.threads.length > 0 && frames < 1000000) {
        vm.runtime._step();
        frames++;
        if ((frames & 63) === 0) await new Promise(r => setImmediate(r));
    }
    const ms = performance.now() - t0;
    return {ms, frames, said};
};

(async () => {
    const results = [];
    for (let i = 0; i < runs; i++) results.push(await once());
    const times = results.map(r => r.ms).sort((a, b) => a - b);
    const out = {vm: which, median_ms: times[Math.floor(times.length / 2)], min_ms: times[0],
        runs: times, frames: results[0].frames, said: results[0].said.slice(-3)};
    process.stdout.write(JSON.stringify(out) + '\n');
    process.exit(0);
})().catch(e => { console.error(e && e.stack || e); process.exit(2); });
