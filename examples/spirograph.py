# SPDX-License-Identifier: AGPL-3.0-only
"""A pen spirograph plus bouncing clones -- the README demo.

    python3 examples/spirograph.py            # writes build/examples/spirograph.sb3
    python3 -m scc build/examples/spirograph.sb3 -o build/examples/spirograph
    build/examples/spirograph --render out --render-every 15
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from sb3 import *  # noqa: E402,F403

p = Project()
p.stage.costumes = [Costume("night", rect_svg(480, 360, "#101828"))]
pen = p.sprite("Pen", x=0, y=0, visible=False,
               costumes=[Costume("dot", circle_svg(4, "#ffffff"))])
i = p.stage.var("i", 0)
pen.proc(define("petal %s %s", ["r", "hue"],
                pen_set_param("color", arg("hue")),
                repeat(36, move(arg("r")), turn_right(10)),
                warp=True))
pen.proc(define("flower", [],
                pen_clear(), pen_size(2), goto_xy(0, 0), point_dir(90), pen_down(),
                set_var(i, 0),
                repeat(36, call("petal %s %s", 9, mul(i, 2.7)), turn_right(10), change_var(i, 1)),
                pen_up(),
                warp=True))
pen.script(when_flag(), call("flower"), broadcast("bounce"))

ball = p.sprite("Ball", x=0, y=0, visible=False,
                costumes=[Costume("ball", circle_svg(26, "#ffb400"))])
ball.script(when_broadcast("bounce"), repeat(6, create_clone()))
ball.script(when_clone(), show(), goto_xy(rnd(-150, 150), rnd(-100, 100)), point_dir(rnd(-180, 180)),
            set_effect("ghost", 20), change_effect("color", mul(rnd(0, 6), 25)),
            repeat(60, move(7), bounce()))

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "build", "examples")
os.makedirs(out, exist_ok=True)
p.save(os.path.join(out, "spirograph.sb3"))
print(os.path.join(out, "spirograph.sb3"))
