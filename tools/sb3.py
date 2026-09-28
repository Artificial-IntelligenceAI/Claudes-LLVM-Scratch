# SPDX-License-Identifier: AGPL-3.0-only
"""
A small Python DSL for authoring Scratch 3 projects (.sb3) in code.

Used to build the test and benchmark corpus. The output is a real .sb3 that
loads in the Scratch editor, in scratch-vm, and in TurboWarp.

    from sb3 import *
    p = Project()
    n = p.stage.var("n", 0)
    cat = p.sprite("Cat")
    cat.script(when_flag(),
        set_var(n, 0),
        repeat(10, change_var(n, 1)),
        say(join("n=", n)),
    )
    p.save("hello.sb3")
"""

import hashlib
import io
import json
import zipfile

# ---------------------------------------------------------------------------
# IDs


class _Ids:
    def __init__(self):
        self.n = 0

    def __call__(self, prefix="b"):
        self.n += 1
        return f"{prefix}{self.n}"


# ---------------------------------------------------------------------------
# Expression / block nodes


class Node:
    """A block, possibly with inputs, fields and a mutation."""

    def __init__(self, opcode, inputs=None, fields=None, mutation=None, shadow=False):
        self.opcode = opcode
        self.inputs = inputs or {}      # name -> (kind, value)  kind: 'num','text','bool','stack', 'menu', ...
        self.fields = fields or {}      # name -> [value, id]
        self.mutation = mutation
        self.shadow = shadow


class Var:
    def __init__(self, name, value, vid, is_list=False):
        self.name = name
        self.value = value
        self.id = vid
        self.is_list = is_list


class Literal:
    def __init__(self, value):
        self.value = value


# Primitive codes used in compressed sb3 inputs
MATH_NUMBER, POSITIVE_NUMBER, WHOLE_NUMBER, INTEGER, ANGLE, COLOR, TEXT, BROADCAST, VARIABLE, LIST = 4, 5, 6, 7, 8, 9, 10, 11, 12, 13


def _num(x):
    return ("prim", MATH_NUMBER, x)


def _text(x):
    return ("prim", TEXT, x)


def _whole(x):
    return ("prim", WHOLE_NUMBER, x)


def _int(x):
    return ("prim", INTEGER, x)


def _angle(x):
    return ("prim", ANGLE, x)


def _pos(x):
    return ("prim", POSITIVE_NUMBER, x)


def _color(x):
    return ("prim", COLOR, x)


def _bool(x):
    return ("bool", x)


def _stack(blocks):
    return ("stack", list(blocks))


def _menu(opcode, field, value):
    return ("menu", opcode, field, value)


# ---------------------------------------------------------------------------
# Hats


def when_flag():
    return Node("event_whenflagclicked")


def when_broadcast(msg):
    return Node("event_whenbroadcastreceived", fields={"BROADCAST_OPTION": ("broadcast", msg)})


def when_clone():
    return Node("control_start_as_clone")


def when_timer_gt(value):
    return Node("event_whengreaterthan", inputs={"VALUE": _num(value)},
                fields={"WHENGREATERTHANMENU": ["TIMER", None]})


def when_key(key):
    return Node("event_whenkeypressed", fields={"KEY_OPTION": [key, None]})


def when_clicked():
    return Node("event_whenthisspriteclicked")


# ---------------------------------------------------------------------------
# Events


def broadcast(msg):
    return Node("event_broadcast", inputs={"BROADCAST_INPUT": ("broadcast", msg)})


def broadcast_wait(msg):
    return Node("event_broadcastandwait", inputs={"BROADCAST_INPUT": ("broadcast", msg)})


# ---------------------------------------------------------------------------
# Control


def repeat(times, *body):
    return Node("control_repeat", inputs={"TIMES": _whole(times), "SUBSTACK": _stack(body)})


def forever(*body):
    return Node("control_forever", inputs={"SUBSTACK": _stack(body)})


def if_(cond, *body):
    return Node("control_if", inputs={"CONDITION": _bool(cond), "SUBSTACK": _stack(body)})


