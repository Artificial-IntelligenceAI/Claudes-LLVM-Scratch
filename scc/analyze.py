# SPDX-License-Identifier: AGPL-3.0-only
"""
Program analyses:

  * Type inference over the lattice  bottom < {num, str, bool} < any.
    Variables, list items and procedure parameters get the join of every
    value that can be stored into them, iterated to a fixpoint. Types are
    *exact*: a variable is `num` only if it can never hold a string, because
    scratch-vm distinguishes the string "1" from the number 1 (list joining,
    costume lookup by name, ...). Literal inputs are strings in project.json,
    so `set x to 1` makes x hold the *string* "1".

  * Warp atomicity: a procedure is atomic if, when run in warp mode ("run
    without screen refresh"), it can never yield. Atomic procedures get a
    direct native implementation (plain LLVM function, loop counters in
    registers) in addition to the resumable state-machine version.
"""

from .ast import Lit, VarGet, ListContents, ArgGet, Op, If, Loop, Call

NUM, STR, BOOL, ANY = "num", "str", "bool", "any"


def join(a, b):
    if a is None:
        return b
    if b is None or a == b:
        return a
    return ANY


def lit_type(v):
    if isinstance(v, bool):
        return BOOL
    if isinstance(v, float):
        return NUM
    return STR


NUM_OPS = {
    "operator_add", "operator_subtract", "operator_multiply", "operator_divide", "operator_mod",
    "operator_round", "operator_mathop", "operator_random", "operator_length",
    "data_itemnumoflist", "data_lengthoflist", "motion_xposition", "motion_yposition",
    "motion_direction", "looks_size", "sensing_distanceto", "sensing_loudness", "sensing_timer",
    "sensing_current", "sensing_dayssince2000",
    "control_get_counter", "sound_volume", "music_getTempo",
}
STR_OPS = {"operator_join", "operator_letter_of", "sensing_answer", "sensing_username",
           "sensing_mousex", "sensing_mousey"}
BOOL_OPS = {
    "operator_lt", "operator_gt", "operator_equals", "operator_and", "operator_or", "operator_not",
    "operator_contains", "data_listcontainsitem", "sensing_touchingobject", "sensing_touchingcolor",
    "sensing_coloristouchingcolor", "sensing_keypressed", "sensing_mousedown", "sensing_loud",
}

# statements that can yield even in warp mode (they wait on time, other threads, or input)
YIELDING = {
    "control_wait", "control_wait_until", "motion_glideto", "motion_glidesecstoxy",
    "looks_sayforsecs", "looks_thinkforsecs", "sensing_askandwait", "event_broadcastandwait",
    "looks_switchbackdroptoandwait", "music_playDrumForBeats", "music_midiPlayDrumForBeats",
    "music_restForBeats", "music_playNoteForBeats", "sound_setvolumeto", "sound_changevolumeby",
}

# reporters with side effects visible to later code (random number consumption)
IMPURE_REPORTERS = {"operator_random"}


# Blocks whose behaviour depends on whether an input is a string or a number
# (beyond Cast.toNumber/toString): costume/backdrop lookup by name vs index.
IDENTITY_SINKS = {
    "looks_switchcostumeto": "COSTUME",
    "looks_switchbackdropto": "BACKDROP",
    "looks_switchbackdroptoandwait": "BACKDROP",
}
LIST_WRITES = {"data_addtolist": "ITEM", "data_insertatlist": "ITEM", "data_replaceitemoflist": "ITEM"}


def integer_string(v):
    """An integer literal string whose String(Number(v)) === v, e.g. "0", "-12".
    Such a string is indistinguishable from the number except by typeof."""
    if not isinstance(v, str) or not v:
        return False
    body = v[1:] if v[0] == "-" else v
    if not body.isdigit() or not body.isascii() or (len(body) > 1 and body[0] == "0") or v == "-0":
        return False
    return abs(int(v)) < 2 ** 53


