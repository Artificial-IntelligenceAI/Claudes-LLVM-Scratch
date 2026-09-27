"""
LLVM IR generation.

Every hat script and procedure compiles to a *resumable* function

    i32 @R_x(ptr %th, ptr %fr)

a state machine over a heap Frame: the entry block switches on fr->state to
the resume point, the body yields by storing the next state and returning
R_YIELD (etc.), exactly where scratch-vm's sequencer would yield.

Procedures that can never yield in warp mode additionally get a *direct*
variant

    void @D_x(ptr %th, ptr %target, <typed params>)

with parameters in registers (double for numbers) and loop counters in
allocas that LLVM promotes to registers. This is where compute-heavy
"run without screen refresh" code ends up.

Values are typed num (double), bool (i1), str (ptr to Str) or any (i64 tag,
i64 bits). Strings are reference counted; each value carries a compile-time
`owned` flag so temporaries are released exactly once.
"""

import struct

from .ast import Lit, VarGet, ListContents, ArgGet, Op, If, Loop, Call
from .analyze import NUM, STR, BOOL, ANY, is_pure

R_RETURN, R_CALL, R_CALL_YIELD, R_YIELD, R_YIELD_TICK, R_PROMISE, R_STOP = range(7)
T_NUM, T_STR, T_BOOL = 0, 1, 2
HAT_IDS = {"FLAG": 1, "BROADCAST": 2, "CLONE": 3, "TIMER_GT": 4, "KEY": 5, "CLICKED": 6,
           "BACKDROP": 7, "STAGE_CLICKED": 8, "TOUCHING": 9}

MATHOPS = {"abs": 0, "floor": 1, "ceiling": 2, "sqrt": 3, "sin": 4, "cos": 5, "tan": 6, "asin": 7,
           "acos": 8, "atan": 9, "ln": 10, "log": 11, "e ^": 12, "10 ^": 13}


def hexd(x):
    return "0x%016X" % struct.unpack("<Q", struct.pack("<d", x))[0]


def llvm_str_bytes(b):
    out = []
    for c in b:
        if 32 <= c < 127 and c not in (34, 92):
            out.append(chr(c))
        else:
            out.append("\\%02X" % c)
    return "".join(out)


def js_number_literal(s):
    """JS Number(s) for a literal string, or None if NaN. Only used to decide
    whether a literal may be compared numerically; conservative on purpose."""
    t = s.strip(" \t\n\r\v\f")
    if t != s or not t:
        return None
    import re
    if re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", t):
        return float(t)
    return None


class V:
    """A compiled value. kind in num/bool/str/any. For any, a=tag, b=bits."""
    __slots__ = ("kind", "a", "b", "owned")

    def __init__(self, kind, a, b=None, owned=False):
        self.kind, self.a, self.b, self.owned = kind, a, b, owned


class Fn:
    def __init__(self, name, ret, params, attrs=""):
        self.name, self.ret, self.params, self.attrs = name, ret, params, attrs
        self.blocks = []
        self.n = 0
        self.allocas = []
        self.entry_tail = None  # terminator appended to the entry block at render time

    def label(self, hint="b"):
        self.n += 1
        return f"{hint}{self.n}"

    def tmp(self):
        self.n += 1
        return f"%t{self.n}"

    def block(self, label):
        self.blocks.append([label, []])

    @property
    def cur(self):
        return self.blocks[-1][1]

    def emit(self, s):
        self.cur.append(s)

    def terminated(self):
        c = self.cur
        return bool(c) and c[-1].split(None, 1)[0] in ("br", "ret", "switch", "unreachable")

    def render(self):
        ps = ", ".join(self.params)
        out = [f"define internal {self.ret} @{self.name}({ps}) {self.attrs} {{"]
        for i, (label, lines) in enumerate(self.blocks):
            out.append(f"{label}:")
            if i == 0:
                out.extend("  " + a for a in self.allocas)
            out.extend("  " + l for l in lines)
            if i == 0 and self.entry_tail:
                out.append("  " + self.entry_tail)
        out.append("}")
        return "\n".join(out)


class Ctx:
    def __init__(self, fn, mode, proc=None):
        self.fn = fn
        self.mode = mode          # 'R' resumable, 'D' direct, 'P' predicate
        self.proc = proc
        self.th = "%th"
        self.fr = "%fr"
        self.tgt = None
        self.vars = self.lists = self.gvars = self.glists = None
        self.dparams = None       # D: list of V (borrowed)
        self.depth = 0
        self.resumes = []