def if_else(cond, then, else_):
    return Node("control_if_else", inputs={"CONDITION": _bool(cond),
                                           "SUBSTACK": _stack(then), "SUBSTACK2": _stack(else_)})


def until(cond, *body):
    return Node("control_repeat_until", inputs={"CONDITION": _bool(cond), "SUBSTACK": _stack(body)})


def while_(cond, *body):
    return Node("control_while", inputs={"CONDITION": _bool(cond), "SUBSTACK": _stack(body)})


def for_each(var, value, *body):
    return Node("control_for_each", inputs={"VALUE": _num(value), "SUBSTACK": _stack(body)},
                fields={"VARIABLE": ("var", var)})


def wait(secs):
    return Node("control_wait", inputs={"DURATION": _pos(secs)})


def wait_until(cond):
    return Node("control_wait_until", inputs={"CONDITION": _bool(cond)})


def stop(option="all"):
    has_next = "true" if option == "other scripts in sprite" or option == "other scripts in stage" else "false"
    return Node("control_stop", fields={"STOP_OPTION": [option, None]},
                mutation={"tagName": "mutation", "children": [], "hasnext": has_next})


def create_clone(of="_myself_"):
    return Node("control_create_clone_of",
                inputs={"CLONE_OPTION": _menu("control_create_clone_of_menu", "CLONE_OPTION", of)})


def delete_clone():
    return Node("control_delete_this_clone")


def counter():
    return Node("control_get_counter")


def incr_counter():
    return Node("control_incr_counter")


def clear_counter():
    return Node("control_clear_counter")


# ---------------------------------------------------------------------------
# Operators


def _bin(op, a, b, k1="NUM1", k2="NUM2", kind=_num):
    return Node(op, inputs={k1: kind(a), k2: kind(b)})


def add(a, b): return _bin("operator_add", a, b)
def sub(a, b): return _bin("operator_subtract", a, b)
def mul(a, b): return _bin("operator_multiply", a, b)
def div(a, b): return _bin("operator_divide", a, b)
def mod(a, b): return _bin("operator_mod", a, b)
def rnd(a, b): return _bin("operator_random", a, b, "FROM", "TO")
def gt(a, b): return _bin("operator_gt", a, b, "OPERAND1", "OPERAND2", _text)
def lt(a, b): return _bin("operator_lt", a, b, "OPERAND1", "OPERAND2", _text)
def eq(a, b): return _bin("operator_equals", a, b, "OPERAND1", "OPERAND2", _text)
def and_(a, b): return Node("operator_and", inputs={"OPERAND1": _bool(a), "OPERAND2": _bool(b)})
def or_(a, b): return Node("operator_or", inputs={"OPERAND1": _bool(a), "OPERAND2": _bool(b)})
def not_(a): return Node("operator_not", inputs={"OPERAND": _bool(a)})
def join(a, b): return _bin("operator_join", a, b, "STRING1", "STRING2", _text)
def letter(i, s): return Node("operator_letter_of", inputs={"LETTER": _whole(i), "STRING": _text(s)})
def length(s): return Node("operator_length", inputs={"STRING": _text(s)})
def contains(a, b): return _bin("operator_contains", a, b, "STRING1", "STRING2", _text)
def round_(x): return Node("operator_round", inputs={"NUM": _num(x)})


def mathop(op, x):
    return Node("operator_mathop", inputs={"NUM": _num(x)}, fields={"OPERATOR": [op, None]})


# ---------------------------------------------------------------------------
# Data


def set_var(v, x):
    return Node("data_setvariableto", inputs={"VALUE": _text(x)}, fields={"VARIABLE": ("var", v)})


def change_var(v, x):
    return Node("data_changevariableby", inputs={"VALUE": _num(x)}, fields={"VARIABLE": ("var", v)})


def show_var(v): return Node("data_showvariable", fields={"VARIABLE": ("var", v)})
def hide_var(v): return Node("data_hidevariable", fields={"VARIABLE": ("var", v)})


