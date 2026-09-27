#!/usr/bin/env python3
"""
Differential fuzzer: generate random Scratch projects, run them in scratch-vm
and natively, and report any divergence.

    python3 tests/fuzz.py --count 200 --start 0 -j 4

Failing projects are kept in build/fuzz/ for reproduction.
"""
import argparse
import concurrent.futures as cf
import os
import random
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)

import sb3  # noqa: E402
from sb3 import *  # noqa: E402,F401,F403
from run import parse, compare  # noqa: E402

WORK = os.path.join(ROOT, "build", "fuzz")

WEIRD = ["", " ", "0", "1", "-1", "1.5", "abc", "ABC", "1e3", "0x10", "-0", "Infinity", "10", "9", "true",
         "false", "é", "日本", " 7 ", ".5", "1.", "NaN", "a b", "3.0", "007", "+4", "1e21", "x"]
NUMS = [0.0, 1.0, -1.0, 2.0, 3.0, 0.5, -2.5, 10.0, 100.0, 1e21, 0.1, 7.0, 360.0, -0.0, 1e-7, 123456789.0]


class Gen:
    def __init__(self, seed):
        self.r = random.Random(seed)
        self.p = Project()
        self.gvars = [self.p.stage.var(f"g{i}", self.lit()) for i in range(3)]
        self.glists = [self.p.stage.list(f"L{i}", [self.lit() for _ in range(self.r.randint(0, 4))]) for i in range(2)]
        self.procs = []   # (sprite, proccode, argnames)
        self.msgs = ["m0", "m1", "m2"]
        self.budget = 0

    def lit(self):
        return self.r.choice(WEIRD) if self.r.random() < 0.55 else self.r.choice(NUMS)

    # -- expressions -----------------------------------------------------------------------

    def expr(self, d, ctx):
        r = self.r
        if d <= 0 or r.random() < 0.3:
            c = r.random()
            if c < 0.45:
                return self.lit()
            if c < 0.75:
                return r.choice(self.gvars + ctx["locals"])
            if c < 0.85 and ctx["args"]:
                return arg(r.choice(ctx["args"]))
            if c < 0.92:
                return list_contents(r.choice(self.glists))
            return r.choice([x_pos(), y_pos(), direction(), size(), costume_number(), timer(), answer()])
        k = r.randint(0, 17)
        e = lambda: self.expr(d - 1, ctx)  # noqa: E731
        if k == 0: return add(e(), e())
        if k == 1: return sub(e(), e())
        if k == 2: return mul(e(), e())
        if k == 3: return div(e(), e())
        if k == 4: return mod(e(), e())
        if k == 5: return join(e(), e())
        if k == 6: return letter(e(), e())
        if k == 7: return length(e())
        if k == 8: return round_(e())
        if k == 9: return mathop(r.choice(["abs", "floor", "ceiling", "sqrt", "sin", "cos", "tan", "asin",
                                           "acos", "atan", "ln", "log", "e ^", "10 ^"]), e())
        if k == 10: return item(r.choice(self.glists), r.choice([e(), 1.0, "last", "random", 0.0, 2.0]))
        if k == 11: return item_num(r.choice(self.glists), e())
        if k == 12: return list_len(r.choice(self.glists))
        if k == 13: return rnd(e(), e())
        if k == 14: return self.cond(d - 1, ctx)
        if k == 15: return contains(e(), e())
        if k == 16: return list_contains(r.choice(self.glists), e())
        return join(self.cond(d - 1, ctx), e())

    def cond(self, d, ctx):
        r = self.r
        e = lambda: self.expr(d - 1, ctx)  # noqa: E731
        k = r.randint(0, 6)
        if k == 0: return lt(e(), e())
        if k == 1: return gt(e(), e())
        if k == 2: return eq(e(), e())
        if k == 3: return and_(self.cond(d - 1, ctx) if d > 0 else eq(e(), e()), eq(e(), e()))
        if k == 4: return or_(eq(e(), e()), lt(e(), e()))
        if k == 5: return not_(eq(e(), e()))
        return contains(e(), e())

    # -- statements -------------------------------------------------------------------------

    def stmts(self, n, d, ctx):
        return [s for s in (self.stmt(d, ctx) for _ in range(n)) if s is not None]

    def stmt(self, d, ctx):
        r = self.r
        self.budget += 1
        if self.budget > 120:
            return say(self.expr(1, ctx))
        k = r.randint(0, 22)
        E = lambda dd=2: self.expr(dd, ctx)  # noqa: E731
        var = lambda: r.choice(self.gvars + ctx["locals"])  # noqa: E731
        L = lambda: r.choice(self.glists)  # noqa: E731
        if k <= 3: return say(E(3))
        if k == 4: return set_var(var(), E())
        if k == 5: return change_var(var(), E(1))
        if k == 6: return list_add(L(), E())
        if k == 7: return list_insert(L(), r.choice([1.0, "last", 2.0, E(1)]), E())
        if k == 8: return list_del(L(), r.choice([1.0, "last", "all", E(1)]))
        if k == 9: return list_replace(L(), r.choice([1.0, "last", E(1)]), E())
        if k == 10 and d > 0: return if_else(self.cond(2, ctx), self.stmts(2, d - 1, ctx), self.stmts(1, d - 1, ctx))
        if k == 11 and d > 0: return if_(self.cond(2, ctx), *self.stmts(2, d - 1, ctx))
        if k == 12 and d > 0:
            return repeat(r.choice([0.0, 1.0, 2.0, 3.0, "2", 2.5, E(1)]) if r.random() < 0.8 else 2.0,
                          *self.stmts(2, d - 1, ctx))
        if k == 13 and d > 0 and ctx["locals"]:
            # bounded until: guard variable
            g = ctx["locals"][0]
            return repeat(1.0, set_var(g, 0.0),
                          until(or_(gt(g, 3.0), self.cond(1, ctx)), change_var(g, 1.0), *self.stmts(1, d - 1, ctx)))
        if k == 14: return broadcast(r.choice(self.msgs))
        if k == 15 and ctx["can_wait"]: return broadcast_wait(r.choice(self.msgs))
        if k == 16 and ctx["can_wait"]: return wait(r.choice([0.0, 0.05, 0.1, E(1)]) if r.random() < 0.9 else 0.0)
        if k == 17: return r.choice([move(E(1)), turn_right(E(1)), goto_xy(E(1), E(1)), change_x(E(1)),
                                     point_dir(E(1)), set_size(E(1)), change_size(E(1)), next_costume(),
                                     bounce(), show(), hide()])
        if k == 18 and self.procs and ctx["depth_arg"] is not None:
            sp, code, names = r.choice(self.procs)
            if sp is ctx["sprite"]:
                return call(code, *[sub(arg(ctx["depth_arg"]), 1.0) if i == 0 else E(1) for i in range(len(names))])
        if k == 18 and self.procs:
            sp, code, names = r.choice(self.procs)
            if sp is ctx["sprite"]:
                return call(code, *[r.choice([2.0, 3.0, "2"]) if i == 0 else E(1) for i in range(len(names))])
        if k == 19 and ctx["clones"]: return create_clone()
        if k == 20: return for_each(var(), r.choice([0.0, 2.0, "3", E(1)]), say(var()))
        if k == 21 and ctx["can_wait"]: return say_for(E(1), r.choice([0.0, 0.05]))
        return say(E(2))

    # -- program ------------------------------------------------------------------------------

    def build(self):
        r = self.r
        sprites = [self.p.sprite(f"S{i}", x=r.choice([0, 10, -30]), y=r.choice([0, 5]),
                                 costumes=[Costume("c1", rect_svg(40, 30)), Costume("c2", rect_svg(12, 60))])
                   for i in range(r.randint(1, 3))]
        # procedures first (so calls can be generated)
        for s in sprites:
            for j in range(r.randint(0, 2)):
                code = f"p{j} %s" + (" %s" if r.random() < 0.5 else "")
                names = ["depth"] + (["x"] if code.count("%s") > 1 else [])
                self.procs.append((s, code, names))
        for s in sprites:
            loc = [s.var(f"l{s.name}", self.lit())]
            for (sp, code, names) in [pp for pp in self.procs if pp[0] is s]:
                warp = r.random() < 0.5
                ctx = {"locals": loc, "args": names, "can_wait": not warp, "depth_arg": "depth",
                       "sprite": s, "clones": False}
                self.budget = 0
                body = self.stmts(r.randint(1, 4), 2, ctx)
                s.proc(define(code, names, if_(gt(arg("depth"), 0), *body), warp=warp))
            for j in range(r.randint(1, 2)):
                ctx = {"locals": loc, "args": [], "can_wait": True, "depth_arg": None, "sprite": s,
                       "clones": r.random() < 0.3}
                self.budget = 0
                s.script(when_flag(), *self.stmts(r.randint(2, 6), 3, ctx))
            ctx = {"locals": loc, "args": [], "can_wait": True, "depth_arg": None, "sprite": s, "clones": False}
            self.budget = 0
            s.script(when_broadcast(r.choice(self.msgs)), *self.stmts(r.randint(1, 3), 2, ctx))
            s.script(when_clone(), say("clone"), *self.stmts(1, 1, ctx), delete_clone())
        ctx = {"locals": [], "args": [], "can_wait": True, "depth_arg": None, "sprite": self.p.stage, "clones": False}
        self.budget = 0
        self.p.stage.script(when_flag(), *self.stmts(r.randint(1, 3), 2, ctx))
        return self.p


