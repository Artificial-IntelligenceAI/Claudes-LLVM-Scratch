#!/usr/bin/env python3
"""
Benchmark scratch-vm (interpreter), TurboWarp (JS compiler) and this
project's native LLVM build on the same projects.

    python3 bench/run.py [--runs 5] [name-filter]

All three run from the green flag to completion with frames stepped
back-to-back (no 30 fps sleeping), in real time, with the same seeded RNG.
Results go to bench/results.json and a table on stdout.
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "tools"), os.path.join(ROOT, "bench"), ROOT]

import suite  # noqa: E402

WORK = os.path.join(ROOT, "build", "bench")


def js(sb3, vm, runs):
    r = subprocess.run(["node", os.path.join(ROOT, "ref", "bench.js"), sb3, "--vm", vm, "--runs", str(runs)],
                       capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        return {"error": r.stderr[-500:]}
    return json.loads(r.stdout.strip().splitlines()[-1])


def native(exe, runs):
    times, said = [], None
    frames = None
    for _ in range(runs):
        r = subprocess.run([exe, "--realtime", "--bench"], capture_output=True, text=True, timeout=3600)
        m = re.search(r"frames=(\d+) ticks=(\d+) wall_ms=([\d.]+)", r.stderr)
        times.append(float(m.group(3)))
        frames = int(m.group(1))
        said = [json.loads(l)["v"] for l in r.stdout.splitlines() if l.startswith('{"t":"say"')][-3:]
    times.sort()
    return {"vm": "native", "median_ms": times[len(times) // 2], "min_ms": times[0], "runs": times,
            "frames": frames, "said": said}


def fmt_ms(ms):
    if ms >= 1000:
        return f"{ms / 1000:.2f} s"
    if ms >= 10:
        return f"{ms:.0f} ms"
    return f"{ms:.2f} ms"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("filter", nargs="?", default="")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--js-runs", type=int, default=3)
    args = ap.parse_args()
    os.makedirs(WORK, exist_ok=True)
    from scc import rtbuild
    rtbuild.build_runtime()
    results = []
    for fn in suite.BENCHMARKS:
        name = fn.__name__
        if args.filter not in name:
            continue
        proj, desc = fn()
        sb3 = os.path.join(WORK, name + ".sb3")
        exe = os.path.join(WORK, name + ".bin")
        proj.save(sb3)
        t0 = time.time()
        c = subprocess.run([sys.executable, "-m", "scc", sb3, "-o", exe, "-q"], cwd=ROOT, capture_output=True, text=True)
        compile_s = time.time() - t0
        if c.returncode != 0:
            print(f"{name}: compile failed\n{c.stderr[-2000:]}")
            continue
        n = native(exe, args.runs)
        sv = js(sb3, "scratch", args.js_runs)
        tw = js(sb3, "turbowarp", args.js_runs)
        agree = n.get("said") == sv.get("said") == tw.get("said")
        row = {"name": name, "desc": desc, "native": n, "scratch": sv, "turbowarp": tw,
               "compile_s": compile_s, "binary_bytes": os.path.getsize(exe), "agree": agree}
        results.append(row)
        sp = sv.get("median_ms", float("nan")) / n["median_ms"]
        tp = tw.get("median_ms", float("nan")) / n["median_ms"]
        print(f"{name:14s} scratch-vm {fmt_ms(sv.get('median_ms', float('nan'))):>10s}  "
              f"TurboWarp {fmt_ms(tw.get('median_ms', float('nan'))):>10s}  native {fmt_ms(n['median_ms']):>10s}  "
              f"x{sp:,.0f} vs scratch-vm, x{tp:,.1f} vs TurboWarp  {'agree' if agree else 'DISAGREE ' + str((sv.get('said'), tw.get('said'), n.get('said')))}",
              flush=True)
    with open(os.path.join(ROOT, "bench", "results.json"), "w") as f:
        json.dump(results, f, indent=1)
    if results:
        gm_s = statistics.geometric_mean([r["scratch"]["median_ms"] / r["native"]["median_ms"] for r in results
                                          if "median_ms" in r["scratch"]])
        gm_t = statistics.geometric_mean([r["turbowarp"]["median_ms"] / r["native"]["median_ms"] for r in results
                                          if "median_ms" in r["turbowarp"]])
        print(f"\ngeometric mean speedup: x{gm_s:,.1f} vs scratch-vm, x{gm_t:,.2f} vs TurboWarp")


if __name__ == "__main__":
    main()