class CodeGen:
    def __init__(self, prog, abi):
        self.prog = prog
        self.abi = abi
        self.off = abi["offsets"]
        self.sigs = abi["signatures"]
        self.strings = {}
        self.globals = []
        self.fns = []
        self.used_decls = set()

    # -- module-level helpers -------------------------------------------------------

    def str_lit(self, s):
        if s in self.strings:
            return self.strings[s]
        name = f"@.s{len(self.strings)}"
        b = s.encode("utf-8")
        u16 = len(s.encode("utf-16-le")) // 2
        ascii_ = 1 if all(c < 128 for c in b) else 0
        n = len(b) + 1
        self.globals.append(
            f"{name} = internal global {{ i32, i32, i32, i8, i8, i16, double, [{n} x i8] }} "
            f"{{ i32 -1, i32 {len(b)}, i32 {u16}, i8 {ascii_}, i8 0, i16 0, double 0.0, "
            f"[{n} x i8] c\"{llvm_str_bytes(b)}\\00\" }}, align 8")
        self.strings[s] = name
        return name

    def call(self, ctx, name, args, want=None):
        """Call a runtime function; args are (type, value) pairs. Returns the result
        ssa (or (tag, bits) for a { i64, i64 } result)."""
        sig = self.sigs.get(name)
        if sig is None:
            raise KeyError(f"runtime function {name} not found")
        self.used_decls.add(name)
        types = sig["params"]
        if len(types) != len(args):
            raise TypeError(f"{name}: expected {len(types)} args, got {len(args)}")
        for (t, _), et in zip(args, types):
            if t != et:
                raise TypeError(f"{name}: arg type {t} != {et}")
        argstr = ", ".join(f"{t} {v}" for t, v in args)
        ret = sig["ret"]
        fn = ctx.fn
        if ret == "void":
            fn.emit(f"call void @{name}({argstr})")
            return None
        r = fn.tmp()
        fn.emit(f"{r} = call {ret} @{name}({argstr})")
        if ret == "{ i64, i64 }":
            a, b = fn.tmp(), fn.tmp()
            fn.emit(f"{a} = extractvalue {{ i64, i64 }} {r}, 0")
            fn.emit(f"{b} = extractvalue {{ i64, i64 }} {r}, 1")
            return (a, b)
        if ret == "i1" and want == "i32":
            z = fn.tmp()
            fn.emit(f"{z} = zext i1 {r} to i32")
            return z
        return r

    def gep(self, ctx, base, off):
        if off == 0:
            return base
        r = ctx.fn.tmp()
        ctx.fn.emit(f"{r} = getelementptr inbounds i8, ptr {base}, i64 {off}")
        return r

    def load(self, ctx, ty, ptr):
        r = ctx.fn.tmp()
        ctx.fn.emit(f"{r} = load {ty}, ptr {ptr}, align {8 if ty in ('i64', 'double', 'ptr') else 4}")
        return r

    def store(self, ctx, ty, val, ptr):
        ctx.fn.emit(f"store {ty} {val}, ptr {ptr}, align {8 if ty in ('i64', 'double', 'ptr') else 4}")

    def op2(self, ctx, instr):
        r = ctx.fn.tmp()
        ctx.fn.emit(f"{r} = {instr}")
        return r

    # -- conversions ------------------------------------------------------------------

    def release(self, ctx, v):
        if not v.owned:
            return
        if v.kind == STR:
            self.call(ctx, "cg_release_str", [("ptr", v.a)])
        elif v.kind == ANY:
            self.call(ctx, "cg_release", [("i64", v.a), ("i64", v.b)])
        v.owned = False

    def to_num(self, ctx, v):
        if v.kind == NUM:
            return v.a
        if v.kind == BOOL:
            return self.op2(ctx, f"uitofp i1 {v.a} to double")
        if v.kind == STR:
            r = self.call(ctx, "cg_str_tonum", [("ptr", v.a)])
            self.release(ctx, v)
            return r
        r = self.call(ctx, "rt_tonum", [("i64", v.a), ("i64", v.b)])
        self.release(ctx, v)
        return r

    def to_bool(self, ctx, v):
        if v.kind == BOOL:
            return v.a
        if v.kind == NUM:
            a = self.op2(ctx, f"fcmp une double {v.a}, 0.0")  # true if != 0 or NaN
            b = self.op2(ctx, f"fcmp ord double {v.a}, 0.0")  # false if NaN
            return self.op2(ctx, f"and i1 {a}, {b}")
        if v.kind == STR:
            r = self.call(ctx, "cg_str_tobool", [("ptr", v.a)])
            self.release(ctx, v)
            return self.op2(ctx, f"icmp ne i32 {r}, 0")
        r = self.call(ctx, "cg_tobool", [("i64", v.a), ("i64", v.b)])
        self.release(ctx, v)
        return self.op2(ctx, f"icmp ne i32 {r}, 0")

    def to_str(self, ctx, v):
        """Returns a V of kind str (owned or borrowed)."""
        if v.kind == STR:
            return v
        if v.kind == NUM:
            return V(STR, self.call(ctx, "rt_num_tostr", [("double", v.a)]), owned=True)
        if v.kind == BOOL:
            z = self.op2(ctx, f"zext i1 {v.a} to i32")
            return V(STR, self.call(ctx, "cg_bool_tostr", [("i32", z)]), owned=False)
        r = self.call(ctx, "rt_tostr", [("i64", v.a), ("i64", v.b)])
        self.release(ctx, v)
        return V(STR, r, owned=True)

    def to_any(self, ctx, v):
        if v.kind == ANY:
            return v
        if v.kind == NUM:
            bits = self.op2(ctx, f"bitcast double {v.a} to i64")
            return V(ANY, "0", bits, owned=False)
        if v.kind == BOOL:
            bits = self.op2(ctx, f"zext i1 {v.a} to i64")
            return V(ANY, "2", bits, owned=False)
        bits = self.op2(ctx, f"ptrtoint ptr {v.a} to i64")
        return V(ANY, "1", bits, owned=v.owned)

    def to_kind(self, ctx, v, kind):
        """Convert to a variable/param representation (no Scratch casting: the
        analysis guarantees v's type is <= kind)."""
        if kind == v.kind:
            return v
        if kind == ANY:
            return self.to_any(ctx, v)
        # kind is narrower than v: only happens for bottom-typed values; box through any
        a = self.to_any(ctx, v)
        if kind == NUM:
            return V(NUM, self.op2(ctx, f"bitcast i64 {a.b} to double"))
        if kind == BOOL:
            return V(BOOL, self.op2(ctx, f"trunc i64 {a.b} to i1"))
        return V(STR, self.op2(ctx, f"inttoptr i64 {a.b} to ptr"), owned=a.owned)

    def retain(self, ctx, v):
        """Make v owned (for storing)."""
        if v.owned or v.kind in (NUM, BOOL):
            return v
        if v.kind == STR:
            self.call(ctx, "cg_retain_str", [("ptr", v.a)])
        else:
            self.call(ctx, "cg_retain", [("i64", v.a), ("i64", v.b)])
        return V(v.kind, v.a, v.b, owned=True)

    # -- storage ---------------------------------------------------------------------------

    def var_ptr(self, ctx, var):
        base = ctx.gvars if var.is_global else ctx.vars
        return self.gep(ctx, base, var.index * self.off["Value.size"])

    def list_ptr(self, ctx, lst):
        base = ctx.glists if lst.is_global else ctx.lists
        return self.gep(ctx, base, lst.index * self.off["List.size"])

    def load_value(self, ctx, ptr, kind):
        """Load a Value slot with a known static kind (borrowed)."""
        if kind == NUM:
            return V(NUM, self.load(ctx, "double", self.gep(ctx, ptr, 8)))
        if kind == BOOL:
            b = self.load(ctx, "i64", self.gep(ctx, ptr, 8))
            return V(BOOL, self.op2(ctx, f"trunc i64 {b} to i1"))
        if kind == STR:
            return V(STR, self.load(ctx, "ptr", self.gep(ctx, ptr, 8)))
        tag = self.load(ctx, "i64", ptr)
        bits = self.load(ctx, "i64", self.gep(ctx, ptr, 8))
        return V(ANY, tag, bits)

    def store_value(self, ctx, ptr, kind, v):
        """Store v (converted to `kind`) into a Value slot, releasing the old value."""
        v = self.to_kind(ctx, v, kind)
        v = self.retain(ctx, v)
        if kind == NUM:
            self.store(ctx, "double", v.a, self.gep(ctx, ptr, 8))
            return
        if kind == BOOL:
            z = self.op2(ctx, f"zext i1 {v.a} to i64")
            self.store(ctx, "i64", z, self.gep(ctx, ptr, 8))
            return
        if kind == STR:
            bp = self.gep(ctx, ptr, 8)
            old = self.load(ctx, "ptr", bp)
            self.store(ctx, "ptr", v.a, bp)
            self.call(ctx, "cg_release_str", [("ptr", old)])
            return
        bp = self.gep(ctx, ptr, 8)
        ot = self.load(ctx, "i64", ptr)
        ob = self.load(ctx, "i64", bp)
        self.store(ctx, "i64", v.a, ptr)
        self.store(ctx, "i64", v.b, bp)
        self.call(ctx, "cg_release", [("i64", ot), ("i64", ob)])

    def store_value_init(self, ctx, ptr, v):
        """Store an owned Value into a fresh slot (no old value to release)."""
        a = self.retain(ctx, self.to_any(ctx, v))
        self.store(ctx, "i64", a.a, ptr)
        self.store(ctx, "i64", a.b, self.gep(ctx, ptr, 8))

    # -- expressions ---------------------------------------------------------------------

    def lit(self, ctx, value):
        if isinstance(value, bool):
            return V(BOOL, "true" if value else "false")
        if isinstance(value, float):
            return V(NUM, hexd(value))
        return V(STR, self.str_lit(str(value)))

    def expr(self, ctx, e):
        if isinstance(e, Lit):
            return self.lit(ctx, e.value)
        if isinstance(e, VarGet):
            return self.load_value(ctx, self.var_ptr(ctx, e.var), e.var.type)
        if isinstance(e, ListContents):
            return V(STR, self.call(ctx, "rt_list_contents", [("ptr", self.list_ptr(ctx, e.lst))]), owned=True)
        if isinstance(e, ArgGet):
            return self.arg(ctx, e.index)
        if isinstance(e, Op):
            return self.reporter(ctx, e)
        raise TypeError(e)

    def arg(self, ctx, i):
        kind = ctx.proc.param_types[i]
        if ctx.mode == "D":
            v = ctx.dparams[i]
            return V(v.kind, v.a, v.b, owned=False)
        slot = self.gep(ctx, ctx.fr, self.off["Frame.slots"] + i * self.off["Value.size"])
        return self.load_value(ctx, slot, kind)

    def eval_args(self, ctx, e, names=None):
        """Evaluate inputs in project.json order (which is scratch-vm's order)."""
        out = {}
        for k, a in e.args.items():
            if names is None or k in names:
                out[k] = self.expr(ctx, a)
        return out

    def num_arg(self, ctx, vals, name):
        v = vals.get(name)
        return self.to_num(ctx, v) if v is not None else "0.0"

    def compare(self, ctx, a, b, ea=None, eb=None):
        """Cast.compare(a, b) -> i32 sign. ea/eb: source expressions (for literal folding)."""
        # numeric literal strings compared with numbers compare numerically
        if a.kind == NUM and b.kind == STR and isinstance(eb, Lit) and isinstance(eb.value, str):
            n = js_number_literal(eb.value)
            if n is not None:
                b = V(NUM, hexd(n))
        if b.kind == NUM and a.kind == STR and isinstance(ea, Lit) and isinstance(ea.value, str):
            n = js_number_literal(ea.value)
            if n is not None:
                a = V(NUM, hexd(n))
        if a.kind == NUM and b.kind == NUM:
            return self.call(ctx, "cg_cmp_nn", [("double", a.a), ("double", b.a)])
        if a.kind == STR and b.kind == STR:
            r = self.call(ctx, "rt_compare_str_str", [("ptr", a.a), ("ptr", b.a)])
            self.release(ctx, a)
            self.release(ctx, b)
            return r
        aa, bb = self.to_any(ctx, a), self.to_any(ctx, b)
        r = self.call(ctx, "cg_cmp", [("i64", aa.a), ("i64", aa.b), ("i64", bb.a), ("i64", bb.b)])
        self.release(ctx, aa)
        self.release(ctx, bb)
        return r

    def reporter(self, ctx, e):
        op = e.opcode
        f = ctx.fn
        if op in ("operator_add", "operator_subtract", "operator_multiply", "operator_divide"):
            vals = self.eval_args(ctx, e)
            a = self.num_arg(ctx, vals, "NUM1")
            b = self.num_arg(ctx, vals, "NUM2")
            ins = {"operator_add": "fadd", "operator_subtract": "fsub",
                   "operator_multiply": "fmul", "operator_divide": "fdiv"}[op]
            return V(NUM, self.op2(ctx, f"{ins} double {a}, {b}"))
        if op == "operator_mod":
            vals = self.eval_args(ctx, e)
            a = self.num_arg(ctx, vals, "NUM1")
            b = self.num_arg(ctx, vals, "NUM2")
            return V(NUM, self.call(ctx, "rt_mod", [("double", a), ("double", b)]))
        if op == "operator_round":
            vals = self.eval_args(ctx, e)
            return V(NUM, self.call(ctx, "rt_round", [("double", self.num_arg(ctx, vals, "NUM"))]))
        if op == "operator_mathop":
            vals = self.eval_args(ctx, e)
            n = self.num_arg(ctx, vals, "NUM")
            name = str(e.fields.get("OPERATOR", "")).lower()
            code = MATHOPS.get(name)
            if code is None:
                return V(NUM, hexd(0.0))
            if name in ("abs", "floor", "sqrt"):
                intr = {"abs": "fabs", "floor": "floor", "sqrt": "sqrt"}[name]
                return V(NUM, self.op2(ctx, f"call double @llvm.{intr}.f64(double {n})"))
            if name == "ceiling":
                return V(NUM, self.op2(ctx, f"call double @llvm.ceil.f64(double {n})"))
            return V(NUM, self.call(ctx, "rt_mathop", [("i32", str(code)), ("double", n)]))
        if op == "operator_random":
            vals = self.eval_args(ctx, e)
            a = self.to_any(ctx, vals.get("FROM", self.lit(ctx, "")))
            b = self.to_any(ctx, vals.get("TO", self.lit(ctx, "")))
            r = self.call(ctx, "rt_random", [("i64", a.a), ("i64", a.b), ("i64", b.a), ("i64", b.b)])
            self.release(ctx, a)
            self.release(ctx, b)
            return V(NUM, r)
        if op in ("operator_lt", "operator_gt", "operator_equals"):
            vals = self.eval_args(ctx, e)
            a = vals.get("OPERAND1", self.lit(ctx, ""))
            b = vals.get("OPERAND2", self.lit(ctx, ""))
            c = self.compare(ctx, a, b, e.args.get("OPERAND1"), e.args.get("OPERAND2"))
            pred = {"operator_lt": "slt", "operator_gt": "sgt", "operator_equals": "eq"}[op]
            return V(BOOL, self.op2(ctx, f"icmp {pred} i32 {c}, 0"))
        if op in ("operator_and", "operator_or"):
            vals = self.eval_args(ctx, e)
            a = self.to_bool(ctx, vals.get("OPERAND1", V(BOOL, "false")))
            b = self.to_bool(ctx, vals.get("OPERAND2", V(BOOL, "false")))
            return V(BOOL, self.op2(ctx, f"{'and' if op == 'operator_and' else 'or'} i1 {a}, {b}"))
        if op == "operator_not":
            vals = self.eval_args(ctx, e)
            a = self.to_bool(ctx, vals.get("OPERAND", V(BOOL, "false")))
            return V(BOOL, self.op2(ctx, f"xor i1 {a}, true"))
        if op == "operator_join":
            vals = self.eval_args(ctx, e)
            a = self.to_str(ctx, vals.get("STRING1", self.lit(ctx, "")))
            b = self.to_str(ctx, vals.get("STRING2", self.lit(ctx, "")))
            r = self.call(ctx, "rt_join", [("ptr", a.a), ("ptr", b.a)])
            self.release(ctx, a)
            self.release(ctx, b)
            return V(STR, r, owned=True)
        if op == "operator_letter_of":
            vals = self.eval_args(ctx, e)
            i = self.num_arg(ctx, vals, "LETTER")
            s = self.to_str(ctx, vals.get("STRING", self.lit(ctx, "")))
            r = self.call(ctx, "rt_letter_of", [("double", i), ("ptr", s.a)])
            self.release(ctx, s)
            return V(STR, r, owned=True)
        if op == "operator_length":
            vals = self.eval_args(ctx, e)
            s = self.to_str(ctx, vals.get("STRING", self.lit(ctx, "")))
            r = self.call(ctx, "rt_length", [("ptr", s.a)])
            self.release(ctx, s)
            return V(NUM, r)
        if op == "operator_contains":
            vals = self.eval_args(ctx, e)
            a = self.to_str(ctx, vals.get("STRING1", self.lit(ctx, "")))
            b = self.to_str(ctx, vals.get("STRING2", self.lit(ctx, "")))
            r = self.call(ctx, "cg_contains", [("ptr", a.a), ("ptr", b.a)])
            self.release(ctx, a)
            self.release(ctx, b)
            return V(BOOL, self.op2(ctx, f"icmp ne i32 {r}, 0"))
        if op == "data_itemoflist":
            vals = self.eval_args(ctx, e)
            lp = self.list_ptr(ctx, e.ref)
            idx = vals.get("INDEX", self.lit(ctx, ""))
            if idx.kind == NUM:
                r = self.call(ctx, "cg_list_item_num", [("ptr", lp), ("double", idx.a)])
            else:
                a = self.to_any(ctx, idx)
                r = self.call(ctx, "cg_list_item", [("ptr", lp), ("i64", a.a), ("i64", a.b)])
                self.release(ctx, a)
            return V(ANY, r[0], r[1], owned=False)
        if op == "data_itemnumoflist":
            vals = self.eval_args(ctx, e)
            a = self.to_any(ctx, vals.get("ITEM", self.lit(ctx, "")))
            r = self.call(ctx, "rt_list_index_of", [("ptr", self.list_ptr(ctx, e.ref)), ("i64", a.a), ("i64", a.b)])
            self.release(ctx, a)
            return V(NUM, r)
        if op == "data_lengthoflist":
            lp = self.list_ptr(ctx, e.ref)
            n = self.load(ctx, "i32", self.gep(ctx, lp, self.off["List.len"]))
            return V(NUM, self.op2(ctx, f"sitofp i32 {n} to double"))
        if op == "data_listcontainsitem":
            vals = self.eval_args(ctx, e)
            a = self.to_any(ctx, vals.get("ITEM", self.lit(ctx, "")))
            r = self.call(ctx, "cg_list_contains", [("ptr", self.list_ptr(ctx, e.ref)), ("i64", a.a), ("i64", a.b)])
            self.release(ctx, a)
            return V(BOOL, self.op2(ctx, f"icmp ne i32 {r}, 0"))
        t = ctx.tgt
        if op == "motion_xposition":
            return V(NUM, self.call(ctx, "rt_get_x", [("ptr", t)]))
        if op == "motion_yposition":
            return V(NUM, self.call(ctx, "rt_get_y", [("ptr", t)]))
        if op == "motion_direction":
            return V(NUM, self.call(ctx, "rt_get_dir", [("ptr", t)]))
        if op == "looks_size":
            return V(NUM, self.call(ctx, "rt_get_size", [("ptr", t)]))
        if op == "looks_costumenumbername":
            if str(e.fields.get("NUMBER_NAME", "number")).lower() == "number":
                return V(NUM, self.call(ctx, "rt_costume_number", [("ptr", t)]))
            return V(STR, self.call(ctx, "rt_costume_name", [("ptr", t)]), owned=True)
        if op == "looks_backdropnumbername":
            if str(e.fields.get("NUMBER_NAME", "number")).lower() == "number":
                return V(NUM, self.call(ctx, "rt_backdrop_number", []))
            return V(STR, self.call(ctx, "rt_backdrop_name", []), owned=True)
        if op == "sensing_touchingobject":
            vals = self.eval_args(ctx, e)
            a = self.to_any(ctx, vals.get("TOUCHINGOBJECTMENU", self.lit(ctx, "")))
            r = self.call(ctx, "cg_touching", [("ptr", t), ("i64", a.a), ("i64", a.b)])
            self.release(ctx, a)
            return V(BOOL, self.op2(ctx, f"icmp ne i32 {r}, 0"))
        if op in ("sensing_touchingcolor", "sensing_coloristouchingcolor", "sensing_keypressed",
                  "sensing_mousedown", "sensing_loud"):
            for v in self.eval_args(ctx, e).values():
                self.release(ctx, v)
            return V(BOOL, "false")
        if op == "sensing_distanceto":
            vals = self.eval_args(ctx, e)
            a = self.to_any(ctx, vals.get("DISTANCETOMENU", self.lit(ctx, "")))
            r = self.call(ctx, "rt_distance_to", [("ptr", t), ("i64", a.a), ("i64", a.b)])
            self.release(ctx, a)
            return V(NUM, r)
        if op == "sensing_answer":
            return V(STR, self.call(ctx, "rt_answer", []), owned=True)
        if op in ("sensing_mousex", "sensing_mousey"):
            return V(NUM, hexd(0.0))
        if op == "sensing_loudness":
            return V(NUM, hexd(-1.0))
        if op == "sensing_timer":
            return V(NUM, self.call(ctx, "rt_timer", []))
        if op == "sensing_of":
            vals = self.eval_args(ctx, e)
            prop = self.str_lit(str(e.fields.get("PROPERTY", "")))
            a = self.to_any(ctx, vals.get("OBJECT", self.lit(ctx, "")))
            r = self.call(ctx, "rt_sensing_of", [("ptr", t), ("ptr", prop), ("i64", a.a), ("i64", a.b)])
            self.release(ctx, a)
            return V(ANY, r[0], r[1], owned=True)
        if op == "sensing_current":
            return V(NUM, self.call(ctx, "cg_current", [("ptr", self.str_lit(str(e.fields.get("CURRENTMENU", ""))))]))
        if op == "sensing_dayssince2000":
            return V(NUM, self.call(ctx, "rt_days_since_2000", []))
        if op == "sensing_username":
            return V(STR, self.call(ctx, "rt_username", []), owned=True)
        if op == "control_get_counter":
            return V(NUM, self.call(ctx, "rt_get_counter", []))
        if op == "sound_volume":
            return V(NUM, self.call(ctx, "cg_get_volume", [("ptr", t)]))
        raise NotImplementedError(op)

    # -- statements -----------------------------------------------------------------------

    def dead_block(self, ctx):
        ctx.fn.block(ctx.fn.label("dead"))

    def new_resume(self, ctx):
        k = len(ctx.resumes) + 1
        label = ctx.fn.label(f"resume{k}_")
        ctx.resumes.append((k, label))
        return k, label

    def set_state(self, ctx, k):
        self.store(ctx, "i32", str(k), self.gep(ctx, ctx.fr, self.off["Frame.state"]))

    def ret_status(self, ctx, code):
        if ctx.mode == "R":
            ctx.fn.emit(f"ret i32 {code}")
        else:
            ctx.fn.emit("ret void")

    def check_stopped(self, ctx):
        """After a call that may stop the current thread: unwind if it did."""
        st = self.load(ctx, "i32", self.gep(ctx, ctx.th, self.off["Thread.status"]))
        c = self.op2(ctx, f"icmp eq i32 {st}, {self.off['TS_DONE']}")
        stop, cont = ctx.fn.label("stopped"), ctx.fn.label("cont")
        ctx.fn.emit(f"br i1 {c}, label %{stop}, label %{cont}")
        ctx.fn.block(stop)
        self.ret_status(ctx, R_STOP)
        ctx.fn.block(cont)

    def stmts(self, ctx, body):
        for s in body:
            self.stmt(ctx, s)

    def clear_resumed(self, ctx):
        if ctx.mode == "R":
            self.store(ctx, "i32", "0", self.gep(ctx, ctx.th, self.off["Thread.resumed"]))

    def stmt(self, ctx, s):
        self.clear_resumed(ctx)
        if isinstance(s, If):
            return self.if_stmt(ctx, s)
        if isinstance(s, Loop):
            return self.loop(ctx, s)
        if isinstance(s, Call):
            return self.call_stmt(ctx, s)
        return self.op_stmt(ctx, s)

    def if_stmt(self, ctx, s):
        c = self.to_bool(ctx, self.expr(ctx, s.cond))
        f = ctx.fn
        lt, le, lm = f.label("then"), f.label("else"), f.label("endif")
        f.emit(f"br i1 {c}, label %{lt}, label %{le}")
        ctx.depth += 1
        f.block(lt)
        self.stmts(ctx, s.then)
        if not f.terminated():
            f.emit(f"br label %{lm}")
        f.block(le)
        self.stmts(ctx, s.else_)
        if not f.terminated():
            f.emit(f"br label %{lm}")
        ctx.depth -= 1
        f.block(lm)

    # loop counters: frame slots (R) or allocas (D)
    def counter_ptr(self, ctx, loop):
        if ctx.mode == "R":
            return self.gep(ctx, ctx.fr, self.off["Frame.slots"] + loop.slot * self.off["Value.size"] + 8)
        if not hasattr(loop, "_alloca") or loop._alloca_fn is not ctx.fn:
            name = ctx.fn.tmp()
            ctx.fn.allocas.append(f"{name} = alloca double, align 8")
            loop._alloca = name
            loop._alloca_fn = ctx.fn
        return loop._alloca

    def loop(self, ctx, s):
        f = ctx.fn
        head, body, exit_ = f.label("head"), f.label("body"), f.label("exit")
        cp = None
        if s.kind == "repeat":
            times = self.to_num(ctx, self.expr(ctx, s.arg))
            n = self.call(ctx, "rt_round", [("double", times)])
            cp = self.counter_ptr(ctx, s)
            self.store(ctx, "double", n, cp)
        elif s.kind == "for_each":
            cp = self.counter_ptr(ctx, s)
            self.store(ctx, "double", "0.0", cp)
        f.emit(f"br label %{head}")
        f.block(head)
        if s.kind == "repeat":
            if not is_pure(s.arg):
                self.release(ctx, self.expr(ctx, s.arg))  # re-evaluated each execution
            cp = self.counter_ptr(ctx, s)
            c = self.load(ctx, "double", cp)
            c2 = self.op2(ctx, f"fsub double {c}, 1.0")
            self.store(ctx, "double", c2, cp)
            go = self.op2(ctx, f"fcmp oge double {c2}, 0.0")
            f.emit(f"br i1 {go}, label %{body}, label %{exit_}")
        elif s.kind == "forever":
            f.emit(f"br label %{body}")
        elif s.kind in ("until", "while"):
            c = self.to_bool(ctx, self.expr(ctx, s.arg))
            if s.kind == "until":
                f.emit(f"br i1 {c}, label %{exit_}, label %{body}")
            else:
                f.emit(f"br i1 {c}, label %{body}, label %{exit_}")
        elif s.kind == "for_each":
            v = self.to_any(ctx, self.expr(ctx, s.arg))
            lim = self.call(ctx, "cg_js_number", [("i64", v.a), ("i64", v.b)])
            self.release(ctx, v)
            cp = self.counter_ptr(ctx, s)
            idx = self.load(ctx, "double", cp)
            go = self.op2(ctx, f"fcmp olt double {idx}, {lim}")
            setv = f.label("foreach_set")
            f.emit(f"br i1 {go}, label %{setv}, label %{exit_}")
            f.block(setv)
            i2 = self.op2(ctx, f"fadd double {idx}, 1.0")
            self.store(ctx, "double", i2, cp)
            self.store_value(ctx, self.var_ptr(ctx, s.var), s.var.type, V(NUM, i2))
            f.emit(f"br label %{body}")
        f.block(body)
        ctx.depth += 1
        self.stmts(ctx, s.body)
        ctx.depth -= 1
        if not f.terminated():
            self.back_edge(ctx, head)
        f.block(exit_)

    def back_edge(self, ctx, head):
        f = ctx.fn
        if ctx.mode != "R":
            f.emit(f"br label %{head}")
            return
        warp = self.load(ctx, "i32", self.gep(ctx, ctx.fr, self.off["Frame.warp"]))
        isw = self.op2(ctx, f"icmp ne i32 {warp}, 0")
        lw, ln, lclr, ly = f.label("be_warp"), f.label("be_norm"), f.label("be_clr"), f.label("be_yield")
        f.emit(f"br i1 {isw}, label %{lw}, label %{ln}")
        f.block(lw)
        exp = self.call(ctx, "cg_warp_expired", [("ptr", ctx.th)])
        e1 = self.op2(ctx, f"icmp ne i32 {exp}, 0")
        f.emit(f"br i1 {e1}, label %{ly}, label %{head}")
        f.block(ln)
        rp = self.gep(ctx, ctx.th, self.off["Thread.resumed"])
        r = self.load(ctx, "i32", rp)
        isr = self.op2(ctx, f"icmp ne i32 {r}, 0")
        f.emit(f"br i1 {isr}, label %{lclr}, label %{ly}")
        f.block(lclr)
        self.store(ctx, "i32", "0", rp)
        f.emit(f"br label %{head}")
        f.block(ly)
        k, rl = self.new_resume(ctx)
        self.set_state(ctx, k)
        f.emit(f"ret i32 {R_YIELD}")
        f.block(rl)
        f.emit(f"br label %{head}")
        self.dead_block(ctx)
        f.emit("unreachable")

    # -- procedure calls -------------------------------------------------------------

    def call_stmt(self, ctx, s):
        p = s.proc
        f = ctx.fn
        order = getattr(s, "order", None) or list(range(len(s.args)))
        vals = [None] * len(s.args)
        for i in order:
            vals[i] = self.expr(ctx, s.args[i])
        vals = [self.to_kind(ctx, v, p.param_types[i]) for i, v in enumerate(vals)]
        if ctx.mode in ("D", "P"):
            self.direct_call(ctx, p, vals)
            return
        if p.atomic and p.warp:
            self.direct_call(ctx, p, vals)
            return
        if p.atomic:
            warp = self.load(ctx, "i32", self.gep(ctx, ctx.fr, self.off["Frame.warp"]))
            isw = self.op2(ctx, f"icmp ne i32 {warp}, 0")
            ld, lr, lj = f.label("call_direct"), f.label("call_res"), f.label("call_join")
            f.emit(f"br i1 {isw}, label %{ld}, label %{lr}")
            f.block(ld)
            self.direct_call(ctx, p, [V(v.kind, v.a, v.b, v.owned) for v in vals], release=True)
            f.emit(f"br label %{lj}")
            f.block(lr)
            self.resumable_call(ctx, p, vals, dynamic_warp=False)
            f.block(lj)
            return
        self.resumable_call(ctx, p, vals, dynamic_warp=True)

    def direct_call(self, ctx, p, vals, release=True):
        args = ["ptr %th", f"ptr {ctx.tgt}"]
        held = []
        for v in vals:
            if v.kind in (STR, ANY) and not v.owned:
                v = self.retain(ctx, v)
            if v.kind == NUM:
                args.append(f"double {v.a}")
            elif v.kind == BOOL:
                args.append(f"i1 {v.a}")
            elif v.kind == STR:
                args.append(f"ptr {v.a}")
                held.append(v)
            else:
                args.append(f"i64 {v.a}")
                args.append(f"i64 {v.b}")
                held.append(v)
        p.needs_direct = True
        ctx.fn.emit(f"call void @D{p.id}({', '.join(args)})")
        for v in held:
            self.release(ctx, v)
        self.check_stopped(ctx)

    def resumable_call(self, ctx, p, vals, dynamic_warp):
        f = ctx.fn
        p.needs_resumable = True
        self.store(ctx, "i32", str(ctx.depth), self.gep(ctx, ctx.fr, self.off["Frame.site_depth"]))
        if p.warp:
            cw = "1"
        else:
            cw = self.load(ctx, "i32", self.gep(ctx, ctx.fr, self.off["Frame.warp"]))
        if p.warp:
            rec = "0"
        else:
            # recursion yield only for non-warp calls
            isw = self.op2(ctx, f"icmp ne i32 {cw}, 0")
            lr, lj = f.label("rec_chk"), f.label("rec_join")
            pre = f.blocks[-1][0]
            f.emit(f"br i1 {isw}, label %{lj}, label %{lr}")
            f.block(lr)
            r = self.call(ctx, "cg_is_recursive", [("ptr", "%th"), ("i32", str(p.id)), ("i32", str(ctx.depth))])
            f.emit(f"br label %{lj}")
            f.block(lj)
            rec = f.tmp()
            f.emit(f"{rec} = phi i32 [ 0, %{pre} ], [ {r}, %{lr} ]")
        k, rl = self.new_resume(ctx)
        self.set_state(ctx, k)
        nf = self.call(ctx, "rt_push_frame", [("ptr", "%th"), ("ptr", f"@R{p.id}"), ("i32", str(p.nslots)),
                                              ("i32", str(p.id)), ("i32", cw)])
        for i, v in enumerate(vals):
            slot = self.gep(ctx, nf, self.off["Frame.slots"] + i * self.off["Value.size"])
            self.store_value_init(ctx, slot, v)
        isrec = self.op2(ctx, f"icmp ne i32 {rec}, 0")
        code = self.op2(ctx, f"select i1 {isrec}, i32 {R_CALL_YIELD}, i32 {R_CALL}")
        f.emit(f"ret i32 {code}")
        f.block(rl)

    # -- re-executing (yielding) blocks ----------------------------------------------------

    def reexec(self, ctx, body_fn):
        """Emit a block that re-executes (with re-evaluated inputs) until the
        runtime returns -1. body_fn(first_ssa) evaluates inputs and returns the
        runtime's i32 result."""
        f = ctx.fn
        k, rl = self.new_resume(ctx)
        ex, nxt, nd, lret = f.label("exec"), f.label("next"), f.label("notdone"), f.label("doret")
        pre = f.blocks[-1][0]
        f.emit(f"br label %{ex}")
        f.block(rl)
        f.emit(f"br label %{ex}")
        f.block(ex)
        first = f.tmp()
        f.emit(f"{first} = phi i32 [ 1, %{pre} ], [ 0, %{rl} ]")
        r = body_fn(first)
        done = self.op2(ctx, f"icmp eq i32 {r}, -1")
        f.emit(f"br i1 {done}, label %{nxt}, label %{nd}")
        f.block(nd)
        # warp mode: a plain yield re-executes immediately unless the warp timer expired
        isy = self.op2(ctx, f"icmp eq i32 {r}, {R_YIELD}")
        warp = self.load(ctx, "i32", self.gep(ctx, ctx.fr, self.off["Frame.warp"]))
        isw = self.op2(ctx, f"icmp ne i32 {warp}, 0")
        both = self.op2(ctx, f"and i1 {isy}, {isw}")
        lspin, lchk = f.label("spin"), f.label("spinchk")
        f.emit(f"br i1 {both}, label %{lchk}, label %{lret}")
        f.block(lchk)
        exp = self.call(ctx, "cg_warp_expired", [("ptr", ctx.th)])
        e1 = self.op2(ctx, f"icmp ne i32 {exp}, 0")
        f.emit(f"br i1 {e1}, label %{lret}, label %{lspin}")
        f.block(lspin)
        f.emit(f"br label %{rl}")
        f.block(lret)
        self.set_state(ctx, k)
        f.emit(f"ret i32 {r}")
        f.block(nxt)

    def promise(self, ctx, r):
        """A promise-returning block: park the thread and continue after it."""
        f = ctx.fn
        k, rl = self.new_resume(ctx)
        self.set_state(ctx, k)
        f.emit(f"ret i32 {r}")
        f.block(rl)

    # -- simple statements ---------------------------------------------------------------

    def op_stmt(self, ctx, s):
        op = s.opcode
        f = ctx.fn
        t = ctx.tgt
        th = ctx.th

        def num(name):
            return self.num_arg(ctx, vals, name)

        def anyv(name):
            return self.to_any(ctx, vals.get(name, self.lit(ctx, "")))

        def strv(name):
            return self.to_str(ctx, vals.get(name, self.lit(ctx, "")))

        if op in ("control_wait", "control_wait_until", "motion_glidesecstoxy", "motion_glideto",
                  "event_broadcastandwait", "looks_switchbackdroptoandwait"):
            assert ctx.mode == "R", op

            def body(first):
                nonlocal vals
                vals = self.eval_args(ctx, s)
                if op == "control_wait":
                    return self.call(ctx, "rt_wait", [("ptr", th), ("ptr", ctx.fr), ("double", num("DURATION")), ("i32", first)])
                if op == "control_wait_until":
                    c = self.to_bool(ctx, vals.get("CONDITION", V(BOOL, "false")))
                    return self.op2(ctx, f"select i1 {c}, i32 -1, i32 {R_YIELD}")
                if op == "motion_glidesecstoxy":
                    secs, x, y = num("SECS"), num("X"), num("Y")
                    return self.call(ctx, "rt_glide", [("ptr", th), ("ptr", ctx.fr), ("double", secs), ("double", x), ("double", y), ("i32", first)])
                if op == "motion_glideto":
                    secs = num("SECS")
                    a = anyv("TO")
                    r = self.call(ctx, "rt_glide_to", [("ptr", th), ("ptr", ctx.fr), ("double", secs), ("i64", a.a), ("i64", a.b), ("i32", first)])
                    self.release(ctx, a)
                    return r
                if op == "event_broadcastandwait":
                    a = anyv("BROADCAST_INPUT")
                    r = self.call(ctx, "cg_broadcast_and_wait", [("ptr", th), ("ptr", ctx.fr), ("i64", a.a), ("i64", a.b), ("i32", first)])
                    self.release(ctx, a)
                    return r
                a = anyv("BACKDROP")
                r = self.call(ctx, "rt_switch_backdrop_and_wait", [("ptr", th), ("ptr", ctx.fr), ("i64", a.a), ("i64", a.b), ("i32", first)])
                self.release(ctx, a)
                return r
            vals = {}
            self.reexec(ctx, body)
            return

        vals = self.eval_args(ctx, s)

        if op in ("looks_sayforsecs", "looks_thinkforsecs"):
            assert ctx.mode == "R", op
            m, secs = anyv("MESSAGE"), anyv("SECS")
            r = self.call(ctx, "cg_say_for_secs", [("ptr", th), ("ptr", ctx.fr), ("i64", m.a), ("i64", m.b),
                                                   ("i64", secs.a), ("i64", secs.b),
                                                   ("i32", "1" if op == "looks_thinkforsecs" else "0")])
            self.release(ctx, m)
            self.release(ctx, secs)
            self.promise(ctx, r)
            return
        if op == "sensing_askandwait":
            assert ctx.mode == "R", op
            q = anyv("QUESTION")
            r = self.call(ctx, "rt_ask", [("ptr", th), ("ptr", ctx.fr), ("i64", q.a), ("i64", q.b), ("i32", "1")])
            self.release(ctx, q)
            self.promise(ctx, r)
            return

        if op == "event_broadcast":
            e = s.args.get("BROADCAST_INPUT")
            if isinstance(e, Lit):
                self.call(ctx, "rt_broadcast", [("ptr", self.str_lit(str(e.value).lower()))])
            else:
                a = anyv("BROADCAST_INPUT")
                self.call(ctx, "cg_broadcast", [("i64", a.a), ("i64", a.b)])
                self.release(ctx, a)
            return
        if op == "control_stop":
            opt = str(s.fields.get("STOP_OPTION", ""))
            if opt == "all":
                self.call(ctx, "rt_stop_all", [("ptr", th)])
                self.ret_status(ctx, R_STOP)
                self.dead_block(ctx)
            elif opt == "this script":
                if ctx.mode == "R":
                    f.emit(f"ret i32 {R_RETURN if ctx.proc is not None else R_STOP}")
                else:
                    f.emit("ret void")
                self.dead_block(ctx)
            elif opt in ("other scripts in sprite", "other scripts in stage"):
                self.call(ctx, "rt_stop_other_scripts", [("ptr", th)])
            return
        if op == "control_create_clone_of":
            a = anyv("CLONE_OPTION")
            self.call(ctx, "cg_create_clone", [("ptr", t), ("i64", a.a), ("i64", a.b)])
            self.release(ctx, a)
            return
        if op == "control_delete_this_clone":
            r = self.call(ctx, "rt_delete_clone", [("ptr", th)])
            c = self.op2(ctx, f"icmp ne i32 {r}, 0")
            ls, lc = f.label("deleted"), f.label("notdeleted")
            f.emit(f"br i1 {c}, label %{ls}, label %{lc}")
            f.block(ls)
            self.ret_status(ctx, R_STOP)
            f.block(lc)
            return
        if op == "control_incr_counter":
            self.call(ctx, "rt_incr_counter", [])
            return
        if op == "control_clear_counter":
            self.call(ctx, "rt_clear_counter", [])
            return
        if op == "data_setvariableto":
            v = vals.get("VALUE", self.lit(ctx, ""))
            self.store_value(ctx, self.var_ptr(ctx, s.ref), s.ref.type, v)
            return
        if op == "data_changevariableby":
            d = num("VALUE")
            vp = self.var_ptr(ctx, s.ref)
            old = self.load_value(ctx, vp, s.ref.type)
            n = self.to_num(ctx, old)
            n2 = self.op2(ctx, f"fadd double {n}, {d}")
            self.store_value(ctx, vp, s.ref.type, V(NUM, n2))
            return
        if op in ("data_showvariable", "data_hidevariable", "data_showlist", "data_hidelist",
                  "looks_hideallsprites", "looks_changestretchby", "looks_setstretchto",
                  "motion_align_scene", "motion_xscroll", "motion_yscroll", "sensing_setdragmode",
                  "sound_play", "sound_playuntildone", "sound_stopallsounds", "sound_seteffectto",
                  "sound_changeeffectby", "sound_cleareffects",
                  "pen_setPenShadeToNumber", "pen_changePenShadeBy", "pen_setPenHueToNumber", "pen_changePenHueBy"):
            for v in vals.values():
                self.release(ctx, v)
            return
        if op in ("data_addtolist", "data_deleteoflist", "data_insertatlist", "data_replaceitemoflist"):
            lp = self.list_ptr(ctx, s.ref)
            if op == "data_addtolist":
                a = anyv("ITEM")
                self.call(ctx, "rt_list_add", [("ptr", lp), ("i64", a.a), ("i64", a.b)])
                self.release(ctx, a)
            elif op == "data_deleteoflist":
                i = anyv("INDEX")
                self.call(ctx, "rt_list_delete", [("ptr", lp), ("i64", i.a), ("i64", i.b)])
                self.release(ctx, i)
            else:
                # evaluation order already handled by eval_args; pass (index, item)
                i, a = anyv("INDEX"), anyv("ITEM")
                fn = "rt_list_insert" if op == "data_insertatlist" else "rt_list_replace"
                self.call(ctx, fn, [("ptr", lp), ("i64", i.a), ("i64", i.b), ("i64", a.a), ("i64", a.b)])
                self.release(ctx, i)
                self.release(ctx, a)
            return
        if op == "data_deletealloflist":
            self.call(ctx, "rt_list_clear", [("ptr", self.list_ptr(ctx, s.ref))])
            return
        if op in ("looks_say", "looks_think"):
            m = anyv("MESSAGE")
            self.call(ctx, "rt_say", [("ptr", t), ("i64", m.a), ("i64", m.b), ("i32", "1" if op == "looks_think" else "0")])
            self.release(ctx, m)
            return
        simple_t = {"looks_show": "rt_show", "looks_hide": "rt_hide", "looks_nextcostume": "rt_next_costume",
                    "looks_cleargraphiceffects": "rt_clear_effects", "motion_ifonedgebounce": "rt_bounce",
                    "pen_stamp": "rt_pen_stamp", "pen_penDown": "rt_pen_down", "pen_penUp": "rt_pen_up"}
        if op in simple_t:
            self.call(ctx, simple_t[op], [("ptr", t)])
            return
        if op == "looks_nextbackdrop":
            self.call(ctx, "rt_next_backdrop", [])
            return
        if op == "pen_clear":
            self.call(ctx, "rt_pen_clear", [])
            return
        if op == "sensing_resettimer":
            self.call(ctx, "rt_reset_timer", [])
            return
        if op in ("looks_switchcostumeto", "looks_switchbackdropto", "motion_goto", "motion_pointtowards",
                  "pen_setPenColorToColor"):
            name = {"looks_switchcostumeto": "COSTUME", "looks_switchbackdropto": "BACKDROP", "motion_goto": "TO",
                    "motion_pointtowards": "TOWARDS", "pen_setPenColorToColor": "COLOR"}[op]
            a = anyv(name)
            if op == "looks_switchbackdropto":
                self.call(ctx, "rt_switch_backdrop", [("i64", a.a), ("i64", a.b)])
            else:
                fn = {"looks_switchcostumeto": "rt_switch_costume", "motion_goto": "rt_goto",
                      "motion_pointtowards": "rt_point_towards", "pen_setPenColorToColor": "rt_pen_set_color"}[op]
                self.call(ctx, fn, [("ptr", t), ("i64", a.a), ("i64", a.b)])
            self.release(ctx, a)
            return
        num_t = {"looks_changesizeby": ("rt_change_size", "CHANGE"), "looks_setsizeto": ("rt_set_size", "SIZE"),
                 "motion_movesteps": ("rt_move_steps", "STEPS"), "motion_turnright": ("rt_turn", "DEGREES"),
                 "motion_pointindirection": ("rt_point_dir", "DIRECTION"), "motion_changexby": ("cg_change_x", "DX"),
                 "motion_setx": ("cg_set_x", "X"), "motion_changeyby": ("cg_change_y", "DY"),
                 "motion_sety": ("cg_set_y", "Y"), "pen_changePenSizeBy": ("rt_pen_change_size", "SIZE"),
                 "pen_setPenSizeTo": ("rt_pen_set_size", "SIZE"), "sound_changevolumeby": ("cg_change_volume", "VOLUME"),
                 "sound_setvolumeto": ("cg_set_volume", "VOLUME")}
        if op in num_t:
            fn, name = num_t[op]
            self.call(ctx, fn, [("ptr", t), ("double", num(name))])
            return
        if op == "motion_turnleft":
            d = num("DEGREES")
            nd = self.op2(ctx, f"fneg double {d}")
            self.call(ctx, "rt_turn", [("ptr", t), ("double", nd)])
            return
        if op == "motion_gotoxy":
            x, y = num("X"), num("Y")
            self.call(ctx, "rt_set_xy", [("ptr", t), ("double", x), ("double", y)])
            return
        if op == "motion_setrotationstyle":
            self.call(ctx, "rt_set_rotation_style", [("ptr", t), ("ptr", self.str_lit(str(s.fields.get("STYLE", ""))))])
            return
        if op in ("looks_changeeffectby", "looks_seteffectto"):
            eff = self.str_lit(str(s.fields.get("EFFECT", "")))
            v = num("CHANGE" if op == "looks_changeeffectby" else "VALUE")
            fn = "cg_change_effect" if op == "looks_changeeffectby" else "cg_set_effect"
            self.call(ctx, fn, [("ptr", t), ("ptr", eff), ("double", v)])
            return
        if op == "looks_gotofrontback":
            front = "1" if str(s.fields.get("FRONT_BACK", "front")) == "front" else "0"
            self.call(ctx, "rt_go_front_back", [("ptr", t), ("i32", front)])
            return
        if op == "looks_goforwardbackwardlayers":
            fwd = "1" if str(s.fields.get("FORWARD_BACKWARD", "forward")) == "forward" else "0"
            self.call(ctx, "rt_go_layers", [("ptr", t), ("i32", fwd), ("double", num("NUM"))])
            return
        if op in ("pen_changePenColorParamBy", "pen_setPenColorParamTo"):
            p = strv("COLOR_PARAM")
            v = num("VALUE")
            fn = "rt_pen_change_param" if op == "pen_changePenColorParamBy" else "rt_pen_set_param"
            self.call(ctx, fn, [("ptr", t), ("ptr", p.a), ("double", v)])
            self.release(ctx, p)
            return
        raise NotImplementedError(op)

    # -- functions ----------------------------------------------------------------------

    def entry(self, ctx, target_from_thread=True):
        f = ctx.fn
        f.block("entry")
        if target_from_thread:
            ctx.tgt = self.load(ctx, "ptr", self.gep(ctx, "%th", self.off["Thread.target"]))
        ctx.vars = self.load(ctx, "ptr", self.gep(ctx, ctx.tgt, self.off["Target.vars"]))
        ctx.lists = self.load(ctx, "ptr", self.gep(ctx, ctx.tgt, self.off["Target.lists"]))
        ctx.gvars = self.load(ctx, "ptr", "@rt_globals")
        ctx.glists = self.load(ctx, "ptr", "@rt_global_lists")

    def assign_slots(self, body, start):
        n = start

        def walk(stmts):
            nonlocal n
            for s in stmts:
                if isinstance(s, If):
                    walk(s.then)
                    walk(s.else_)
                elif isinstance(s, Loop):
                    if s.kind in ("repeat", "for_each"):
                        s.slot = n
                        n += 1
                    walk(s.body)
        walk(body)
        return n

    def resumable(self, name, body, proc):
        fn = Fn(name, "i32", ["ptr %th", "ptr %fr"])
        ctx = Ctx(fn, "R", proc)
        self.entry(ctx)
        state = self.load(ctx, "i32", self.gep(ctx, "%fr", self.off["Frame.state"]))
        fn.block("start")
        self.stmts(ctx, body)
        if not fn.terminated():
            fn.emit(f"ret i32 {R_RETURN if proc is not None else R_STOP}")
        cases = " ".join(f"i32 {k}, label %{l}" for k, l in ctx.resumes)
        fn.entry_tail = f"switch i32 {state}, label %start [ {cases} ]"
        self.fns.append(fn)

    def direct(self, p):
        params = ["ptr %th", "ptr %tg"]
        dparams = []
        for i, kind in enumerate(p.param_types):
            if kind == NUM:
                params.append(f"double %p{i}")
                dparams.append(V(NUM, f"%p{i}"))
            elif kind == BOOL:
                params.append(f"i1 %p{i}")
                dparams.append(V(BOOL, f"%p{i}"))
            elif kind == STR:
                params.append(f"ptr %p{i}")
                dparams.append(V(STR, f"%p{i}"))
            else:
                params.append(f"i64 %p{i}a")
                params.append(f"i64 %p{i}b")
                dparams.append(V(ANY, f"%p{i}a", f"%p{i}b"))
        fn = Fn(f"D{p.id}", "void", params)
        ctx = Ctx(fn, "D", p)
        ctx.tgt = "%tg"
        ctx.dparams = dparams
        self.entry(ctx, target_from_thread=False)
        self.stmts(ctx, p.body)
        if not fn.terminated():
            fn.emit("ret void")
        self.fns.append(fn)

    def predicate(self, sc):
        fn = Fn(f"P{sc.index}", "{ i64, i64 }", ["ptr %tg"])
        ctx = Ctx(fn, "P", None)
        ctx.tgt = "%tg"
        ctx.th = "null"
        self.entry(ctx, target_from_thread=False)
        if sc.hat == "TIMER_GT":
            v = self.expr(ctx, sc.hat_args["VALUE"])
            n = self.to_num(ctx, v)
            if sc.hat_args.get("menu") == "timer":
                tm = self.call(ctx, "rt_timer", [])
                b = self.op2(ctx, f"fcmp ogt double {tm}, {n}")
            else:
                b = "false"
        else:
            a = self.to_any(ctx, self.expr(ctx, sc.hat_args["TOUCHINGOBJECTMENU"]))
            r = self.call(ctx, "cg_touching", [("ptr", "%tg"), ("i64", a.a), ("i64", a.b)])
            self.release(ctx, a)
            b = self.op2(ctx, f"icmp ne i32 {r}, 0")
        z = self.op2(ctx, f"zext i1 {b} to i64")
        r1 = self.op2(ctx, "insertvalue { i64, i64 } undef, i64 2, 0")
        r2 = self.op2(ctx, f"insertvalue {{ i64, i64 }} {r1}, i64 {z}, 1")
        fn.emit(f"ret {{ i64, i64 }} {r2}")
        self.fns.append(fn)

    # -- program init -----------------------------------------------------------------------

    def value_args(self, ctx, value):
        if isinstance(value, bool):
            return [("i64", "2"), ("i64", "1" if value else "0")]
        if isinstance(value, float):
            return [("i64", "0"), ("i64", str(struct.unpack("<q", struct.pack("<d", value))[0]))]
        return [("i64", "1"), ("i64", f"ptrtoint (ptr {self.str_lit(str(value))} to i64)")]

    def program_init(self):
        fn = Fn("rt_program_init", "void", [])
        ctx = Ctx(fn, "I")
        fn.block("entry")
        for s in self.prog.sprites:
            sp = self.call(ctx, "rt_add_sprite", [("ptr", self.str_lit(s.name)), ("i32", "1" if s.is_stage else "0"),
                                                  ("i32", str(len(s.vars))), ("i32", str(len(s.lists))),
                                                  ("i32", str(len(s.costumes)))])
            for v in s.vars:
                val = v.value if isinstance(v.value, (str, float, bool)) else str(v.value)
                self.call(ctx, "rt_sprite_var", [("ptr", sp), ("i32", str(v.index)), ("ptr", self.str_lit(v.name))]
                          + self.value_args(ctx, val))
            for L in s.lists:
                self.call(ctx, "rt_sprite_list", [("ptr", sp), ("i32", str(L.index)), ("ptr", self.str_lit(L.name))])
                for item in L.value:
                    item = item if isinstance(item, (str, float, bool)) else str(item)
                    self.call(ctx, "rt_sprite_list_item", [("ptr", sp), ("i32", str(L.index))] + self.value_args(ctx, item))
            for i, c in enumerate(s.costumes):
                if c.raster:
                    rw, rh, scale, gname = c.raster
                    raster = [("i32", str(rw)), ("i32", str(rh)), ("double", hexd(scale)), ("ptr", gname)]
                else:
                    raster = [("i32", "0"), ("i32", "0"), ("double", hexd(1.0)), ("ptr", "null")]
                self.call(ctx, "rt_sprite_costume", [("ptr", sp), ("i32", str(i)), ("ptr", self.str_lit(c.name)),
                                                     ("double", hexd(c.w)), ("double", hexd(c.h)),
                                                     ("double", hexd(c.cx / c.bitmap_res)),
                                                     ("double", hexd(c.cy / c.bitmap_res))] + raster)
            cur = s.current_costume if 0 <= s.current_costume < max(1, len(s.costumes)) else 0
            self.call(ctx, "rt_sprite_state", [("ptr", sp), ("double", hexd(s.x)), ("double", hexd(s.y)),
                                               ("double", hexd(s.direction)), ("double", hexd(s.size)),
                                               ("i32", "1" if s.visible else "0"), ("i32", str(cur)),
                                               ("i32", str(s.layer)), ("i32", str(s.rotation_style)),
                                               ("double", hexd(s.volume))])
            for sc in s.scripts:
                match = self.str_lit(sc.match) if sc.match is not None else "null"
                pred = f"@P{sc.index}" if sc.hat in ("TIMER_GT", "TOUCHING") else "null"
                self.call(ctx, "rt_add_script", [("ptr", sp), ("i32", str(HAT_IDS[sc.hat])), ("ptr", f"@S{sc.index}"),
                                                 ("i32", str(sc.nslots)), ("ptr", match), ("ptr", pred)])
        fn.emit("ret void")
        text = fn.render().replace("define internal void @rt_program_init", "define void @rt_program_init")
        return text

    # -- module -------------------------------------------------------------------------------

    def generate(self):
        prog = self.prog
        for p in prog.procs:
            p.nslots = self.assign_slots(p.body, len(p.argids))
        for sc in prog.scripts:
            sc.nslots = self.assign_slots(sc.body, 0)
        for sc in prog.scripts:
            self.resumable(f"S{sc.index}", sc.body, None)
            if sc.hat in ("TIMER_GT", "TOUCHING"):
                self.predicate(sc)
        # procedures: generate on demand (a direct variant can be needed from a
        # resumable caller, and vice versa); iterate until no new variants appear
        done_r, done_d = set(), set()
        while True:
            progress = False
            for p in prog.procs:
                if p.needs_resumable and p.id not in done_r:
                    done_r.add(p.id)
                    self.resumable(f"R{p.id}", p.body, p)
                    progress = True
                if p.needs_direct and p.id not in done_d:
                    done_d.add(p.id)
                    self.direct(p)
                    progress = True
            if not progress:
                break
        init = self.program_init()
        out = [f"; generated by Claude's LLVM Scratch (scc)",
               f"target datalayout = {self.abi['datalayout']}",
               f"target triple = {self.abi['triple']}", ""]
        out.append("@rt_globals = external global ptr")
        out.append("@rt_global_lists = external global ptr")
        out.extend(self.globals)
        out.append("")
        for name in sorted(self.used_decls):
            out.append(self.sigs[name]["decl"])
        for intr in ("fabs", "floor", "ceil", "sqrt"):
            out.append(f"declare double @llvm.{intr}.f64(double)")
        out.append("")
        for fn in self.fns:
            out.append(fn.render())
            out.append("")
        out.append(init)
        return "\n".join(out) + "\n"


def generate(prog, abi):
    return CodeGen(prog, abi).generate()
