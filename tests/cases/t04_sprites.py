"""Motion, looks, clones, sensing, pen, ask, randomness."""
from sb3 import *


def case_motion():
    p = Project()
    s = p.sprite("S", x=0, y=0)
    s.script(when_flag(),
             move(10), say(x_pos()), turn_right(30), move(10), say(x_pos()), say(y_pos()), say(direction()),
             turn_left(400), say(direction()), point_dir(-200), say(direction()), point_dir(180), say(direction()),
             goto_xy(1000, 1000), say(x_pos()), say(y_pos()), goto_xy(-1000, -50), say(x_pos()),
             set_x(10.5), change_x(0.1), say(x_pos()), set_y(-3), change_y(-0.25), say(y_pos()),
             goto_xy(0.1, 0.2), change_x(0.2), say(x_pos()),
             )
    return p


def case_bounce():
    p = Project()
    s = p.sprite("S", x=0, y=0)
    s.script(when_flag(), point_dir(45), repeat(60, move(15), bounce()), say(round_(x_pos())),
             say(round_(y_pos())), say(round_(direction())))
    return p


def case_glide():
    p = Project()
    s = p.sprite("S", x=0, y=0)
    t = p.sprite("T", x=100, y=-50)
    s.script(when_flag(), glide_xy(0.2, 50, 60), say(x_pos()), glide_xy(0, -20, -20), say(y_pos()))
    s.script(when_broadcast("go"), point_towards("T"), say(round_(direction())), goto("T"), say(x_pos()))
    t.script(when_flag(), wait(0.5), broadcast("go"))
    return p


def case_size_costume():
    p = Project()
    c1 = Costume("one", rect_svg(40, 20))
    c2 = Costume("two", rect_svg(10, 10))
    c3 = Costume("3", rect_svg(30, 30))
    s = p.sprite("S", costumes=[c1, c2, c3])
    s.script(when_flag(),
             set_size(50), say(size()), set_size(10000), say(size()), set_size(1), say(size()), change_size(100), say(size()),
             switch_costume("two"), say(costume_number()), say(costume_name()), next_costume(), say(costume_name()),
             next_costume(), say(costume_name()), switch_costume(2), say(costume_name()), switch_costume("3"),
             say(costume_number()), switch_costume(0), say(costume_number()), switch_costume(-1), say(costume_number()),
             switch_costume("next costume"), say(costume_name()), switch_costume("nope"), say(costume_name()),
             switch_costume(add(1, 1)), say(costume_name()), switch_costume(" "), say(costume_name()),
             )
    return p


def case_backdrops():
    p = Project()
    p.stage.costumes = [Costume("b1", rect_svg(480, 360, "#fff")), Costume("b2", rect_svg(480, 360, "#000")),
                        Costume("b3", rect_svg(480, 360, "#f00"))]
    s = p.sprite("S")
    s.script(when_flag(), switch_backdrop("b2"), say(backdrop_name()), next_backdrop(), say(backdrop_number()),
             next_backdrop(), say(backdrop_name()))
    seen = p.stage.var("seen", 0)
    p.stage.script(Node("event_whenbackdropswitchesto", fields={"BACKDROP": ["b3", None]}), set_var(seen, "b3 seen"))
    return p


def case_visibility_layers():
    p = Project()
    a = p.sprite("A")
    b = p.sprite("B")
    c = p.sprite("C")
    # execution order follows layer order (top first); front/back reorders it
    a.script(when_flag(), say("A"), go_front(), wait(0.05), broadcast("again"))
    b.script(when_flag(), say("B"), go_back())
    c.script(when_flag(), say("C"), go_layers("backward", 1))
    for s in (a, b, c):
        s.script(when_broadcast("again"), say(join("again ", s.name)))
    a.script(when_flag(), hide(), say("hidden"), show(), say("shown"))
    return p


def case_effects():
    p = Project()
    s = p.sprite("S")
    s.script(when_flag(), set_effect("ghost", 150), change_effect("GHOST", -30), change_effect("brightness", 500),
             set_effect("nope", 5), clear_effects(), say("ok"))
    return p


