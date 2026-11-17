"""Test de bout en bout du pipeline avec le module `bpy` (pip install bpy).

Construit un petit personnage toon, l'exporte en GLB, puis le passe dans
import → rig → animate → export et vérifie le GLB final.
Lancement : python tests/test_pipeline_bpy.py
"""
import json
import os
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "blender_ai_studio", "blender_side"))

import bpy  # noqa: E402

import bas_pipeline as P  # noqa: E402


def build_dummy(path):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(0, 0, 1.0))
    bpy.context.object.scale = (1, 0.8, 1.1)
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.35, location=(0, 0, 1.75))
    for x in (-0.22, 0.22):
        bpy.ops.mesh.primitive_cylinder_add(radius=0.09, depth=0.5, location=(x, 0, 0.3))
    for x in (-0.65, 0.65):
        bpy.ops.mesh.primitive_cylinder_add(radius=0.07, depth=0.45, location=(x, 0, 1.1),
                                            rotation=(0, 1.3, 0))
    bpy.ops.export_scene.gltf(filepath=path, export_format="GLB")


def glb_json(path):
    with open(path, "rb") as fh:
        data = fh.read()
    length = struct.unpack_from("<I", data, 12)[0]
    return json.loads(data[20:20 + length])


def main():
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "dummy.glb")
    build_dummy(src)
    bpy.ops.wm.read_factory_settings(use_empty=True)

    r = P.run_step("import", {"path": src, "target_faces": 3000, "height": 1.0})
    print("import:", r)
    assert r["faces"] <= 3200 and abs(r["dimensions"][2] - 1.0) < 0.01

    r = P.run_step("rig")
    print("rig:", r)
    assert {"hips", "spine", "head", "leg.L", "arm.R"} <= set(r["bones"])
    assert r["has_legs"] and 0.15 < r["hip_height"] < 0.35, r["hip_height"]

    r = P.run_step("animate", {"kinds": ["idle", "walk", "jump", "wave"]})
    print("animate:", r)

    out = os.path.join(tmp, "out.glb")
    r = P.run_step("export", {"path": out})
    print("export:", r)
    gltf = glb_json(out)
    names = sorted(a["name"] for a in gltf.get("animations", []))
    print("animations dans le GLB:", names, "| skins:", len(gltf.get("skins", [])))
    assert names == ["Idle", "Jump", "Walk", "Wave"], names
    assert gltf.get("skins")

    # La marche déforme bien le maillage ?
    char = bpy.data.objects[P.CHAR_NAME]
    rig = bpy.data.objects[P.RIG_NAME]
    rig.animation_data.action = bpy.data.actions["Walk"]
    P._assign_slot(rig.animation_data, bpy.data.actions["Walk"])
    deps = bpy.context.evaluated_depsgraph_get()

    def low_points(frame):
        bpy.context.scene.frame_set(frame)
        ev = char.evaluated_get(deps)
        return [tuple(round(c, 4) for c in v.co) for v in ev.data.vertices if v.co.z < 0.2][:50]

    assert low_points(0) != low_points(8), "les jambes ne bougent pas"
    print("OK — pipeline complet")


if __name__ == "__main__":
    main()
