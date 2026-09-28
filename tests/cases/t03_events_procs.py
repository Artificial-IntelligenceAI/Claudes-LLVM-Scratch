# SPDX-License-Identifier: AGPL-3.0-only
"""Broadcasts, procedures, recursion and warp mode."""
from sb3 import *


def case_broadcast():
    p = Project()
    a = p.sprite("A")
    b = p.sprite("B")
    a.script(when_flag(), say("send"), broadcast("go"), say("sent"))
    b.script(when_broadcast("go"), say("b got go"), say("b again"))
    b.script(when_broadcast("GO"), say("b case-insensitive"))
    p.stage.script(when_broadcast("go"), say("stage got go"))
    return p


def case_broadcast_and_wait():
    p = Project()
    a = p.sprite("A")
    b = p.sprite("B")
    a.script(when_flag(), say("before"), broadcast_wait("work"), say("after"), broadcast_wait("nobody"), say("end"))
    b.script(when_broadcast("work"), repeat(3, say("working")), wait(0.1), say("worked"))
    return p


def case_broadcast_restart():
    """Re-broadcasting restarts a running receiver; broadcast-and-wait then stops waiting."""
    p = Project()
    a = p.sprite("A")
    b = p.sprite("B")
    a.script(when_flag(), broadcast("go"), wait(0.05), broadcast("go"), say("a done"))
    b.script(when_broadcast("go"), say("start"), repeat(5, say("loop")), say("finished"))
    return p


def case_broadcast_self_restart():
    p = Project()
    n = p.stage.var("n", 0)
    a = p.sprite("A")
    a.script(when_flag(), broadcast("loop"))
    a.script(when_broadcast("loop"), change_var(n, 1), say(join("in ", n)),
             if_(lt(n, 3), broadcast("loop")), say(join("after ", n)))
    return p


def case_dynamic_broadcast():
    p = Project()
    a = p.sprite("A")
    b = p.sprite("B")
    a.script(when_flag(), broadcast(join("he", "llo")), broadcast_wait(join("W", "AIT")), say("done"))
    b.script(when_broadcast("hello"), say("hello!"))
    b.script(when_broadcast("wait"), say("waited"), say("waited2"))
    return p


def case_procedures():
    p = Project()
    out = p.stage.var("out", "")
    s = p.sprite("S")
    s.proc(define("greet %s and %s", ["a", "b"], say(join(arg("a"), arg("b"))), say(arg("missing"))))
    s.proc(define("check %b", ["c"], if_else(argb("c"), [say("true")], [say("false")]), say(arg("c"))))
    s.proc(define("early %s", ["x"], say("early1"), if_(gt(arg("x"), 1), stop("this script")), say("early2")))
    s.script(when_flag(), call("greet %s and %s", "x", 1), call("check %b", eq(1, 1)), call("check %b", None),
             call("early %s", 5), call("early %s", 0), say(arg("a")))
    return p


def case_recursion_warp():
    p = Project()
    r = p.stage.var("r", 0)
    s = p.sprite("S")
    s.proc(define("fib %s", ["n"],
                  if_else(lt(arg("n"), 2), [change_var(r, arg("n"))],
                          [call("fib %s", sub(arg("n"), 1)), call("fib %s", sub(arg("n"), 2))]), warp=True))
    s.script(when_flag(), set_var(r, 0), call("fib %s", 15), say(r))
    return p


def case_recursion_nonwarp():
    """Non-warp recursive calls yield (isRecursiveCall) -> interleaves with other threads."""
    p = Project()
    s = p.sprite("S")
    s.proc(define("down %s", ["n"], say(join("d", arg("n"))),
                  if_(gt(arg("n"), 0), call("down %s", sub(arg("n"), 1))), say(join("u", arg("n")))))
    s.script(when_flag(), call("down %s", 4), say("done"))
    t = p.sprite("T")
    t.script(when_flag(), repeat(8, say("t")))
    return p


def case_recursion_depth_window():
    """isRecursiveCall only looks 5 stack entries up: deep nesting hides recursion."""
    p = Project()
    s = p.sprite("S")
    s.proc(define("deep %s", ["n"], say(join("deep", arg("n"))),
                  if_(gt(arg("n"), 0), if_(eq(1, 1), if_(eq(1, 1), if_(eq(1, 1), if_(eq(1, 1),
                      if_(eq(1, 1), call("deep %s", sub(arg("n"), 1))))))))))
    s.proc(define("mutA %s", ["n"], say(join("A", arg("n"))), if_(gt(arg("n"), 0), call("mutB %s", sub(arg("n"), 1)))))
    s.proc(define("mutB %s", ["n"], say(join("B", arg("n"))), if_(gt(arg("n"), 0), call("mutA %s", sub(arg("n"), 1)))))
    s.script(when_flag(), call("deep %s", 3), call("mutA %s", 4), say("end"))
    t = p.sprite("T")
    t.script(when_flag(), repeat(12, say("t")))
    return p


def case_warp_loops():
    """Loops inside warp procedures do not yield; other threads see the final state."""
    p = Project()
    n = p.stage.var("n", 0)
    s = p.sprite("S")
    s.proc(define("count", [], repeat(1000, change_var(n, 1)), warp=True))
    s.proc(define("slowcount", [], repeat(3, change_var(n, 1))))
    s.script(when_flag(), call("count"), say(n), call("slowcount"), say(n))
    t = p.sprite("T")
    t.script(when_flag(), repeat(3, say(join("t", n))))
    return p


def case_warp_calls_nonwarp():
    p = Project()
    n = p.stage.var("n", 0)
    s = p.sprite("S")
    s.proc(define("inner", [], repeat(3, change_var(n, 1))))
    s.proc(define("outer", [], call("inner"), call("inner"), say(n), warp=True))
    s.script(when_flag(), call("outer"), call("inner"), say(n))
    t = p.sprite("T")
    t.script(when_flag(), repeat(4, say(join("t", n))))
    return p


def case_warp_with_wait():
    """A warp procedure containing wait(0) cannot be compiled atomically."""
    p = Project()
    s = p.sprite("S")
    s.proc(define("w", [], say("w1"), wait(0), say("w2"), warp=True))
    s.script(when_flag(), call("w"), say("after"))
    t = p.sprite("T")
    t.script(when_flag(), repeat(3, say("t")))
    return p


def case_proc_param_types():
    p = Project()
    L = p.stage.list("L")
    s = p.sprite("S")
    s.proc(define("add %s", ["v"], list_add(L, arg("v"))))
    s.script(when_flag(), call("add %s", 1), call("add %s", add(1, 1)), call("add %s", "3"),
             say(list_contents(L)), say(item(L, 2)))
    return p


def case_undefined_proc_and_loose_stacks():
    p = Project()
    s = p.sprite("S")
    s.script(when_flag(), say("only this"))
    return p