def case_clones():
    p = Project()
    n = p.stage.var("n", 0)
    s = p.sprite("S", x=0, y=0)
    me = s.var("me", 0)
    s.script(when_flag(), set_var(me, "original"), repeat(3, change_var(n, 1), create_clone()), say(n))
    s.script(when_clone(), set_var(me, join("clone", n)), say(me), change_x(n), wait(0.1),
             if_(eq(n, 3), say(join("bye ", me)), delete_clone()), say(join("still ", me)))
    return p


def case_clone_limit():
    p = Project()
    n = p.stage.var("n", 0)
    s = p.sprite("S")
    s.script(when_flag(), repeat(310, create_clone()), say("made"))
    s.script(when_clone(), change_var(n, 1))
    p.stage.script(when_flag(), wait(0.2), say(n))
    return p


def case_clone_of_other():
    p = Project()
    a = p.sprite("A", x=10)
    b = p.sprite("B", x=-10)
    a.script(when_flag(), create_clone("B"), create_clone("Nope"), wait(0.05), broadcast("hi"))
    b.script(when_clone(), say("B clone"), change_y(20))
    b.script(when_broadcast("hi"), say(join("hi from ", y_pos())))
    return p


def case_sensing():
    p = Project()
    a = p.sprite("A", x=0, y=0)
    b = p.sprite("B", x=30, y=40)
    bv = b.var("secret", 42)
    a.script(when_flag(), say(distance_to("B")), say(distance_to("Nope")), say(of("x position", "B")),
             say(of("secret", "B")), say(of("costume name", "B")), say(of("backdrop #", "_stage_")),
             say(of("nope", "B")), say(of("x position", "Nope")), say(touching("B")), say(touching("_edge_")),
             goto_xy(20, 30), say(touching("B")), goto_xy(235, 0), say(touching("_edge_")),
             say(mouse_x()), say(mouse_down()), say(username()), say(key_pressed("space")))
    return p


def case_ask():
    p = Project()
    a = p.sprite("A")
    a.script(when_flag(), ask("name?"), say(join("hi ", answer())), ask("age?"), say(add(answer(), 1)))
    b = p.sprite("B")
    b.script(when_flag(), ask("B asks"), say(join("B got ", answer())))
    return p, {"answers": ["Ada", "36", "yes"]}


def case_say_for_secs():
    p = Project()
    a = p.sprite("A")
    a.script(when_flag(), say_for("hello", 0.1), say("next"), repeat(2, say_for("loop", 0)), say("end"))
    b = p.sprite("B")
    b.script(when_flag(), repeat(6, say("b")))
    return p


def case_pen():
    p = Project()
    s = p.sprite("S", x=0, y=0)
    s.script(when_flag(), pen_clear(), pen_down(), move(50), pen_color("#ff0000"), turn_right(90), move(20),
             pen_size(5), pen_change_param("color", 30), goto_xy(-10, -10), pen_set_param("transparency", 50),
             pen_change_param("brightness", -20), move(1), pen_up(), move(100), pen_stamp(), pen_change_size(-10),
             pen_down(), pen_color(add(123456, 0)), goto_xy(0, 0), pen_color(join("#", "abc")), change_x(1),
             pen_color(join("#1", "2345678")), change_x(1), pen_color(add(4278190335, 0)), change_x(1))
    return p


def case_random_seeded():
    p = Project()
    s = p.sprite("S")
    L = p.stage.list("L", ["a", "b", "c", "d"])
    s.script(when_flag(), say(rnd(1, 10)), say(rnd(1, 10)), say(rnd(0.5, 1)), say(rnd(10, 1)), say(rnd("1", "3")),
             say(rnd("1.0", 3)), say(rnd(5, 5)), say(item(L, "random")), goto("_random_"), say(x_pos()),
             repeat(rnd(1, 3), say("r")), wait(rnd(0, 0.1)), say("end"))
    return p, {"seed": 12345}


def case_days_current():
    p = Project()
    s = p.sprite("S")
    s.script(when_flag(), say(Node("sensing_dayssince2000")),
             say(Node("sensing_current", fields={"CURRENTMENU": ["YEAR", None]})),
             say(Node("sensing_current", fields={"CURRENTMENU": ["hour", None]})))
    return p