def list_add(L, x): return Node("data_addtolist", inputs={"ITEM": _text(x)}, fields={"LIST": ("list", L)})
def list_del(L, i): return Node("data_deleteoflist", inputs={"INDEX": _int(i)}, fields={"LIST": ("list", L)})
def list_clear(L): return Node("data_deletealloflist", fields={"LIST": ("list", L)})


def list_insert(L, i, x):
    return Node("data_insertatlist", inputs={"INDEX": _int(i), "ITEM": _text(x)}, fields={"LIST": ("list", L)})


def list_replace(L, i, x):
    return Node("data_replaceitemoflist", inputs={"INDEX": _int(i), "ITEM": _text(x)}, fields={"LIST": ("list", L)})


def item(L, i): return Node("data_itemoflist", inputs={"INDEX": _int(i)}, fields={"LIST": ("list", L)})
def item_num(L, x): return Node("data_itemnumoflist", inputs={"ITEM": _text(x)}, fields={"LIST": ("list", L)})
def list_len(L): return Node("data_lengthoflist", fields={"LIST": ("list", L)})
def list_contains(L, x): return Node("data_listcontainsitem", inputs={"ITEM": _text(x)}, fields={"LIST": ("list", L)})
def list_contents(L): return ("listref", L)


# ---------------------------------------------------------------------------
# Procedures


class ProcRef:
    def __init__(self, proccode, argnames, argkinds, warp):
        self.proccode = proccode
        self.argnames = argnames
        self.argkinds = argkinds
        self.warp = warp
        self.argids = None  # assigned at serialization, per-target


def define(proccode, argnames=(), *body, warp=False):
    """proccode like 'fib %s' or 'draw %s %b'. argnames in order."""
    kinds = []
    i = 0
    while i < len(proccode):
        if proccode[i] == "%" and i + 1 < len(proccode) and proccode[i + 1] in "sbn":
            kinds.append(proccode[i + 1])
            i += 2
        else:
            i += 1
    assert len(kinds) == len(argnames), (proccode, argnames)
    ref = ProcRef(proccode, list(argnames), kinds, warp)
    return ("define", ref, list(body))


def call(proccode, *args):
    return Node("procedures_call", inputs={"__args__": ("args", list(args))},
                mutation={"proccode": proccode})


def arg(name):
    return Node("argument_reporter_string_number", fields={"VALUE": [name, None]})


def argb(name):
    return Node("argument_reporter_boolean", fields={"VALUE": [name, None]})


# ---------------------------------------------------------------------------
# Looks


def say(x): return Node("looks_say", inputs={"MESSAGE": _text(x)})
def think(x): return Node("looks_think", inputs={"MESSAGE": _text(x)})
def say_for(x, secs): return Node("looks_sayforsecs", inputs={"MESSAGE": _text(x), "SECS": _num(secs)})
def show(): return Node("looks_show")
def hide(): return Node("looks_hide")
def set_size(x): return Node("looks_setsizeto", inputs={"SIZE": _num(x)})
def change_size(x): return Node("looks_changesizeby", inputs={"CHANGE": _num(x)})
def size(): return Node("looks_size")
def next_costume(): return Node("looks_nextcostume")
def next_backdrop(): return Node("looks_nextbackdrop")


def switch_costume(c):
    return Node("looks_switchcostumeto", inputs={"COSTUME": _menu("looks_costume", "COSTUME", c)})


def switch_backdrop(c):
    return Node("looks_switchbackdropto", inputs={"BACKDROP": _menu("looks_backdrops", "BACKDROP", c)})


def costume_number(): return Node("looks_costumenumbername", fields={"NUMBER_NAME": ["number", None]})
def costume_name(): return Node("looks_costumenumbername", fields={"NUMBER_NAME": ["name", None]})
def backdrop_number(): return Node("looks_backdropnumbername", fields={"NUMBER_NAME": ["number", None]})
def backdrop_name(): return Node("looks_backdropnumbername", fields={"NUMBER_NAME": ["name", None]})


def set_effect(effect, x):
    return Node("looks_seteffectto", inputs={"VALUE": _num(x)}, fields={"EFFECT": [effect, None]})