def run_seed(seed, keep_all=False):
    os.makedirs(WORK, exist_ok=True)
    base = os.path.join(WORK, f"fuzz{seed}")
    try:
        proj = Gen(seed).build()
    except Exception as ex:  # generator bug: skip
        return seed, "GENFAIL", [repr(ex)]
    proj.save(base + ".sb3")
    extra = ["--frames", "150", "--seed", str(seed + 1)]
    try:
        ref = subprocess.run(["node", os.path.join(ROOT, "ref", "run.js"), base + ".sb3", *extra],
                             capture_output=True, text=True, timeout=120)
        if ref.returncode != 0:
            return seed, "ORACLE", [ref.stderr[-500:]]
        c = subprocess.run([sys.executable, "-m", "scc", base + ".sb3", "-o", base + ".bin", "-q"],
                           capture_output=True, text=True, timeout=300, cwd=ROOT)
        if c.returncode != 0:
            return seed, "COMPILE", [c.stderr[-1500:]]
        nat = subprocess.run([base + ".bin", *extra], capture_output=True, text=True, timeout=120)
        if nat.returncode != 0:
            return seed, "CRASH", [nat.stderr[-500:]]
        diffs = compare(parse(ref.stdout), parse(nat.stdout))
    except subprocess.TimeoutExpired:
        return seed, "TIMEOUT", []
    if not diffs and not keep_all:
        for ext in (".sb3", ".bin"):
            try:
                os.remove(base + ext)
            except OSError:
                pass
    return seed, ("PASS" if not diffs else "FAIL"), diffs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 2)
    args = ap.parse_args()
    from scc import rtbuild
    rtbuild.build_runtime()
    stats = {}
    fails = []
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = [ex.submit(run_seed, s) for s in range(args.start, args.start + args.count)]
        for fut in cf.as_completed(futs):
            seed, status, diffs = fut.result()
            stats[status] = stats.get(status, 0) + 1
            if status != "PASS":
                fails.append((seed, status, diffs))
                print(f"{status} seed={seed}")
                for d in diffs[:3]:
                    print("    " + str(d).replace("\n", "\n    "))
    print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(stats.items())))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
