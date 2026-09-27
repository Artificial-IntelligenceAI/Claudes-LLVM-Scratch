"""
Load a .sb3 and lower its blocks into the AST.

Mirrors how scratch-vm interprets project.json:
  - inputs are [shadowType, value, shadow?]; the effective value is `value`
  - compressed primitives: [4..10, v], [11, name, id], [12, name, id], [13, name, id]
  - a block with no implementation, one field and no inputs is a literal
    (BlockCached._isShadowBlock)
  - variables resolve like Target.lookupOrCreateVariable: by id (own, then
    stage), then by name (own, then stage), else created on the target
  - procedures resolve like Blocks.getProcedureDefinition: first definition
    in block order
"""

import json
import zipfile

from .ast import (Lit, VarGet, ListContents, ArgGet, Op, If, Loop, Call,
                  Variable, Procedure, Script, Costume, Sprite, Program)

HATS = {
    "event_whenflagclicked": "FLAG",
    "event_whenbroadcastreceived": "BROADCAST",
    "control_start_as_clone": "CLONE",
    "event_whengreaterthan": "TIMER_GT",
    "event_whenkeypressed": "KEY",
    "event_whenthisspriteclicked": "CLICKED",
    "event_whenstageclicked": "STAGE_CLICKED",
    "event_whenbackdropswitchesto": "BACKDROP",
    "event_whentouchingobject": "TOUCHING",
}

# Opcodes the compiler implements (statements and reporters). Anything else
# with one field and no inputs is a menu literal; otherwise it is unsupported.
STATEMENTS = {
    "event_broadcast", "event_broadcastandwait",
    "control_wait", "control_wait_until", "control_stop", "control_create_clone_of",
    "control_delete_this_clone", "control_incr_counter", "control_clear_counter",
    "data_setvariableto", "data_changevariableby", "data_showvariable", "data_hidevariable",
    "data_addtolist", "data_deleteoflist", "data_deletealloflist", "data_insertatlist",
    "data_replaceitemoflist", "data_showlist", "data_hidelist",
    "looks_say", "looks_think", "looks_sayforsecs", "looks_thinkforsecs", "looks_show", "looks_hide",
    "looks_switchcostumeto", "looks_nextcostume", "looks_switchbackdropto", "looks_switchbackdroptoandwait",
    "looks_nextbackdrop", "looks_changeeffectby", "looks_seteffectto", "looks_cleargraphiceffects",
    "looks_changesizeby", "looks_setsizeto", "looks_gotofrontback", "looks_goforwardbackwardlayers",
    "looks_hideallsprites", "looks_changestretchby", "looks_setstretchto",
    "motion_movesteps", "motion_turnright", "motion_turnleft", "motion_goto", "motion_gotoxy",
    "motion_glideto", "motion_glidesecstoxy", "motion_pointindirection", "motion_pointtowards",
    "motion_changexby", "motion_setx", "motion_changeyby", "motion_sety", "motion_ifonedgebounce",
    "motion_setrotationstyle", "motion_align_scene", "motion_xscroll", "motion_yscroll",
    "sensing_askandwait", "sensing_resettimer", "sensing_setdragmode",
    "pen_clear", "pen_stamp", "pen_penDown", "pen_penUp", "pen_setPenColorToColor",
    "pen_changePenColorParamBy", "pen_setPenColorParamTo", "pen_changePenSizeBy", "pen_setPenSizeTo",
    "pen_setPenShadeToNumber", "pen_changePenShadeBy", "pen_setPenHueToNumber", "pen_changePenHueBy",
    "sound_play", "sound_playuntildone", "sound_stopallsounds", "sound_seteffectto",
    "sound_changeeffectby", "sound_cleareffects", "sound_changevolumeby", "sound_setvolumeto",
}

