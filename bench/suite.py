"""Benchmark projects. Each returns (Project, one-line description).

Compute benchmarks use "run without screen refresh" custom blocks, which is
how Scratch users write heavy code. Every project ends by saying its result,
so the three implementations are also checked for agreement.
"""
from sb3 import *


def fib():
    p = Project()
    acc = p.stage.var("acc", 0)
    s = p.sprite("S")
    s.proc(define("fib %s", ["n"],
                  if_else(lt(arg("n"), 2), [change_var(acc, arg("n"))],
                          [call("fib %s", sub(arg("n"), 1)), call("fib %s", sub(arg("n"), 2))]), warp=True))
    s.script(when_flag(), set_var(acc, 0), call("fib %s", 25), say(acc))
    return p, "recursive fib(25): 242,785 custom-block calls"


def sieve():
    p = Project()
    L = p.stage.list("flags")
    i = p.stage.var("i", 0)
    j = p.stage.var("j", 0)
    c = p.stage.var("count", 0)
    N = 100000
    s = p.sprite("S")
    s.proc(define("sieve", [],
                  list_clear(L), repeat(N, list_add(L, 1)), list_replace(L, 1, 0),
                  set_var(i, 2),
                  until(gt(mul(i, i), N),
                        if_(eq(item(L, i), 1),
                            set_var(j, mul(i, i)),
                            until(gt(j, N), list_replace(L, j, 0), change_var(j, i))),
                        change_var(i, 1)),
                  set_var(c, 0), set_var(i, 1),
                  repeat(N, if_(eq(item(L, i), 1), change_var(c, 1)), change_var(i, 1)),
                  warp=True))
    s.script(when_flag(), call("sieve"), say(c))
    return p, "sieve of Eratosthenes to 100,000 on a Scratch list"


def mandelbrot():
    p = Project()
    v = {n: p.stage.var(n, 0) for n in ["total", "py", "px", "cr", "ci", "zr", "zi", "k", "t"]}
    s = p.sprite("S")
    W, H, MAXI = 160, 120, 64
    zr, zi = v["zr"], v["zi"]
    s.proc(define("mandel", [],
                  set_var(v["total"], 0), set_var(v["py"], 0),
                  repeat(H,
                         set_var(v["px"], 0),
                         repeat(W,
                                set_var(v["cr"], sub(div(mul(v["px"], 3.5), W), 2.5)),
                                set_var(v["ci"], sub(div(mul(v["py"], 2), H), 1)),
                                set_var(zr, 0), set_var(zi, 0), set_var(v["k"], 0),
                                until(or_(gt(add(mul(zr, zr), mul(zi, zi)), 4), not_(lt(v["k"], MAXI))),
                                      set_var(v["t"], add(sub(mul(zr, zr), mul(zi, zi)), v["cr"])),
                                      set_var(zi, add(mul(mul(2, zr), zi), v["ci"])),
                                      set_var(zr, v["t"]),
                                      change_var(v["k"], 1)),
                                change_var(v["total"], v["k"]),
                                change_var(v["px"], 1)),
                         change_var(v["py"], 1)),
                  warp=True))
    s.script(when_flag(), call("mandel"), say(v["total"]))
    return p, "Mandelbrot 160x120, 64 iterations, float math in variables"


def bubble():
    p = Project()
    L = p.stage.list("L")
    seed = p.stage.var("seed", 0)
    i = p.stage.var("i", 0)
    j = p.stage.var("j", 0)
    t = p.stage.var("t", 0)
    N = 1000
    s = p.sprite("S")
    s.proc(define("sort", [],
                  list_clear(L), set_var(seed, 42),
                  repeat(N, set_var(seed, mod(add(mul(seed, 1103515245), 12345), 2147483648)),
                         list_add(L, mod(seed, 10000))),
                  set_var(i, N),
                  until(lt(i, 2),
                        set_var(j, 1),
                        repeat(sub(i, 1),
                               if_(gt(item(L, j), item(L, add(j, 1))),
                                   set_var(t, item(L, j)),
                                   list_replace(L, j, item(L, add(j, 1))),
                                   list_replace(L, add(j, 1), t)),
                               change_var(j, 1)),
                        change_var(i, -1)),
                  warp=True))
    s.script(when_flag(), call("sort"), say(join(item(L, 1), join(" ", item(L, N)))))
    return p, "bubble sort of 1,000 list items (~500k compare/swap)"


def strings():
    p = Project()
    s_ = p.stage.var("s", "")
    r = p.stage.var("r", "")
    i = p.stage.var("i", 0)
    N = 10000
    s = p.sprite("S")
    s.proc(define("build", [],
                  set_var(s_, ""), set_var(i, 0),
                  repeat(N, change_var(i, 1), set_var(s_, join(s_, letter(add(mod(i, 26), 1), "abcdefghijklmnopqrstuvwxyz")))),
                  set_var(r, ""), set_var(i, length(s_)),
                  repeat(length(s_), set_var(r, join(r, letter(i, s_))), change_var(i, -1)),
                  warp=True))
    s.script(when_flag(), call("build"), say(join(length(r), join(" ", letter(1, r)))))
    return p, "build a 10,000-char string with join, then reverse it with letter-of"


def nonwarp_loop():
    p = Project()
    n = p.stage.var("n", 0)
    s = p.sprite("S")
    s.script(when_flag(), set_var(n, 0), repeat(100000, change_var(n, 1)), say(n))
    return p, "100,000 iterations of a normal (screen-refreshing) loop: scheduler overhead"


def clones():
    p = Project()
    done = p.stage.var("done", 0)
    s = p.sprite("S", costumes=[Costume("c", rect_svg(10, 10))])
    s.script(when_flag(), set_var(done, 0), hide(), repeat(100, create_clone()))
    s.script(when_clone(), show(), goto_xy(rnd(-200, 200), rnd(-150, 150)), point_dir(rnd(-180, 180)),
             repeat(200, move(5), bounce()), change_var(done, 1), delete_clone())
    p.stage.script(when_flag(), wait_until(eq(done, 100)), say(done))
    return p, "100 clones each moving and bouncing for 200 frames"


BENCHMARKS = [fib, sieve, mandelbrot, bubble, strings, nonwarp_loop, clones]