def change_effect(effect, x):
    return Node("looks_changeeffectby", inputs={"CHANGE": _num(x)}, fields={"EFFECT": [effect, None]})


def clear_effects(): return Node("looks_cleargraphiceffects")


def go_front(): return Node("looks_gotofrontback", fields={"FRONT_BACK": ["front", None]})
def go_back(): return Node("looks_gotofrontback", fields={"FRONT_BACK": ["back", None]})


def go_layers(direction, n):
    return Node("looks_goforwardbackwardlayers", inputs={"NUM": _int(n)},
                fields={"FORWARD_BACKWARD": [direction, None]})


# ---------------------------------------------------------------------------
# Motion


def move(n): return Node("motion_movesteps", inputs={"STEPS": _num(n)})
def turn_right(n): return Node("motion_turnright", inputs={"DEGREES": _num(n)})
def turn_left(n): return Node("motion_turnleft", inputs={"DEGREES": _num(n)})
def goto_xy(x, y): return Node("motion_gotoxy", inputs={"X": _num(x), "Y": _num(y)})
def set_x(x): return Node("motion_setx", inputs={"X": _num(x)})
def set_y(y): return Node("motion_sety", inputs={"Y": _num(y)})
def change_x(x): return Node("motion_changexby", inputs={"DX": _num(x)})
def change_y(y): return Node("motion_changeyby", inputs={"DY": _num(y)})
def point_dir(d): return Node("motion_pointindirection", inputs={"DIRECTION": _angle(d)})
def glide_xy(secs, x, y): return Node("motion_glidesecstoxy", inputs={"SECS": _num(secs), "X": _num(x), "Y": _num(y)})
def bounce(): return Node("motion_ifonedgebounce")
def x_pos(): return Node("motion_xposition")
def y_pos(): return Node("motion_yposition")
def direction(): return Node("motion_direction")


def goto(target):
    return Node("motion_goto", inputs={"TO": _menu("motion_goto_menu", "TO", target)})


def point_towards(target):
    return Node("motion_pointtowards", inputs={"TOWARDS": _menu("motion_pointtowards_menu", "TOWARDS", target)})


def rotation_style(style):
    return Node("motion_setrotationstyle", fields={"STYLE": [style, None]})


# ---------------------------------------------------------------------------
# Sensing


def timer(): return Node("sensing_timer")
def reset_timer(): return Node("sensing_resettimer")
def answer(): return Node("sensing_answer")
def mouse_x(): return Node("sensing_mousex")
def mouse_y(): return Node("sensing_mousey")
def mouse_down(): return Node("sensing_mousedown")
def username(): return Node("sensing_username")


def distance_to(target):
    return Node("sensing_distanceto", inputs={"DISTANCETOMENU": _menu("sensing_distancetomenu", "DISTANCETOMENU", target)})


def of(prop, obj):
    return Node("sensing_of", inputs={"OBJECT": _menu("sensing_of_object_menu", "OBJECT", obj)},
                fields={"PROPERTY": [prop, None]})


def touching(obj):
    return Node("sensing_touchingobject",
                inputs={"TOUCHINGOBJECTMENU": _menu("sensing_touchingobjectmenu", "TOUCHINGOBJECTMENU", obj)})


def key_pressed(key):
    return Node("sensing_keypressed", inputs={"KEY_OPTION": _menu("sensing_keyoptions", "KEY_OPTION", key)})


def ask(q): return Node("sensing_askandwait", inputs={"QUESTION": _text(q)})


# ---------------------------------------------------------------------------
# Pen


def pen_clear(): return Node("pen_clear")
def pen_down(): return Node("pen_penDown")
def pen_up(): return Node("pen_penUp")
def pen_stamp(): return Node("pen_stamp")
def pen_size(x): return Node("pen_setPenSizeTo", inputs={"SIZE": _num(x)})
def pen_change_size(x): return Node("pen_changePenSizeBy", inputs={"SIZE": _num(x)})
def pen_color(hexcolor): return Node("pen_setPenColorToColor", inputs={"COLOR": _color(hexcolor)})


