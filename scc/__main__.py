"""scc: compile a Scratch 3 project (.sb3) to a native executable via LLVM.

    python3 -m scc project.sb3 -o project [--emit-llvm project.ll] [-O3]
"""
import argparse
import os
import subprocess
import sys
import time

from . import rtbuild
from .load import load
from .analyze import analyze
from .codegen import generate
from .costumes import measure_all


def main(argv=None):
    ap = argparse.ArgumentParser(prog="scc", description="Scratch 3 -> native, via LLVM")
    ap.add_argument("input")
    ap.add_argument("-o", "--output")
    ap.add_argument("--emit-llvm", help="also write the generated LLVM IR here")
    ap.add_argument("-O", dest="opt", default="3", help="optimisation level for LTO (default 3)")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("--time", action="store_true", help="print compile phase timings")
    args = ap.parse_args(argv)

    out = args.output or os.path.splitext(os.path.basename(args.input))[0]
    t0 = time.time()
    objs, abi = rtbuild.build_runtime()
    t1 = time.time()
    prog = load(args.input)
    measure_all(prog)
    analyze(prog)
    ir = generate(prog, abi)
    t2 = time.time()
    ll = args.emit_llvm or out + ".ll"
    with open(ll, "w") as f:
        f.write(ir)
    cmd = [rtbuild.CLANG, f"-O{args.opt}", "-flto", "-fno-math-errno", ll, *objs, "-o", out, "-lm"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if not args.emit_llvm:
        os.remove(ll)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        sys.stderr.write(f"\nscc: LLVM build failed (IR kept at {ll})\n")
        if not args.emit_llvm:
            with open(ll, "w") as f:
                f.write(ir)
        return 1
    t3 = time.time()
    if not args.quiet:
        for w in prog.warnings:
            sys.stderr.write(f"scc: warning: {w}\n")
    if args.time:
        sys.stderr.write(f"scc: runtime {1000*(t1-t0):.0f} ms, frontend {1000*(t2-t1):.0f} ms, "
                         f"llvm {1000*(t3-t2):.0f} ms\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