REPORTERS = {
    "operator_add", "operator_subtract", "operator_multiply", "operator_divide", "operator_lt",
    "operator_equals", "operator_gt", "operator_and", "operator_or", "operator_not", "operator_random",
    "operator_join", "operator_letter_of", "operator_length", "operator_contains", "operator_mod",
    "operator_round", "operator_mathop",
    "data_itemoflist", "data_itemnumoflist", "data_lengthoflist", "data_listcontainsitem",
    "motion_xposition", "motion_yposition", "motion_direction",
    "looks_size", "looks_costumenumbername", "looks_backdropnumbername",
    "sensing_touchingobject", "sensing_touchingcolor", "sensing_coloristouchingcolor",
    "sensing_distanceto", "sensing_answer", "sensing_keypressed", "sensing_mousedown",
    "sensing_mousex", "sensing_mousey", "sensing_loudness", "sensing_loud", "sensing_timer",
    "sensing_of", "sensing_current", "sensing_dayssince2000", "sensing_username",
    "control_get_counter", "sound_volume",
}

CONTROL = {"control_if", "control_if_else", "control_repeat", "control_forever",
           "control_repeat_until", "control_while", "control_for_each", "control_all_at_once",
           "procedures_call", "procedures_definition", "procedures_prototype",
           "argument_reporter_string_number", "argument_reporter_boolean",
           "data_variable", "data_listcontents"}

KNOWN = STATEMENTS | REPORTERS | CONTROL | set(HATS)

# inputs that are boolean slots (empty -> false)
BOOL_INPUTS = {"CONDITION", "OPERAND"}
BOOL_OPS = {"operator_and", "operator_or", "operator_not"}


