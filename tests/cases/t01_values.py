# SPDX-License-Identifier: AGPL-3.0-only
"""Casting, number formatting, operators and exact value types."""
from sb3 import *


def _sayall(exprs):
    p = Project()
    s = p.sprite("S")
    s.script(when_flag(), *[say(e) for e in exprs])
    return p


def case_number_formatting():
    return _sayall([
        add(0.1, 0.2), div(1, 3), div(2, 3), mul(1e21, 1), mul(1e20, 10), div(1, 1e7), div(1, 1e6),
        sub(0, 0), mul(-1, 0), div(1, 0), div(-1, 0), div(0, 0), mul(123456789, 987654321),
        add("1e3", 0), add("0x1F", 0), add("0b101", 0), add("0o17", 0), add(" 12 ", 0),
        add("12abc", 0), add("", 0), add("Infinity", 0), add("-Infinity", 1), add("infinity", 0),
        add("1.", 0), add(".5", 0), add("+.5", 0), add("-.5e-3", 0), add("1e", 0), add("--1", 0),
        add(9007199254740993, 0), div(5e-324, 1), mul(1.7976931348623157e308, 10),
    ])


def case_compare():
    pairs = [("10", "9"), ("10", 9), ("abc", "ABC"), ("abc", "abd"), ("", 0), (" ", 0), ("0", ""),
             ("1e3", 1000), ("0x10", 16), ("Infinity", "infinity"), ("apple", 5), ("-0", "0"),
             (" 5", "5 "), ("\t", "  "), ("a", "b"), ("B", "a"), ("é", "E"), ("10.0", "10")]
    exprs = []
    for a, b in pairs:
        exprs += [lt(a, b), eq(a, b), gt(a, b)]
    return _sayall(exprs)


def case_booleans():
    return _sayall([
        and_(eq(1, 1), eq(2, 2)), or_(eq(1, 2), eq(2, 3)), not_(eq(1, 1)), not_(None),
        join(eq(1, 1), gt(1, 2)), add(eq(1, 1), eq(1, 1)), join(not_(None), "!"),
    ])


def case_string_ops():
    return _sayall([
        join("hello ", "world"), join(1, 2), join(add(1, 2), "x"), letter(1, "hello"), letter(0, "hello"),
        letter(5, "hello"), letter(6, "hello"), letter(2.7, "hello"), letter("2", "hello"),
        length("hello"), length(""), length("héllo"), length(12345), contains("Hello World", "WORLD"),
        contains("abc", ""), contains("", "a"), letter(2, "héllo"), letter(1, "ÉCOLE"),
        contains("ÉCOLE", "é"), join("日本", "語"), length("日本語"), letter(2, "日本語"),
    ])


def case_math():
    exprs = []
    for op in ["abs", "floor", "ceiling", "sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "ln", "log", "e ^", "10 ^"]:
        for x in [0, 1, -1, 0.5, 30, 45, 90, 180, 270, -90, 360, 2.5, 100, -7.25, "abc", 1e10]:
            exprs.append(mathop(op, x))
    return _sayall(exprs)


def case_mod_round():
    exprs = []
    for a, b in [(7, 3), (-7, 3), (7, -3), (-7, -3), (5.5, 2), (0, 5), (5, 0), (-0.5, 1), (1e21, 7), ("x", 3)]:
        exprs.append(mod(a, b))
    for x in [0.5, 1.5, 2.5, -0.5, -1.5, -2.5, 0.49999999999999994, 1e20 + 0.5, -0.4, "3.7", ""]:
        exprs.append(round_(x))
    exprs.append(div(1, round_(-0.4)))
    return _sayall(exprs)


def case_variable_types():
    """set x to 1 stores the *string* "1"; change turns it into a number."""
    p = Project()
    x = p.stage.var("x", 0)
    y = p.stage.var("y", "hello")
    L = p.stage.list("L")
    s = p.sprite("S")
    s.script(when_flag(),
             set_var(x, 1), list_add(L, x), list_add(L, 2), say(list_contents(L)),
             change_var(x, 1), list_add(L, x), say(list_contents(L)),
             set_var(y, add(1, 1)), list_add(L, y), say(list_contents(L)),
             set_var(x, "1.50"), say(x), change_var(x, 0), say(x),
             set_var(x, eq(1, 1)), say(x), set_var(x, join(x, "!")), say(x),
             change_var(y, "abc"), say(y),
             )
    return p


def case_list_ops():
    p = Project()
    L = p.stage.list("L", ["a", "b", "c"])
    M = p.stage.list("M", [1, 2, 3])
    s = p.sprite("S")
    s.script(when_flag(),
             say(list_contents(L)), say(list_contents(M)),
             say(item(L, 1)), say(item(L, 0)), say(item(L, 4)), say(item(L, "last")), say(item(L, "2")),
             say(item(L, 1.9)), say(item(L, "x")), say(item_num(L, "B")), say(item_num(M, "3")),
             say(list_contains(L, "C")), say(list_contains(M, "2.0")), say(list_len(L)),
             list_insert(L, 1, "z"), list_insert(L, 5, "end"), list_insert(L, 7, "no"), list_insert(L, "last", "L"),
             say(list_contents(L)),
             list_replace(L, 2, "R"), list_replace(L, "last", "Q"), list_replace(L, 99, "no"), say(list_contents(L)),
             list_del(L, 1), list_del(L, "last"), list_del(L, 0), say(list_contents(L)),
             list_del(L, "all"), say(list_len(L)), say(list_contents(L)),
             list_add(M, "x y"), say(list_contents(M)), list_clear(M), list_add(M, ""), say(list_contents(M)),
             list_add(M, "é"), list_add(M, "日"), say(list_contents(M)),
             )
    return p


def case_list_stress():
    p = Project()
    L = p.stage.list("L")
    i = p.stage.var("i", 0)
    s = p.sprite("S")
    s.proc(define("fill %s", ["n"],
                  set_var(i, 0),
                  repeat(arg("n"), change_var(i, 1), list_add(L, mul(i, i)), if_(eq(mod(i, 7), 0), list_del(L, 1))),
                  warp=True))
    s.script(when_flag(), call("fill %s", 500), say(list_len(L)), say(item(L, 1)), say(item(L, "last")),
             say(item_num(L, 2500)))
    return p
