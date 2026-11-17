"""Lancé par l'application : blender -b [projet.blend] --python run_step.py -- '<json>'

Le JSON contient : step, args, save_as (chemin .blend), preview (chemin .glb optionnel).
Le résultat est imprimé sur une ligne préfixée par __BAS_RESULT__.
"""
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bpy  # noqa: E402

import bas_pipeline  # noqa: E402

MARK = "__BAS_RESULT__"


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    job = json.loads(argv[0]) if argv else {}
    try:
        result = bas_pipeline.run_step(job["step"], job.get("args"))
        if job.get("preview"):
            bas_pipeline.step_export(job["preview"], "glb")
        if job.get("save_as"):
            bpy.context.preferences.filepaths.save_version = 0  # pas de fichiers .blend1
            bpy.ops.wm.save_as_mainfile(filepath=job["save_as"])
        print(MARK + json.dumps({"ok": True, "result": result}), flush=True)
    except Exception as exc:
        print(MARK + json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                                 "traceback": traceback.format_exc()[-3000:]}), flush=True)


main()
