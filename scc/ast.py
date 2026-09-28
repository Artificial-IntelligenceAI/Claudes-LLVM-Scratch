"""
AST for compiled Scratch programs.

Blocks are lowered into statements and expressions. Anything whose exact
semantics lives in the runtime stays a generic Op carrying its opcode.
"""


class Node:
    __slots__ = ()


# ---------------------------------------------------------------------------
# Expressions


class Lit(Node):
    """A literal value exactly as stored in project.json (str, float, bool)."""
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def __repr__(self):
        return f"Lit({self.value!r})"


class VarGet(Node):
    __slots__ = ("var",)

    def __init__(self, var):
        self.var = var

    def __repr__(self):
        return f"VarGet({self.var.name})"


class ListContents(Node):
    __slots__ = ("lst",)

    def __init__(self, lst):
        self.lst = lst


class ArgGet(Node):
    """argument_reporter_*: index into the enclosing procedure's params, or None (-> 0)."""
    __slots__ = ("index", "boolean")

    def __init__(self, index, boolean):
        self.index = index
        self.boolean = boolean


class Op(Node):
    """A reporter or statement handled by opcode.

    args: dict input-name -> expression
    fields: dict field-name -> raw field value
    ref: resolved variable/list for data blocks
    """
    __slots__ = ("opcode", "args", "fields", "ref", "extra")

    def __init__(self, opcode, args=None, fields=None, ref=None, extra=None):
        self.opcode = opcode
        self.args = args or {}
        self.fields = fields or {}
        self.ref = ref
        self.extra = extra

    def __repr__(self):
        return f"Op({self.opcode}, {self.args}, {self.fields})"


# ---------------------------------------------------------------------------
# Statements (control flow is structured)


class If(Node):
    __slots__ = ("cond", "then", "else_")

    def __init__(self, cond, then, else_):
        self.cond, self.then, self.else_ = cond, then, else_


class Loop:
    """kind: repeat | forever | until | while | for_each"""

    def __init__(self, kind, arg, body, var=None):
        self.kind, self.arg, self.body, self.var = kind, arg, body, var
        self.slot = None  # frame slot for the loop counter (resumable code)


class Call:
    def __init__(self, proc, args, order=None):
        self.proc = proc      # Procedure
        self.args = args      # list of expressions, one per param
        self.order = order    # evaluation order (project.json input order)


# ---------------------------------------------------------------------------
# Program structure


class Variable:
    def __init__(self, name, value, owner, index, is_list=False, vid=None):
        self.name = name
        self.value = value          # initial value (list: list of items)
        self.owner = owner          # Sprite
        self.index = index          # slot index within owner
        self.is_list = is_list
        self.id = vid
        self.type = None            # inferred (scalars) / item type (lists)

    @property
    def is_global(self):
        return self.owner.is_stage

    def __repr__(self):
        return f"<{'list' if self.is_list else 'var'} {self.owner.name}.{self.name}>"


class Procedure:
    def __init__(self, sprite, proccode, argnames, argids, argdefaults, warp, kinds):
        self.sprite = sprite
        self.proccode = proccode
        self.argnames = argnames
        self.argids = argids
        self.argdefaults = argdefaults
        self.warp = warp
        self.kinds = kinds          # 's' or 'b' per param
        self.body = []
        self.id = None              # global procedure index
        self.param_types = None
        self.atomic = None          # no yields when run in warp mode
        self.needs_resumable = False
        self.needs_direct = False
        self.nslots = 0

    def __repr__(self):
        return f"<proc {self.sprite.name}:{self.proccode}>"


class Script:
    def __init__(self, sprite, hat, match, body, hat_args=None):
        self.sprite = sprite
        self.hat = hat              # HAT_* name
        self.match = match          # lower-cased match string or None
        self.body = body
        self.hat_args = hat_args or {}
        self.index = None
        self.nslots = 0


class Costume:
    def __init__(self, name, data_format, asset, cx, cy, bitmap_res, md5ext):
        self.name = name
        self.data_format = data_format
        self.asset = asset          # bytes or None
        self.cx, self.cy = cx, cy
        self.bitmap_res = bitmap_res
        self.md5ext = md5ext
        self.w = self.h = 0.0       # logical size, filled by the rasterizer
        self.raster = None          # (w, h, scale, rgba bytes)


class Sprite:
    def __init__(self, name, is_stage):
        self.name = name
        self.is_stage = is_stage
        self.vars = []
        self.lists = []
        self.scripts = []
        self.procs = []             # in definition order
        self.proc_by_code = {}
        self.costumes = []
        self.current_costume = 0
        self.x = self.y = 0.0
        self.direction = 90.0
        self.size = 100.0
        self.visible = True
        self.layer = 0
        self.rotation_style = 0
        self.volume = 100.0
        self.tempo = 60.0


class Program:
    def __init__(self):
        self.stage = None
        self.sprites = []           # stage first
        self.procs = []
        self.scripts = []
        self.warnings = []
        self.extensions = []