def pen_set_param(param, x):
    return Node("pen_setPenColorParamTo", inputs={"COLOR_PARAM": _menu("pen_menu_colorParam", "colorParam", param),
                                                  "VALUE": _num(x)})


def pen_change_param(param, x):
    return Node("pen_changePenColorParamBy", inputs={"COLOR_PARAM": _menu("pen_menu_colorParam", "colorParam", param),
                                                     "VALUE": _num(x)})


# ---------------------------------------------------------------------------
# Costumes


def rect_svg(w, h, fill="#ff8c1a"):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<rect x="0" y="0" width="{w}" height="{h}" fill="{fill}"/></svg>')


def circle_svg(d, fill="#4c97ff"):
    r = d / 2
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{d}" height="{d}" viewBox="0 0 {d} {d}">'
            f'<circle cx="{r}" cy="{r}" r="{r}" fill="{fill}"/></svg>')


class Costume:
    def __init__(self, name, svg, cx=None, cy=None):
        self.name = name
        self.svg = svg
        # parse width/height for default rotation centre
        import re
        m = re.search(r'width="([\d.]+)"\s+height="([\d.]+)"', svg)
        w, h = (float(m.group(1)), float(m.group(2))) if m else (0, 0)
        self.cx = w / 2 if cx is None else cx
        self.cy = h / 2 if cy is None else cy


# ---------------------------------------------------------------------------
# Targets and project


class Target:
    def __init__(self, project, name, is_stage=False, x=0, y=0, costumes=None, visible=True,
                 size=100, direction=90, rotation_style="all around"):
        self.project = project
        self.name = name
        self.is_stage = is_stage
        self.x, self.y = x, y
        self.visible = visible
        self.size = size
        self.direction = direction
        self.rotation_style = rotation_style
        self.vars = []
        self.lists = []
        self.scripts = []
        self.procs = []
        if costumes is None:
            if is_stage:
                costumes = [Costume("backdrop1", rect_svg(480, 360, "#ffffff"))]
            else:
                costumes = [Costume("costume1", rect_svg(40, 40))]
        self.costumes = costumes

    def var(self, name, value=0):
        v = Var(name, value, self.project.ids("v"))
        self.vars.append(v)
        return v

    def list(self, name, items=()):
        L = Var(name, list(items), self.project.ids("l"), is_list=True)
        self.lists.append(L)
        return L

    def script(self, hat, *body):
        self.scripts.append((hat, list(body)))

    def proc(self, definition):
        _, ref, body = definition
        self.procs.append((ref, body))
        return ref


