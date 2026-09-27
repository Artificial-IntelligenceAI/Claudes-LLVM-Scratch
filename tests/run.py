#!/usr/bin/env python3
"""
Differential test runner.

Every test case builds a Scratch project with tools/sb3.py, then:
  1. runs it in real scratch-vm (ref/run.js, deterministic),
  2. compiles it with scc and runs the native binary,
  3. compares the two JSON-lines traces event by event (including frame
     numbers) and the final state of every target.

    python3 tests/run.py            # all cases
    python3 tests/run.py lists -v   # cases whose name contains "lists"
"""

import argparse
import concurrent.futures as cf
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)

CASES_DIR = os.path.join(ROOT, "tests", "cases")
WORK = os.path.join(ROOT, "build", "tests")


def discover():
    cases = []
    for fn in sorted(os.listdir(CASES_DIR)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(fn[:-3], os.path.join(CASES_DIR, fn))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for name in dir(mod):
            if name.startswith("case_"):
                fn_ = getattr(mod, name)
                cases.append((f"{fn[:-3]}.{name[5:]}", fn_))
    return cases


def run(cmd, timeout=120):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def parse(out):
    events = []
    for line in out.splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def norm_end(ev):
    ts = ev["targets"]
    return {"f": ev["f"], "targets": ts}


def compare(ref, nat):
    """Return a list of human-readable differences (empty if identical)."""
    diffs = []
    re_, ne_ = [e for e in ref if e["t"] != "end"], [e for e in nat if e["t"] != "end"]
    for i in range(max(len(re_), len(ne_))):
        a = re_[i] if i < len(re_) else None
        b = ne_[i] if i < len(ne_) else None
        if a != b:
            diffs.append(f"event {i}:\n    scratch-vm: {json.dumps(a)}\n    native:     {json.dumps(b)}")
            if len(diffs) >= 5:
                break
    ra = [e for e in ref if e["t"] == "end"]
    na = [e for e in nat if e["t"] == "end"]
    if ra and na:
        a, b = norm_end(ra[0]), norm_end(na[0])
        if a != b:
            if a["f"] != b["f"]:
                diffs.append(f"end frame: scratch-vm {a['f']} vs native {b['f']}")
            at, bt = a["targets"], b["targets"]
            if len(at) != len(bt):
                diffs.append(f"target count: scratch-vm {len(at)} vs native {len(bt)}")
            for x, y in zip(at, bt):
                if x != y:
                    keys = sorted(set(x) | set(y))
                    d = {k: (x.get(k), y.get(k)) for k in keys if x.get(k) != y.get(k)}
                    diffs.append(f"target {x.get('name')}: {json.dumps(d)}")
    elif ra or na:
        diffs.append("missing end event")
    return diffs


def run_case(name, fn, verbose=False):
    os.makedirs(WORK, exist_ok=True)
    spec = fn()
    if isinstance(spec, tuple):
        project, opts = spec
    else:
        project, opts = spec, {}
    base = os.path.join(WORK, name)
    sb3 = base + ".sb3"
    project.save(sb3)
    extra = []
    if "frames" in opts:
        extra += ["--frames", str(opts["frames"])]
    if "ticks" in opts:
        extra += ["--ticks", str(opts["ticks"])]
    if "answers" in opts:
        extra += ["--answers", ",".join(opts["answers"])]
    if "seed" in opts:
        extra += ["--seed", str(opts["seed"])]
    t0 = time.time()
    code, ref_out, ref_err = run(["node", os.path.join(ROOT, "ref", "run.js"), sb3, *extra])
    if code != 0:
        return name, "ERROR", [f"oracle failed: {ref_err[-2000:]}"], 0
    exe = base + ".bin"
    code, _, err = run([sys.executable, "-m", "scc", sb3, "-o", exe, "-q"], timeout=300)
    if code != 0:
        return name, "ERROR", [f"compile failed: {err[-3000:]}"], 0
    code, nat_out, nat_err = run([exe, *extra])
    if code != 0:
        return name, "ERROR", [f"native exited {code}: {nat_err[-2000:]}"], 0
    diffs = compare(parse(ref_out), parse(nat_out))
    if verbose and not diffs:
        print(f"--- {name}\n{ref_out}")
    return name, ("PASS" if not diffs else "FAIL"), diffs, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("filter", nargs="?", default="")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 2)
    args = ap.parse_args()
    # build the runtime once up front (parallel compiles would race)
    from scc import rtbuild
    rtbuild.build_runtime()
    cases = [(n, f) for n, f in discover() if args.filter in n]
    results = []
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = [ex.submit(run_case, n, f, args.verbose) for n, f in cases]
        for fut in cf.as_completed(futs):
            name, status, diffs, dt = fut.result()
            results.append((name, status, diffs))
            mark = {"PASS": "ok  ", "FAIL": "FAIL", "ERROR": "ERR "}[status]
            print(f"{mark} {name}" + (f"  ({dt:.1f}s)" if status == "PASS" else ""))
            for d in diffs:
                print("      " + d.replace("\n", "\n      "))
    passed = sum(1 for _, s, _ in results if s == "PASS")
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
