# SPDX-License-Identifier: AGPL-3.0-only
"""
Build the runtime as LLVM bitcode (for link-time optimisation with compiled
programs) and extract the ABI facts the code generator relies on:

  - struct offsets (runtime/offsets.c)
  - exact function signatures, read from the runtime's own LLVM IR so the
    generated `declare`s always match clang's lowering (e.g. bool -> zeroext i1,
    a 16-byte Value -> two i64 arguments / { i64, i64 } return)
"""

import json
import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RT = os.path.join(ROOT, "runtime")
BUILD = os.path.join(ROOT, "build", "rt")

C_SOURCES = ["value.c", "list.c", "sched.c", "blocks.c", "cg.c", "render.c", "main.c"]
CXX_SOURCES = [("fdlibm/ieee754.cc", "fdlibm_ieee754"), ("fdlibm/fd.cc", "fdlibm_fd")]
CFLAGS = ["-O2", "-std=gnu11", "-fno-math-errno", "-Wall", "-Wextra"]
CXXFLAGS = ["-O2", "-std=c++17", "-fno-exceptions", "-fno-rtti", "-fno-math-errno", "-I", os.path.join(RT, "fdlibm")]

CLANG = os.environ.get("CLANG", "clang")
CLANGXX = os.environ.get("CLANGXX", "clang++")


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}\n{r.stdout}\n{r.stderr}")
    return r.stdout


def _stale(out, deps):
    if not os.path.exists(out):
        return True
    t = os.path.getmtime(out)
    return any(os.path.getmtime(d) > t for d in deps)


def build_runtime(verbose=False):
    os.makedirs(BUILD, exist_ok=True)
    header = os.path.join(RT, "rt.h")
    objs, lls = [], []
    for src in C_SOURCES:
        path = os.path.join(RT, src)
        base = os.path.splitext(src)[0]
        obj = os.path.join(BUILD, base + ".o")
        ll = os.path.join(BUILD, base + ".ll")
        if _stale(obj, [path, header]):
            if verbose:
                print(f"  cc  {src}")
            _run([CLANG, *CFLAGS, "-flto", "-c", path, "-o", obj])
        if _stale(ll, [path, header]):
            _run([CLANG, *CFLAGS, "-S", "-emit-llvm", path, "-o", ll])
        objs.append(obj)
        lls.append(ll)
    for src, base in CXX_SOURCES:
        path = os.path.join(RT, src)
        obj = os.path.join(BUILD, base + ".o")
        ll = os.path.join(BUILD, base + ".ll")
        deps = [path] + [os.path.join(RT, "fdlibm/src/base", h) for h in os.listdir(os.path.join(RT, "fdlibm/src/base"))]
        if _stale(obj, deps):
            if verbose:
                print(f"  c++ {src}")
            _run([CLANGXX, *CXXFLAGS, "-flto", "-c", path, "-o", obj])
        if _stale(ll, deps):
            _run([CLANGXX, *CXXFLAGS, "-S", "-emit-llvm", path, "-o", ll])
        objs.append(obj)
        lls.append(ll)
    abi_path = os.path.join(BUILD, "abi.json")
    if _stale(abi_path, [os.path.join(RT, "offsets.c"), header] + lls):
        exe = os.path.join(BUILD, "offsets")
        _run([CLANG, "-O0", "-I", RT, os.path.join(RT, "offsets.c"), "-o", exe])
        offsets = json.loads(_run([exe]))
        sigs = {}
        for ll in lls:
            sigs.update(extract_signatures(open(ll).read()))
        triple = dl = None
        for line in open(lls[0]):
            if line.startswith("target triple"):
                triple = line.split("=", 1)[1].strip()
            elif line.startswith("target datalayout"):
                dl = line.split("=", 1)[1].strip()
        abi = {"offsets": offsets, "signatures": sigs, "triple": triple, "datalayout": dl}
        with open(abi_path, "w") as f:
            json.dump(abi, f, indent=1)
    with open(abi_path) as f:
        abi = json.load(f)
    return objs, abi


_ATTR = re.compile(
    r"\b(noundef|zeroext|signext|nonnull|noalias|nocapture|readonly|writeonly|returned|inreg|"
    r"dso_local|local_unnamed_addr|unnamed_addr|internal|hidden|nofree|nosync|nounwind|"
    r"willreturn|mustprogress|inlinehint|noinline|optnone|cold|hot|fastcc|ccc|captures\([^)]*\))\b"
    r"|\b(dereferenceable|dereferenceable_or_null|align|range)\s*\([^)]*\)|\balign\s+\d+")


_TYPE = re.compile(r"\{[^}]*\}|\bptr\b|\bdouble\b|\bfloat\b|\bvoid\b|\bi\d+\b")


def _clean_type(t):
    """Extract the IR type from a parameter/return string, ignoring attributes."""
    t = re.sub(r"\b\w+\([^)]*\)", " ", t)  # attributes with arguments, e.g. range(i32 0, 2)
    m = _TYPE.search(t)
    if not m:
        raise ValueError(f"cannot find a type in {t!r}")
    return " ".join(m.group(0).replace("{", "{ ").replace("}", " }").replace(",", ", ").split())


def extract_signatures(text):
    """Return {name: {"ret": type, "params": [types], "decl": 'declare ...'}} for rt_/cg_ functions."""
    sigs = {}
    for line in text.splitlines():
        if not line.startswith("define "):
            continue
        m = re.match(r"define\s+(.*?)@([\w.$]+)\((.*)\)\s*(.*)\{\s*$", line)
        if not m:
            continue
        name = m.group(2)
        if not (name.startswith("rt_") or name.startswith("cg_")):
            continue
        prefix, params_s = m.group(1), m.group(3)
        # split params at top-level commas
        params, depth, cur = [], 0, ""
        for ch in params_s:
            if ch in "({[":
                depth += 1
            elif ch in ")}]":
                depth -= 1
            if ch == "," and depth == 0:
                params.append(cur)
                cur = ""
            else:
                cur += ch
        if cur.strip():
            params.append(cur)
        ret_attrs = " ".join(a for a in ("zeroext", "signext") if re.search(rf"\b{a}\b", prefix))
        ret = _clean_type(prefix)
        ptypes, pdecl = [], []
        for p in params:
            p = p.strip()
            if p == "...":
                continue
            ty = _clean_type(p)
            attrs = " ".join(a for a in ("zeroext", "signext") if re.search(rf"\b{a}\b", p))
            ptypes.append(ty)
            pdecl.append(f"{ty} {attrs}".strip())
        decl = f"declare {ret_attrs + ' ' if ret_attrs else ''}{ret} @{name}({', '.join(pdecl)})"
        sigs[name] = {"ret": ret, "params": ptypes, "decl": decl}
    return sigs


if __name__ == "__main__":
    objs, abi = build_runtime(verbose=True)
    print(json.dumps(abi["offsets"], indent=1))
    for k in ("rt_tonum", "rt_tobool", "rt_tostr", "rt_list_item", "cg_cmp_nn", "rt_js_random"):
        print(abi["signatures"].get(k, {}).get("decl"))