class Analyzer:
    def __init__(self, prog):
        self.prog = prog
        self.changed = False

    # -- string identity ---------------------------------------------------------------
    #
    # A value's *identity* (string "5" vs number 5) is observable only by:
    #   - list contents joining (single-letter strings join without spaces),
    #   - costume/backdrop switching (name lookup vs index).
    # Values flow unchanged through variables, parameters and list items. A
    # store target is "sensitive" if identity-preserving reads of it can reach
    # one of those observers. Integer literal strings stored into insensitive
    # targets are replaced by numbers, which lets the type inference prove
    # loop counters, indices and accumulators numeric.

    def _sources(self, e, proc):
        """Identity-preserving sources of an expression's value."""
        if isinstance(e, VarGet):
            return [("var", e.var)]
        if isinstance(e, ArgGet):
            return [("param", (proc, e.index))]
        if isinstance(e, Op) and e.opcode == "data_itemoflist":
            return [("list", e.ref)]
        if isinstance(e, Op) and e.opcode == "sensing_of":
            name = str(e.fields.get("PROPERTY", ""))
            return [("var", v) for s in self.prog.sprites if not s.is_stage for v in s.vars if v.name == name]
        return []

    def _mark(self, e, proc):
        for kind, x in self._sources(e, proc):
            key = (kind, id(x) if kind != "param" else (id(x[0]), x[1]))
            if key not in self.sensitive:
                self.sensitive.add(key)
                self.changed = True

    def _is_sens(self, kind, x):
        return (kind, id(x) if kind != "param" else (id(x[0]), x[1])) in self.sensitive

    def _walk_exprs(self, e, fn):
        fn(e)
        if isinstance(e, Op):
            for a in e.args.values():
                self._walk_exprs(a, fn)

    def _sens_stmts(self, stmts, proc):
        for s in stmts:
            if isinstance(s, If):
                self._sens_expr(s.cond, proc)
                self._sens_stmts(s.then, proc)
                self._sens_stmts(s.else_, proc)
            elif isinstance(s, Loop):
                if s.arg is not None:
                    self._sens_expr(s.arg, proc)
                self._sens_stmts(s.body, proc)
            elif isinstance(s, Call):
                for i, a in enumerate(s.args):
                    self._sens_expr(a, proc)
                    if self._is_sens("param", (s.proc, i)):
                        self._mark(a, proc)
            elif isinstance(s, Op):
                for a in s.args.values():
                    self._sens_expr(a, proc)
                op = s.opcode
                if op in IDENTITY_SINKS and IDENTITY_SINKS[op] in s.args:
                    self._mark(s.args[IDENTITY_SINKS[op]], proc)
                elif op == "data_setvariableto" and self._is_sens("var", s.ref):
                    self._mark(s.args.get("VALUE", Lit("")), proc)
                elif op in LIST_WRITES and self._is_sens("list", s.ref):
                    self._mark(s.args.get("ITEM", Lit("")), proc)

    def _sens_expr(self, e, proc):
        def visit(x):
            if isinstance(x, ListContents):
                key = ("list", id(x.lst))
                if key not in self.sensitive:
                    self.sensitive.add(key)
                    self.changed = True
        self._walk_exprs(e, visit)

    def _numify_stmts(self, stmts, proc):
        def num(e):
            if isinstance(e, Lit) and integer_string(e.value):
                return Lit(float(e.value))
            return e
        for s in stmts:
            if isinstance(s, If):
                self._numify_stmts(s.then, proc)
                self._numify_stmts(s.else_, proc)
            elif isinstance(s, Loop):
                self._numify_stmts(s.body, proc)
            elif isinstance(s, Call):
                for i in range(len(s.args)):
                    if not self._is_sens("param", (s.proc, i)):
                        s.args[i] = num(s.args[i])
            elif isinstance(s, Op):
                if s.opcode == "data_setvariableto" and not self._is_sens("var", s.ref) and "VALUE" in s.args:
                    s.args["VALUE"] = num(s.args["VALUE"])
                elif s.opcode in LIST_WRITES and not self._is_sens("list", s.ref) and "ITEM" in s.args:
                    s.args["ITEM"] = num(s.args["ITEM"])

    def numify(self):
        self.sensitive = set()
        bodies = [(p.body, p) for p in self.prog.procs] + [(sc.body, None) for sc in self.prog.scripts]
        while True:
            self.changed = False
            for body, proc in bodies:
                self._sens_stmts(body, proc)
            if not self.changed:
                break
        for body, proc in bodies:
            self._numify_stmts(body, proc)
        for sp in self.prog.sprites:
            for v in sp.vars:
                if not self._is_sens("var", v) and integer_string(v.value):
                    v.value = float(v.value)
            for L in sp.lists:
                if not self._is_sens("list", L):
                    L.value = [float(x) if integer_string(x) else x for x in L.value]
        self.stats = {"sensitive": len(self.sensitive)}

    # -- types --------------------------------------------------------------------

    def expr_type(self, e, proc):
        if isinstance(e, Lit):
            return lit_type(e.value)
        if isinstance(e, VarGet):
            return e.var.type
        if isinstance(e, ListContents):
            return STR
        if isinstance(e, ArgGet):
            return proc.param_types[e.index] if proc is not None else NUM
        if isinstance(e, Op):
            op = e.opcode
            if op in NUM_OPS:
                return NUM
            if op in STR_OPS:
                return STR
            if op in BOOL_OPS:
                return BOOL
            if op == "data_itemoflist":
                return join(e.ref.type, STR)  # invalid index reports ""
            if op in ("looks_costumenumbername", "looks_backdropnumbername"):
                return NUM if str(e.fields.get("NUMBER_NAME", "number")).lower() == "number" else STR
            if op == "sensing_of":
                return ANY
            return ANY
        raise TypeError(e)

    def _store(self, var, t):
        new = join(var.type, t)
        if new != var.type:
            var.type = new
            self.changed = True

    def _param(self, proc, i, t):
        new = join(proc.param_types[i], t)
        if new != proc.param_types[i]:
            proc.param_types[i] = new
            self.changed = True

    def _walk_exprs_for_types(self, e, proc):
        # nothing to record: expressions do not store; kept for symmetry
        pass

    def stmt_types(self, stmts, proc):
        for s in stmts:
            if isinstance(s, If):
                self.stmt_types(s.then, proc)
                self.stmt_types(s.else_, proc)
            elif isinstance(s, Loop):
                if s.kind == "for_each":
                    self._store(s.var, NUM)
                self.stmt_types(s.body, proc)
            elif isinstance(s, Call):
                for i, a in enumerate(s.args):
                    self._param(s.proc, i, self.expr_type(a, proc))
            elif isinstance(s, Op):
                op = s.opcode
                if op == "data_setvariableto":
                    self._store(s.ref, self.expr_type(s.args.get("VALUE", Lit("")), proc))
                elif op == "data_changevariableby":
                    self._store(s.ref, NUM)
                elif op in ("data_addtolist", "data_insertatlist", "data_replaceitemoflist"):
                    self._store(s.ref, self.expr_type(s.args.get("ITEM", Lit("")), proc))

    def infer_types(self):
        for s in self.prog.sprites:
            for v in s.vars:
                v.type = lit_type(v.value) if not isinstance(v.value, (list, dict)) else STR
            for L in s.lists:
                t = None
                for item in L.value:
                    t = join(t, lit_type(item))
                L.type = t
            for p in s.procs:
                p.param_types = [None] * len(p.argids)
        while True:
            self.changed = False
            for s in self.prog.sprites:
                for p in s.procs:
                    self.stmt_types(p.body, p)
                for sc in s.scripts:
                    self.stmt_types(sc.body, None)
            if not self.changed:
                break
        # never-written params/list items default to something concrete
        for s in self.prog.sprites:
            for p in s.procs:
                p.param_types = [t if t is not None else STR for t in p.param_types]
            for L in s.lists:
                if L.type is None:
                    L.type = ANY

    # -- atomicity ------------------------------------------------------------------

    def _has_yield(self, stmts):
        for s in stmts:
            if isinstance(s, Op) and s.opcode in ("sound_seteffectto", "sound_changeeffectby") and \
                    str(s.fields.get("EFFECT", "")).lower() in ("pitch", "pan"):
                return True
            if isinstance(s, If):
                if self._has_yield(s.then) or self._has_yield(s.else_):
                    return True
            elif isinstance(s, Loop):
                if self._has_yield(s.body):
                    return True
            elif isinstance(s, Call):
                if s.proc.atomic is False:
                    return True
            elif isinstance(s, Op):
                if s.opcode in YIELDING:
                    return True
        return False

    def infer_atomic(self):
        for p in self.prog.procs:
            p.atomic = True
        while True:
            changed = False
            for p in self.prog.procs:
                if p.atomic and self._has_yield(p.body):
                    p.atomic = False
                    changed = True
            if not changed:
                break

    def run(self):
        self.numify()
        self.infer_types()
        self.infer_atomic()
        return self.prog


def is_pure(e):
    """True if evaluating e has no side effects (so it may be evaluated once)."""
    if isinstance(e, (Lit, VarGet, ListContents, ArgGet)):
        return True
    if isinstance(e, Op):
        if e.opcode in IMPURE_REPORTERS:
            return False
        if e.opcode == "data_itemoflist":
            idx = e.args.get("INDEX")
            if not (isinstance(idx, Lit) and isinstance(idx.value, float)):
                return False  # "random"/"any" index consumes a random number
        if e.opcode in ("sensing_of",):
            pass
        return all(is_pure(a) for a in e.args.values())
    return True


def analyze(prog):
    return Analyzer(prog).run()
