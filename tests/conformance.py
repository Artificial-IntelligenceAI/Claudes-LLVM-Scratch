#!/usr/bin/env python3
"""
Run scratch-vm's own execute-fixture projects (from the TurboWarp clone,
ref/turbowarp/scratch-vm/test/fixtures/execute/*.sb3) through the native
compiler. Each fixture reports its own results by saying "plan N",
"pass ...", "fail ..." and "end".

For each fixture we report:
  - the fixture's verdict in scratch-vm (the oracle) and natively
  - whether the full traces are identical

    python3 tests/conformance.py [filter]
"""
import concurrent.futures as cf
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "tests"), ROOT]
from run import parse, compare  # noqa: E402

FIXTURES = os.path.join(ROOT, "ref", "turbowarp", "scratch-vm", "test", "fixtures", "execute")
WORK = os.path.join(ROOT, "build", "conformance")


def verdict(events):
    plan, passes, fails, ended = None, 0, [], False
    for e in events:
        if e.get("t") not in ("say", "think"):
            continue
        text = e["v"]
        cmd = text.split(None, 1)[0].lower() if text.strip() else ""
        rest = text[len(cmd):].strip()
        if cmd == "plan":
            try:
                plan = int(float(rest))
            except ValueError:
                pass
        elif cmd == "pass":
            passes += 1
        elif cmd == "fail":
            fails.append(rest)
        elif cmd == "end":
            ended = True
    ok = ended and not fails and (plan is None or plan == passes)
    return {"ok": ok, "plan": plan, "passes": passes, "fails": fails, "ended": ended}


def run_one(fn):
    try:
        return _run_one(fn)
    except subprocess.TimeoutExpired as ex:
        who = "scratch-vm" if "run.js" in " ".join(map(str, ex.cmd)) else "native"
        return fn[:-4], None, None, [f"{who} timed out (warp loop waiting on real time? see README)"], []


def _run_one(fn):
    name = fn[:-4]
    src = os.path.join(FIXTURES, fn)
    exe = os.path.join(WORK, name + ".bin")
    args = ["--frames", "600"]
    ref = subprocess.run(["node", os.path.join(ROOT, "ref", "run.js"), src, *args], capture_output=True, text=True, timeout=60)
    if ref.returncode != 0:
        return name, None, None, [f"oracle failed: {ref.stderr[-300:]}"], []
    rev = parse(ref.stdout)
    c = subprocess.run([sys.executable, "-m", "scc", src, "-o", exe], capture_output=True, text=True, cwd=ROOT, timeout=600)
    warnings = [l for l in c.stderr.splitlines() if "warning" in l]
    if c.returncode != 0:
        return name, verdict(rev), None, [f"compile failed: {c.stderr[-800:]}"], warnings
    nat = subprocess.run([exe, *args], capture_output=True, text=True, timeout=60)
    if nat.returncode != 0:
        return name, verdict(rev), None, [f"native crashed: {nat.stderr[-300:]}"], warnings
    nev = parse(nat.stdout)
    return name, verdict(rev), verdict(nev), compare(rev, nev), warnings


def main():
    flt = sys.argv[1] if len(sys.argv) > 1 else ""
    os.makedirs(WORK, exist_ok=True)
    from scc import rtbuild
    rtbuild.build_runtime()
    # tw-procedure-return-*: TurboWarp's non-standard "return" extension; vanilla
    # scratch-vm does not implement it either, so these are out of scope.
    files = sorted(f for f in os.listdir(FIXTURES) if f.endswith(".sb3") and flt in f
                   and not f.startswith("tw-procedure-return"))
    rows = []
    with cf.ThreadPoolExecutor(max_workers=os.cpu_count() or 2) as ex:
        for name, rv, nv, diffs, warns in ex.map(run_one, files):
            rows.append((name, rv, nv, diffs, warns))
    same = sum(1 for r in rows if not r[3])
    ref_ok = sum(1 for r in rows if r[1] and r[1]["ok"])
    nat_ok = sum(1 for r in rows if r[2] and r[2]["ok"])
    for name, rv, nv, diffs, warns in rows:
        rs = "pass" if rv and rv["ok"] else "FAIL" if rv else "ERR "
        ns = "pass" if nv and nv["ok"] else "FAIL" if nv else "ERR "
        print(f"scratch-vm {rs}  native {ns}  trace {'identical' if not diffs else 'DIFFERS  '}  {name}")
        if diffs:
            for d in diffs[:2]:
                print("      " + str(d).replace("\n", "\n      "))
        if nv and nv["fails"]:
            print(f"      native fails: {nv['fails'][:4]}")
        for w in warns[:2]:
            print("      " + w)
    print(f"\n{len(rows)} fixtures: scratch-vm self-reports pass on {ref_ok}, native on {nat_ok}; "
          f"traces identical on {same}/{len(rows)}")
    with open(os.path.join(WORK, "summary.json"), "w") as f:
        json.dump([{"name": n, "scratch_vm": rv, "native": nv, "identical": not d} for n, rv, nv, d, _ in rows], f, indent=1)


if __name__ == "__main__":
    main()