class Project:
    def __init__(self):
        self.ids = _Ids()
        self.stage = Target(self, "Stage", is_stage=True)
        self.sprites = []
        self.broadcasts = {}  # name -> id
        self.extensions = set()

    def sprite(self, name, **kw):
        t = Target(self, name, **kw)
        self.sprites.append(t)
        return t

    # -- serialization ------------------------------------------------------

    def _bcast_id(self, name):
        if name not in self.broadcasts:
            self.broadcasts[name] = self.ids("m")
        return self.broadcasts[name]

    def to_json(self):
        assets = {}
        targets = []
        all_targets = [self.stage] + self.sprites
        for layer, t in enumerate(all_targets):
            targets.append(self._target_json(t, layer, assets))
        # broadcasts are collected during serialization; put them on the stage
        targets[0]["broadcasts"] = {bid: name for name, bid in self.broadcasts.items()}
        proj = {
            "targets": targets,
            "monitors": [],
            "extensions": sorted(self.extensions),
            "meta": {"semver": "3.0.0", "vm": "5.0.300", "agent": "claudes-llvm-scratch sb3.py"},
        }
        return proj, assets

    def save(self, path):
        proj, assets = self.to_json()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("project.json", json.dumps(proj))
            for name, data in assets.items():
                z.writestr(name, data)
        with open(path, "wb") as f:
            f.write(buf.getvalue())

    def _target_json(self, t, layer, assets):
        blocks = {}
        ser = _Serializer(self, t, blocks)
        # procedures first so calls can find argument ids
        for ref, _ in t.procs:
            ref.argids = [self.ids("a") for _ in ref.argnames]
        y = 0
        for ref, body in t.procs:
            ser.definition(ref, body, 0, y)
            y += 200
        for hat, body in t.scripts:
            ser.script(hat, body, 400, y)
            y += 200
        costumes = []
        for c in t.costumes:
            data = c.svg.encode()
            md5 = hashlib.md5(data).hexdigest()
            assets[f"{md5}.svg"] = data
            costumes.append({
                "name": c.name, "bitmapResolution": 1, "dataFormat": "svg",
                "assetId": md5, "md5ext": f"{md5}.svg",
                "rotationCenterX": c.cx, "rotationCenterY": c.cy,
            })
        j = {
            "isStage": t.is_stage,
            "name": t.name,
            "variables": {v.id: [v.name, v.value] for v in t.vars},
            "lists": {L.id: [L.name, L.value] for L in t.lists},
            "broadcasts": {},
            "blocks": blocks,
            "comments": {},
            "currentCostume": 0,
            "costumes": costumes,
            "sounds": [],
            "volume": 100,
            "layerOrder": layer,
        }
        if t.is_stage:
            j.update({"tempo": 60, "videoTransparency": 50, "videoState": "on", "textToSpeechLanguage": None})
        else:
            j.update({"visible": t.visible, "x": t.x, "y": t.y, "size": t.size, "direction": t.direction,
                      "draggable": False, "rotationStyle": t.rotation_style})
        return j


