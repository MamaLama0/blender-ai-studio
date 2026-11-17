"""Contrôle visuel du rig : fabrique des personnages « type IA » (un seul maillage
remaillé), les passe dans le pipeline, et rend une planche d'images par animation.

python tests/visual_check.py OUT_DIR [penguin|humanoid|blob]
"""
import math
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "blender_ai_studio", "blender_side"))

import bpy  # noqa: E402
from mathutils import Vector  # noqa: E402

import bas_pipeline as P  # noqa: E402


def _ellipsoid(loc, scale, rot=(0, 0, 0)):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16, location=loc, rotation=rot)
    bpy.context.object.scale = scale
    return bpy.context.object


def _cyl(loc, r, depth, rot=(0, 0, 0)):
    bpy.ops.mesh.primitive_cylinder_add(vertices=32, radius=r, depth=depth, location=loc, rotation=rot)
    return bpy.context.object


def build(kind, path):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    if kind == "penguin":  # le pingouin cowboy du tweet : corps-tête d'un seul bloc
        _ellipsoid((0, 0, 0.9), (0.55, 0.45, 0.75))
        _ellipsoid((0, -0.38, 1.15), (0.12, 0.08, 0.05))          # bec / moustache
        for s in (-1, 1):
            _ellipsoid((s * 0.55, 0, 0.85), (0.08, 0.18, 0.32), rot=(0, s * 0.35, 0))  # ailerons
            _ellipsoid((s * 0.22, -0.12, 0.08), (0.14, 0.22, 0.07))  # pieds
            _cyl((s * 0.22, 0, 0.22), 0.07, 0.3)                       # pattes
        _cyl((0, 0, 1.68), 0.42, 0.04)                                 # bord du chapeau
        _cyl((0, 0, 1.82), 0.22, 0.28)                                 # chapeau
    elif kind == "humanoid":  # bonhomme en T-pose
        _ellipsoid((0, 0, 1.15), (0.25, 0.16, 0.35))
        _ellipsoid((0, 0, 1.7), (0.17, 0.17, 0.2))
        _cyl((0, 0, 1.47), 0.07, 0.15)
        for s in (-1, 1):
            _cyl((s * 0.55, 0, 1.38), 0.06, 0.6, rot=(0, math.pi / 2, 0))
            _cyl((s * 0.13, 0, 0.42), 0.09, 0.84)
            _ellipsoid((s * 0.13, -0.06, 0.03), (0.09, 0.15, 0.04))
    else:  # boule sans bras ni jambes
        _ellipsoid((0, 0, 0.6), (0.6, 0.55, 0.6))
        for s in (-1, 1):
            _ellipsoid((s * 0.2, -0.45, 0.8), (0.1, 0.05, 0.12))

    bpy.ops.object.select_all(action="SELECT")
    bpy.context.view_layer.objects.active = bpy.context.selected_objects[0]
    bpy.ops.object.convert(target="MESH")
    bpy.ops.object.join()
    obj = bpy.context.object
    mod = obj.modifiers.new("Remesh", "REMESH")  # comme une sortie d'IA : un seul maillage dense
    mod.mode = "VOXEL"
    mod.voxel_size = 0.025
    bpy.ops.object.modifier_apply(modifier=mod.name)
    bpy.ops.export_scene.gltf(filepath=path, export_format="GLB")


def setup_render():
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.display.shading.light = "STUDIO"
    scene.display.shading.color_type = "OBJECT"
    scene.render.resolution_x = scene.render.resolution_y = 320
    scene.render.film_transparent = False
    char = bpy.data.objects[P.CHAR_NAME]
    char.color = (0.38, 0.7, 0.68, 1)
    rig = bpy.data.objects[P.RIG_NAME]
    rig.show_in_front = True
    cam_data = bpy.data.cameras.new("Cam")
    cam = bpy.data.objects.new("Cam", cam_data)
    scene.collection.objects.link(cam)
    cam.location = Vector((1.9, -2.6, 1.0))
    direction = Vector((0, 0, 0.5)) - cam.location
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    cam_data.lens = 75
    scene.camera = cam
    if scene.world is None:
        scene.world = bpy.data.worlds.new("W")


def render_sheet(out_dir, kind, actions, frames=6):
    from PIL import Image

    rig = bpy.data.objects[P.RIG_NAME]
    scene = bpy.context.scene
    rows = []
    for name in actions:
        act = bpy.data.actions[name]
        rig.animation_data.action = act
        P._assign_slot(rig.animation_data, act)
        start, end = act.frame_range
        row = []
        for i in range(frames):
            f = int(start + (end - start) * i / frames)
            scene.frame_set(f)
            fp = os.path.join(tempfile.gettempdir(), f"vc_{name}_{i}.png")
            scene.render.filepath = fp
            bpy.ops.render.render(write_still=True)
            row.append(Image.open(fp).convert("RGB"))
        rows.append(row)
    w, h = rows[0][0].size
    sheet = Image.new("RGB", (w * frames, h * len(rows)), "white")
    for r, row in enumerate(rows):
        for c, img in enumerate(row):
            sheet.paste(img, (c * w, r * h))
    out = os.path.join(out_dir, f"sheet-{kind}.png")
    sheet.save(out)
    return out


def main():
    out_dir = sys.argv[1]
    kinds = sys.argv[2:] or ["penguin"]
    os.makedirs(out_dir, exist_ok=True)
    for kind in kinds:
        src = os.path.join(tempfile.gettempdir(), f"{kind}.glb")
        build(kind, src)
        bpy.ops.wm.read_factory_settings(use_empty=True)
        print(kind, "import", P.run_step("import", {"path": src, "target_faces": 15000}))
        print(kind, "rig", P.run_step("rig"))
        print(kind, "anim", P.run_step("animate", {"kinds": ["idle", "walk", "jump", "wave"]}))
        setup_render()
        print(kind, "->", render_sheet(out_dir, kind, ["Walk", "Jump", "Wave"]))


if __name__ == "__main__":
    main()
