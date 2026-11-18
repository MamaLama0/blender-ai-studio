#!/usr/bin/env python3
"""Imite la ligne de commande de Blender avec le module `bpy` (pip install bpy).

Sert uniquement aux tests sur une machine sans Blender :
  fake_blender.py -b [--factory-startup] [fichier.blend] --python script.py -- args
"""
import runpy
import sys

argv = sys.argv[1:]
rest = argv[argv.index("--"):] if "--" in argv else []
head = argv[: len(argv) - len(rest)]

import bpy  # noqa: E402

script = None
expr = None
i = 0
while i < len(head):
    arg = head[i]
    if arg == "--python":
        script = head[i + 1]
        i += 2
        continue
    if arg == "--python-expr":
        expr = head[i + 1]
        i += 2
        continue
    if arg.endswith(".blend"):
        bpy.ops.wm.open_mainfile(filepath=arg)
    elif arg == "--factory-startup":
        bpy.ops.wm.read_factory_settings(use_empty=True)
    i += 1

if expr:
    exec(expr, {"__name__": "__main__"})
if script:
    sys.argv = [script] + rest
    runpy.run_path(script, run_name="__main__")