class _Serializer:
    def __init__(self, project, target, blocks):
        self.p = project
        self.t = target
        self.blocks = blocks

    def _procs(self):
        return {ref.proccode: ref for ref, _ in self.t.procs}

    def script(self, hat, body, x, y):
        hid = self.block(hat, None)
        b = self.blocks[hid]
        b["topLevel"] = True
        b["x"], b["y"] = x, y
        first = self.stack(body, hid)
        b["next"] = first

    def definition(self, ref, body, x, y):
        did = self.p.ids()
        pid = self.p.ids()
        arg_shadows = {}
        proto_inputs = {}
        for aid, name, kind in zip(ref.argids, ref.argnames, ref.argkinds):
            sid = self.p.ids()
            op = "argument_reporter_boolean" if kind == "b" else "argument_reporter_string_number"
            self.blocks[sid] = {"opcode": op, "next": None, "parent": pid, "inputs": {},
                                "fields": {"VALUE": [name, None]}, "shadow": True, "topLevel": False}
            proto_inputs[aid] = [1, sid]
            arg_shadows[aid] = sid
        defaults = ["false" if k == "b" else "" for k in ref.argkinds]
        self.blocks[pid] = {
            "opcode": "procedures_prototype", "next": None, "parent": did,
            "inputs": proto_inputs, "fields": {}, "shadow": True, "topLevel": False,
            "mutation": {"tagName": "mutation", "children": [], "proccode": ref.proccode,
                         "argumentids": json.dumps(ref.argids), "argumentnames": json.dumps(ref.argnames),
                         "argumentdefaults": json.dumps(defaults), "warp": "true" if ref.warp else "false"},
        }
        self.blocks[did] = {"opcode": "procedures_definition", "next": None, "parent": None,
                            "inputs": {"custom_block": [1, pid]}, "fields": {}, "shadow": False,
                            "topLevel": True, "x": x, "y": y}
        self.blocks[did]["next"] = self.stack(body, did)

    def stack(self, nodes, parent):
        first = None
        prev = None
        for n in nodes:
            bid = self.block(n, prev if prev else parent)
            if prev is None:
                first = bid
            else:
                self.blocks[prev]["next"] = bid
            prev = bid
        return first

    def block(self, n, parent, shadow=False):
        assert isinstance(n, Node), n
        bid = self.p.ids()
        b = {"opcode": n.opcode, "next": None, "parent": parent, "inputs": {}, "fields": {},
             "shadow": shadow or n.shadow, "topLevel": False}
        self.blocks[bid] = b
        if n.opcode.startswith("pen_"):
            self.p.extensions.add("pen")
        for fname, fval in n.fields.items():
            if isinstance(fval, tuple) and fval[0] == "var":
                v = fval[1]
                b["fields"][fname] = [v.name, v.id]
            elif isinstance(fval, tuple) and fval[0] == "list":
                v = fval[1]
                b["fields"][fname] = [v.name, v.id]
            elif isinstance(fval, tuple) and fval[0] == "broadcast":
                b["fields"][fname] = [fval[1], self.p._bcast_id(fval[1])]
            else:
                b["fields"][fname] = fval
        if n.opcode == "procedures_call":
            self._call(n, b, bid)
        else:
            for iname, ival in n.inputs.items():
                b["inputs"][iname] = self.input(ival, bid)
        if n.mutation is not None and n.opcode != "procedures_call":
            b["mutation"] = n.mutation
        return bid

    def _call(self, n, b, bid):
        proccode = n.mutation["proccode"]
        ref = self._procs().get(proccode)
        if ref is None:
            raise KeyError(f"call to undefined procedure {proccode!r} in {self.t.name}")
        args = n.inputs["__args__"][1]
        assert len(args) == len(ref.argids), (proccode, args)
        for aid, kind, a in zip(ref.argids, ref.argkinds, args):
            if kind == "b":
                b["inputs"][aid] = self.input(_bool(a), bid)
            else:
                b["inputs"][aid] = self.input(_text(a), bid)
        b["mutation"] = {"tagName": "mutation", "children": [], "proccode": proccode,
                         "argumentids": json.dumps(ref.argids), "warp": "true" if ref.warp else "false"}

    def input(self, ival, parent):
        kind = ival[0]
        if kind == "stack":
            first = self.stack(ival[1], parent)
            return [2, first]
        if kind == "bool":
            v = ival[1]
            if v is None:
                return [1, None]
            return [2, self.reporter(v, parent)]
        if kind == "prim":
            code, v = ival[1], ival[2]
            if isinstance(v, (Node, Var, tuple)):
                shadow = [code, "" if code == TEXT else "0"] if code != COLOR else [COLOR, "#000000"]
                return [3, self.reporter(v, parent), shadow]
            return [1, [code, _lit(v)]]
        if kind == "broadcast":
            v = ival[1]
            if isinstance(v, str):
                return [1, [BROADCAST, v, self.p._bcast_id(v)]]
            return [3, self.reporter(v, parent), [BROADCAST, "message1", self.p._bcast_id("message1")]]
        if kind == "menu":
            _, opcode, field, value = ival
            if isinstance(value, (Node, Var, tuple)):
                sid = self.p.ids()
                self.blocks[sid] = {"opcode": opcode, "next": None, "parent": parent, "inputs": {},
                                    "fields": {field: [_default_menu(opcode), None]}, "shadow": True,
                                    "topLevel": False}
                return [3, self.reporter(value, parent), sid]
            sid = self.p.ids()
            self.blocks[sid] = {"opcode": opcode, "next": None, "parent": parent, "inputs": {},
                                "fields": {field: [value, None]}, "shadow": True, "topLevel": False}
            return [1, sid]
        raise ValueError(ival)

    def reporter(self, v, parent):
        """Serialize a reporter. Vars/lists become compressed top-level primitive arrays."""
        if isinstance(v, Var):
            if v.is_list:
                return [LIST, v.name, v.id]
            return [VARIABLE, v.name, v.id]
        if isinstance(v, tuple) and v[0] == "listref":
            L = v[1]
            return [LIST, L.name, L.id]
        return self.block(v, parent)


def _lit(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v) if not isinstance(v, str) else v


def _default_menu(opcode):
    return {"looks_costume": "costume1", "looks_backdrops": "backdrop1", "motion_goto_menu": "_random_",
            "control_create_clone_of_menu": "_myself_"}.get(opcode, "")