def _num(v):
    """JSON numbers become Python floats (JS numbers); strings stay strings."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    return v


class Loader:
    def __init__(self, data):
        self.zf = zipfile.ZipFile(data) if not isinstance(data, dict) else None
        if isinstance(data, dict):
            self.project = data
        else:
            self.project = json.loads(self.zf.read("project.json"))
        self.prog = Program()
        self.unsupported = {}

    def warn(self, msg):
        if msg not in self.prog.warnings:
            self.prog.warnings.append(msg)

    # -- top level ------------------------------------------------------------

    def load(self):
        targets = self.project["targets"]
        self.prog.extensions = self.project.get("extensions", [])
        stage_json = next(t for t in targets if t.get("isStage"))
        ordered = [stage_json] + [t for t in targets if not t.get("isStage")]
        self.sprites = []
        for tj in ordered:
            s = self._sprite(tj)
            self.sprites.append((s, tj))
            if s.is_stage:
                self.prog.stage = s
        self.prog.sprites = [s for s, _ in self.sprites]
        # procedures must exist before any block is lowered
        for s, tj in self.sprites:
            self._collect_procs(s, tj)
        for s, tj in self.sprites:
            self._lower_target(s, tj)
        # number procedures and scripts
        for s in self.prog.sprites:
            for p in s.procs:
                p.id = len(self.prog.procs)
                self.prog.procs.append(p)
            for sc in s.scripts:
                sc.index = len(self.prog.scripts)
                self.prog.scripts.append(sc)
        for op, n in sorted(self.unsupported.items()):
            self.warn(f"unsupported block {op} ({n} use{'s' if n != 1 else ''}) compiled as a no-op")
        return self.prog

    def _sprite(self, tj):
        s = Sprite(tj["name"], bool(tj.get("isStage")))
        for vid, arr in tj.get("variables", {}).items():
            s.vars.append(Variable(arr[0], _num(arr[1]), s, len(s.vars), vid=vid))
        for lid, arr in tj.get("lists", {}).items():
            s.lists.append(Variable(arr[0], [_num(x) for x in arr[1]], s, len(s.lists), is_list=True, vid=lid))
        for c in tj.get("costumes", []):
            md5ext = c.get("md5ext") or f"{c.get('assetId')}.{c.get('dataFormat')}"
            asset = None
            if self.zf is not None:
                for name in (md5ext, f"{c.get('assetId')}.{c.get('dataFormat')}"):
                    try:
                        asset = self.zf.read(name)
                        break
                    except KeyError:
                        pass
            s.costumes.append(Costume(c.get("name", ""), c.get("dataFormat", "svg"), asset,
                                      float(c.get("rotationCenterX", 0) or 0),
                                      float(c.get("rotationCenterY", 0) or 0),
                                      int(c.get("bitmapResolution", 1) or 1), md5ext))
        s.current_costume = int(tj.get("currentCostume", 0) or 0)
        s.layer = int(tj.get("layerOrder", 0) or 0)
        s.volume = float(tj.get("volume", 100) or 0)
        if not s.is_stage:
            s.x = float(tj.get("x", 0) or 0)
            s.y = float(tj.get("y", 0) or 0)
            s.size = float(tj.get("size", 100) if tj.get("size") is not None else 100)
            s.direction = float(tj.get("direction", 90) if tj.get("direction") is not None else 90)
            s.visible = bool(tj.get("visible", True))
            s.rotation_style = {"all around": 0, "left-right": 1, "don't rotate": 2}.get(
                tj.get("rotationStyle", "all around"), 0)
        return s

    # -- variables ------------------------------------------------------------

    def resolve(self, sprite, vid, name, is_list):
        stage = self.prog.stage
        own = sprite.lists if is_list else sprite.vars
        for v in own:
            if v.id == vid:
                return v
        if not sprite.is_stage:
            for v in (stage.lists if is_list else stage.vars):
                if v.id == vid:
                    return v
        for v in own:
            if v.name == name:
                return v
        if not sprite.is_stage:
            for v in (stage.lists if is_list else stage.vars):
                if v.name == name:
                    return v
        v = Variable(name, [] if is_list else 0.0, sprite, len(own), is_list=is_list, vid=vid)
        own.append(v)
        self.warn(f"{'list' if is_list else 'variable'} {name!r} referenced by {sprite.name} "
                  f"does not exist; created on {sprite.name}")
        return v

    # -- procedures -------------------------------------------------------------

    def _collect_procs(self, s, tj):
        blocks = tj.get("blocks", {})
        protos = {}
        for bid, b in blocks.items():
            if isinstance(b, dict) and b.get("opcode") == "procedures_prototype":
                m = b.get("mutation", {})
                code = m.get("proccode")
                if code is not None and code not in protos:
                    protos[code] = m
        for bid, b in blocks.items():
            if not isinstance(b, dict) or b.get("opcode") != "procedures_definition":
                continue
            inp = b.get("inputs", {}).get("custom_block")
            if not inp or not isinstance(inp[1], str) or inp[1] not in blocks:
                continue
            proto = blocks[inp[1]]
            m = proto.get("mutation", {})
            code = m.get("proccode")
            if code is None or code in s.proc_by_code:
                continue
            pm = protos.get(code, m)
            names = json.loads(pm.get("argumentnames", "[]") or "[]")
            ids = json.loads(pm.get("argumentids", "[]") or "[]")
            defaults = json.loads(pm.get("argumentdefaults", "[]") or "[]")
            warp = m.get("warp", False)
            if isinstance(warp, str):
                warp = json.loads(warp)
            kinds = []
            i = 0
            while i < len(code):
                if code[i] == "%" and i + 1 < len(code) and code[i + 1] in "snb":
                    kinds.append("b" if code[i + 1] == "b" else "s")
                    i += 2
                else:
                    i += 1
            while len(kinds) < len(ids):
                kinds.append("s")
            while len(defaults) < len(ids):
                defaults.append("")
            p = Procedure(s, code, names, ids, defaults, bool(warp), kinds[:len(ids)])
            p._def_id = bid
            s.procs.append(p)
            s.proc_by_code[code] = p

    # -- lowering ---------------------------------------------------------------

    def _lower_target(self, s, tj):
        self.blocks = tj.get("blocks", {})
        self.sprite = s
        for p in s.procs:
            self.proc = p
            first = self.blocks[p._def_id].get("next")
            p.body = self.stack(first)
        self.proc = None
        for bid, b in self.blocks.items():
            if not isinstance(b, dict) or not b.get("topLevel"):
                continue
            op = b.get("opcode")
            if op not in HATS:
                if op not in ("procedures_definition",) and op in KNOWN | {"procedures_prototype"}:
                    pass  # loose stacks never run
                elif op not in KNOWN:
                    if op and not op.startswith("procedures"):
                        self.warn(f"hat {op} is not supported; its script never runs")
                continue
            hat = HATS[op]
            match = None
            hat_args = {}
            f = b.get("fields", {})
            if hat == "BROADCAST":
                match = str(f.get("BROADCAST_OPTION", [""])[0]).lower()
            elif hat == "KEY":
                match = str(f.get("KEY_OPTION", [""])[0]).lower()
            elif hat == "BACKDROP":
                match = str(f.get("BACKDROP", [""])[0]).lower()
            elif hat == "TIMER_GT":
                hat_args["menu"] = str(f.get("WHENGREATERTHANMENU", ["TIMER"])[0]).lower()
                hat_args["VALUE"] = self.input(b, "VALUE")
            elif hat == "TOUCHING":
                hat_args["TOUCHINGOBJECTMENU"] = self.input(b, "TOUCHINGOBJECTMENU")
            body = self.stack(b.get("next"))
            s.scripts.append(Script(s, hat, match, body, hat_args))

    def stack(self, bid):
        out = []
        seen = set()
        while bid:
            if bid in seen:
                break
            seen.add(bid)
            b = self.blocks.get(bid)
            if not isinstance(b, dict):
                break
            st = self.statement(b)
            if st is not None:
                out.append(st)
            bid = b.get("next")
        return out

    def substack(self, b, name):
        inp = b.get("inputs", {}).get(name)
        if not inp or not isinstance(inp[1], str):
            return []
        return self.stack(inp[1])

    def field(self, b, name, default=None):
        f = b.get("fields", {}).get(name)
        if f is None:
            return default
        return f[0]

    def var_field(self, b, name, is_list):
        f = b.get("fields", {}).get(name)
        if f is None:
            return self.resolve(self.sprite, None, "", is_list)
        return self.resolve(self.sprite, f[1] if len(f) > 1 else None, f[0], is_list)

    def statement(self, b):
        op = b.get("opcode")
        if op == "control_if":
            return If(self.bool_input(b, "CONDITION"), self.substack(b, "SUBSTACK"), [])
        if op == "control_if_else":
            return If(self.bool_input(b, "CONDITION"), self.substack(b, "SUBSTACK"), self.substack(b, "SUBSTACK2"))
        if op == "control_all_at_once":
            return If(Lit(True), self.substack(b, "SUBSTACK"), [])
        if op == "control_repeat":
            return Loop("repeat", self.input(b, "TIMES"), self.substack(b, "SUBSTACK"))
        if op == "control_forever":
            return Loop("forever", None, self.substack(b, "SUBSTACK"))
        if op == "control_repeat_until":
            return Loop("until", self.bool_input(b, "CONDITION"), self.substack(b, "SUBSTACK"))
        if op == "control_while":
            return Loop("while", self.bool_input(b, "CONDITION"), self.substack(b, "SUBSTACK"))
        if op == "control_for_each":
            return Loop("for_each", self.input(b, "VALUE"), self.substack(b, "SUBSTACK"),
                        var=self.var_field(b, "VARIABLE", False))
        if op == "procedures_call":
            return self.call(b)
        if op in HATS or op in ("procedures_definition", "procedures_prototype"):
            return None
        if op in STATEMENTS:
            return self.op(b)
        if op in REPORTERS or op in CONTROL:
            return None  # a reporter used as a statement does nothing observable
        self.unsupported[op] = self.unsupported.get(op, 0) + 1
        return None

    def call(self, b):
        m = b.get("mutation", {})
        code = m.get("proccode")
        p = self.sprite.proc_by_code.get(code)
        if p is None:
            return None  # scratch-vm: undefined procedure is a no-op
        args = []
        inputs = b.get("inputs", {})
        order = [p.argids.index(k) for k in inputs if k in p.argids]
        order += [i for i in range(len(p.argids)) if i not in order]
        for aid, kind, default in zip(p.argids, p.kinds, p.argdefaults):
            inp = inputs.get(aid)
            node = None
            if inp is not None and inp[1] is not None:
                node = self.value(inp[1])
            if node is None:
                node = Lit(_num(default) if not isinstance(default, str) else default)
            args.append(node)
        return Call(p, args, order)

    def op(self, b):
        op = b.get("opcode")
        args = {}
        for name in b.get("inputs", {}):
            if name in ("SUBSTACK", "SUBSTACK2", "custom_block"):
                continue
            if name in BOOL_INPUTS or (op in BOOL_OPS and name.startswith("OPERAND")):
                args[name] = self.bool_input(b, name)
            else:
                args[name] = self.input(b, name)
        fields = {k: v[0] for k, v in b.get("fields", {}).items()}
        ref = None
        if "VARIABLE" in b.get("fields", {}):
            ref = self.var_field(b, "VARIABLE", False)
        elif "LIST" in b.get("fields", {}):
            ref = self.var_field(b, "LIST", True)
        # broadcast inputs: a literal [11, name, id] or a reporter
        if "BROADCAST_INPUT" in b.get("inputs", {}):
            inp = b["inputs"]["BROADCAST_INPUT"]
            if isinstance(inp[1], list) and inp[1] and inp[1][0] == 11:
                args["BROADCAST_INPUT"] = Lit(str(inp[1][1]))
        return Op(op, args, fields, ref)

    def input(self, b, name):
        inp = b.get("inputs", {}).get(name)
        if inp is None or len(inp) < 2 or inp[1] is None:
            if inp is not None and len(inp) >= 3 and inp[2] is not None:
                return self.value(inp[2])
            return Lit("")
        return self.value(inp[1])

    def bool_input(self, b, name):
        inp = b.get("inputs", {}).get(name)
        if inp is None or len(inp) < 2 or inp[1] is None:
            return Lit(False)
        return self.value(inp[1])

    def value(self, x):
        if isinstance(x, list):
            kind = x[0]
            if kind in (4, 5, 6, 7, 8, 9, 10):
                return Lit(_num(x[1]))
            if kind == 11:
                return Lit(str(x[1]))
            if kind == 12:
                return VarGet(self.resolve(self.sprite, x[2] if len(x) > 2 else None, x[1], False))
            if kind == 13:
                return ListContents(self.resolve(self.sprite, x[2] if len(x) > 2 else None, x[1], True))
            return Lit("")
        b = self.blocks.get(x)
        if not isinstance(b, dict):
            return Lit("")
        return self.expr(b)

    def expr(self, b):
        op = b.get("opcode")
        if op == "data_variable":
            return VarGet(self.var_field(b, "VARIABLE", False))
        if op == "data_listcontents":
            return ListContents(self.var_field(b, "LIST", True))
        if op in ("argument_reporter_string_number", "argument_reporter_boolean"):
            name = self.field(b, "VALUE", "")
            idx = None
            if self.proc is not None:
                for i, n in enumerate(self.proc.argnames):
                    if n == name:
                        idx = i  # last one wins (pushParam overwrites)
            if idx is None:
                return Lit(0.0)
            return ArgGet(idx, op == "argument_reporter_boolean")
        if op in REPORTERS:
            return self.op(b)
        fields = b.get("fields", {})
        if len(fields) == 1 and not b.get("inputs"):
            v = list(fields.values())[0][0]
            return Lit(_num(v))
        self.unsupported[op] = self.unsupported.get(op, 0) + 1
        return Lit("")


def load(path_or_file):
    return Loader(path_or_file).load()
