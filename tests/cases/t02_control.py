"""Control flow, loops, yields and multi-thread interleaving."""
from sb3 import *


def case_loops_basic():
    p = Project()
    i = p.stage.var("i", 0)
    s = p.sprite("S")
    s.script(when_flag(),
             repeat(3, say(join("r", i)), change_var(i, 1)),
             repeat(0, say("never")), repeat(-1, say("never")), repeat(2.5, say("r2.5")), repeat("2", say("rs")),
             set_var(i, 0), until(gt(i, 2), say(join("u", i)), change_var(i, 1)),
             while_(lt(i, 5), say(join("w", i)), change_var(i, 1)),
             for_each(i, 3, say(join("f", i))), say(i),
             if_else(eq(i, 3), [say("yes")], [say("no")]),
             if_(eq(i, 4), say("never")),
             )
    return p


def case_interleave():
    """Two sprites each loop; every iteration yields, so they interleave by tick."""
    p = Project()
    a = p.sprite("A")
    b = p.sprite("B")
    a.script(when_flag(), repeat(4, say("a")), say("a done"))
    b.script(when_flag(), repeat(3, say("b")), say("b done"))
    p.stage.script(when_flag(), repeat(2, say("stage")))
    return p


def case_nested_loops_yield():
    p = Project()
    n = p.stage.var("n", 0)
    a = p.sprite("A")
    a.script(when_flag(), repeat(3, repeat(2, change_var(n, 1)), say(n)))
    b = p.sprite("B")
    b.script(when_flag(), repeat(5, say(join("b", n))))
    return p


def case_forever_stop():
    p = Project()
    n = p.stage.var("n", 0)
    a = p.sprite("A")
    a.script(when_flag(), forever(change_var(n, 1), if_(gt(n, 5), say("stopping"), stop("this script")), say(n)))
    b = p.sprite("B")
    b.script(when_flag(), forever(say(join("b", n)), if_(gt(n, 3), stop("this script"))))
    return p


def case_stop_all():
    """stop all: threads later in the same tick still run once (scratch-vm quirk)."""
    p = Project()
    a = p.sprite("A")
    b = p.sprite("B")
    c = p.sprite("C")
    a.script(when_flag(), say("a1"), say("a2"))
    b.script(when_flag(), say("b1"), stop("all"), say("never"))
    c.script(when_flag(), say("c1"), repeat(3, say("c loop")))
    return p


def case_stop_other():
    p = Project()
    a = p.sprite("A")
    a.script(when_flag(), repeat(10, say("x")))
    a.script(when_flag(), repeat(2, say("y")), stop("other scripts in sprite"), say("z"))
    b = p.sprite("B")
    b.script(when_flag(), repeat(4, say("b")))
    return p


def case_wait_timing():
    p = Project()
    a = p.sprite("A")
    a.script(when_flag(), say("start"), wait(0.1), say("0.1"), wait(0), say("0"), wait(1), say("1"),
             wait(-5), say("neg"), wait("abc"), say("abc"), say(round_(timer())))
    b = p.sprite("B")
    b.script(when_flag(), repeat(10, say("tick")))
    return p


def case_wait_until():
    p = Project()
    flag = p.stage.var("flag", 0)
    a = p.sprite("A")
    a.script(when_flag(), wait_until(eq(flag, 1)), say("released"))
    b = p.sprite("B")
    b.script(when_flag(), repeat(3, say("working")), set_var(flag, 1), say("set"))
    return p


def case_timer_hat():
    p = Project()
    a = p.sprite("A")
    a.script(when_timer_gt(0.5), say("timer > 0.5"))
    a.script(when_flag(), wait(1), reset_timer(), say("reset"), wait(0.7), say("done"))
    return p, {"frames": 90}


def case_counter():
    p = Project()
    a = p.sprite("A")
    a.script(when_flag(), clear_counter(), repeat(5, incr_counter()), say(counter()))
    return p


def case_many_threads():
    p = Project()
    acc = p.stage.var("acc", "")
    for k in range(6):
        s = p.sprite(f"T{k}")
        s.script(when_flag(), repeat(3, set_var(acc, join(acc, k))))
    p.stage.script(when_flag(), wait(0.2), say(acc))
    return p


def case_tick_budget():
    """A long non-warp loop spans frames according to the tick budget."""
    p = Project()
    n = p.stage.var("n", 0)
    a = p.sprite("A")
    a.script(when_flag(), repeat(200, change_var(n, 1)), say(n))
    b = p.sprite("B")
    b.script(when_flag(), wait(0.05), say(n), wait(0.05), say(n))
    return p
