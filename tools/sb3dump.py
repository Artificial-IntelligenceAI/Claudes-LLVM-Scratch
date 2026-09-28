#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Pretty-print the scripts of a .sb3 as indented pseudo-code (for debugging)."""
import json
import sys
import zipfile


def main(path):
    proj = json.loads(zipfile.ZipFile(path).read("project.json"))
    for t in proj["targets"]:
        blocks = t["blocks"]
        print(f"== {t['name']}  vars={ {v[0]: v[1] for v in t['variables'].values()} }  "
              f"lists={ {v[0]: v[1] for v in t['lists'].values()} }")

        def val(x):
            if isinstance(x, list):
                if x[0] in (12, 13):
                    return f"${x[1]}"
                return json.dumps(x[1])
            if x is None:
                return "<empty>"
            return expr(x)

        def inp(b, name):
            i = b["inputs"].get(name)
            if not i:
                return "<none>"
            return val(i[1])

        def expr(bid):
            b = blocks[bid]
            op = b["opcode"]
            f = b.get("fields", {})
            if not b["inputs"] and len(f) == 1:
                return json.dumps(list(f.values())[0][0])
            parts = [f"{k}={inp(b, k)}" for k in b["inputs"] if k not in ("SUBSTACK", "SUBSTACK2")]
            parts += [f"{k}:{v[0]}" for k, v in f.items()]
            return f"{op.split('_', 1)[1]}({', '.join(parts)})"

        def stack(bid, ind):
            while bid:
                b = blocks[bid]
                op = b["opcode"]
                if op == "procedures_definition":
                    proto = blocks[b["inputs"]["custom_block"][1]]
                    print(" " * ind + f"define {proto['mutation']['proccode']} {proto['mutation']['argumentnames']}"
                          f" warp={proto['mutation']['warp']}")
                elif op == "procedures_call":
                    args = [inp(b, k) for k in b["inputs"]]
                    print(" " * ind + f"call {b['mutation']['proccode']} {args}")
                else:
                    print(" " * ind + expr(bid))
                for sub in ("SUBSTACK", "SUBSTACK2"):
                    if sub in b["inputs"] and b["inputs"][sub][1]:
                        print(" " * ind + f"  [{sub}]")
                        stack(b["inputs"][sub][1], ind + 4)
                bid = b.get("next")

        for bid, b in blocks.items():
            if isinstance(b, dict) and b.get("topLevel"):
                stack(bid, 2)
                print()


if __name__ == "__main__":
    main(sys.argv[1])
