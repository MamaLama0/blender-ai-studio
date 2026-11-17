"""Étapes du pipeline, exécutées DANS Blender (en direct via le pont, ou en arrière-plan).

Chaque étape est une fonction `step_<nom>(**args) -> dict`. Point d'entrée commun :
`run_step(nom, args)`.
"""
from __future__ import annotations

import math
import os

import bmesh
import bpy
from mathutils import Vector

CHAR_NAME = "Character"
RIG_NAME = "Character_Rig"
FPS = 30


# --------------------------------------------------------------------------
# Utilitaires
# --------------------------------------------------------------------------
def _deselect_all():
    for obj in bpy.context.view_layer.objects:
        obj.select_set(False)


def _activate(obj, select_only=True):
    if select_only:
        _deselect_all()
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def _ensure_object_mode():
    if bpy.context.object and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")


def _character():
    obj = bpy.data.objects.get(CHAR_NAME)
    if obj is None:
        meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
        if not meshes:
            raise RuntimeError("Aucun personnage dans la scène : importe ou génère un modèle d'abord.")
        obj = max(meshes, key=lambda o: len(o.data.polygons))
    return obj


def _rig():
    rig = bpy.data.objects.get(RIG_NAME)
    if rig is None:
        raise RuntimeError("Pas de squelette : lance l'étape « Rig » d'abord.")
    return rig


def _world_coords(obj):
    mat = obj.matrix_world
    return [mat @ v.co for v in obj.data.vertices]


# --------------------------------------------------------------------------
# 1. Import + nettoyage
# --------------------------------------------------------------------------
IMPORTERS = {
    ".glb": lambda p: bpy.ops.import_scene.gltf(filepath=p),
    ".gltf": lambda p: bpy.ops.import_scene.gltf(filepath=p),
    ".obj": lambda p: bpy.ops.wm.obj_import(filepath=p),
    ".fbx": lambda p: bpy.ops.import_scene.fbx(filepath=p),
    ".stl": lambda p: bpy.ops.wm.stl_import(filepath=p),
    ".ply": lambda p: bpy.ops.wm.ply_import(filepath=p),
}


def step_import(path, target_faces=20000, height=1.0, clear_scene=True):
    ext = os.path.splitext(path)[1].lower()
    if ext not in IMPORTERS:
        raise ValueError(f"Format non géré : {ext} (glb, gltf, obj, fbx, stl, ply)")
    _ensure_object_mode()

    if clear_scene:
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        for action in list(bpy.data.actions):
            bpy.data.actions.remove(action)

    before = set(bpy.data.objects)
    IMPORTERS[ext](path)
    new_objects = [o for o in bpy.data.objects if o not in before]
    meshes = [o for o in new_objects if o.type == "MESH"]
    if not meshes:
        raise RuntimeError("Le fichier ne contient aucun maillage.")

    # On garde les maillages, on retire les vides/caméras/os importés
    for obj in new_objects:
        if obj.type != "MESH":
            for child in obj.children:
                mat = child.matrix_world.copy()
                child.parent = None
                child.matrix_world = mat
    for obj in new_objects:
        if obj.type != "MESH":
            bpy.data.objects.remove(obj, do_unlink=True)

    # Applique les transformations et fusionne en un seul objet
    _deselect_all()
    for obj in meshes:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    if len(meshes) > 1:
        bpy.ops.object.join()
    char = bpy.context.view_layer.objects.active
    char.name = CHAR_NAME
    char.data.name = CHAR_NAME + "_Mesh"

    stats = {"faces_before": len(char.data.polygons)}
    clean_mesh(char)

    if target_faces and len(char.data.polygons) > target_faces:
        mod = char.modifiers.new("Decimate", "DECIMATE")
        mod.ratio = max(0.01, target_faces / len(char.data.polygons))
        _activate(char)
        bpy.ops.object.modifier_apply(modifier=mod.name)
        clean_mesh(char)

    normalize(char, height)
    _shade_smooth(char)
    stats.update(_mesh_stats(char))
    return stats


def clean_mesh(obj):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    size = max(obj.dimensions) or 1.0
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=size * 1e-5)
    loose = [v for v in bm.verts if not v.link_edges]
    bmesh.ops.delete(bm, geom=loose, context="VERTS")
    degenerate = [f for f in bm.faces if f.calc_area() <= 0.0]
    bmesh.ops.delete(bm, geom=degenerate, context="FACES")
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.validate(clean_customdata=False)
    obj.data.update()


def normalize(obj, height=1.0):
    """Pieds au sol (z=0), centré en x/y, hauteur fixée."""
    coords = _world_coords(obj)
    zmin = min(c.z for c in coords)
    zmax = max(c.z for c in coords)
    cx = (min(c.x for c in coords) + max(c.x for c in coords)) / 2
    cy = (min(c.y for c in coords) + max(c.y for c in coords)) / 2
    current = (zmax - zmin) or 1.0
    factor = height / current if height else 1.0
    offset = Vector((-cx, -cy, -zmin))
    for v in obj.data.vertices:
        v.co = (v.co + offset) * factor
    obj.location = (0, 0, 0)
    obj.data.update()


def _shade_smooth(obj):
    values = [True] * len(obj.data.polygons)
    obj.data.polygons.foreach_set("use_smooth", values)
    _activate(obj)
    try:
        bpy.ops.object.shade_smooth_by_angle(angle=math.radians(40))
    except (AttributeError, RuntimeError):
        pass


def _mesh_stats(obj):
    return {
        "vertices": len(obj.data.vertices),
        "faces": len(obj.data.polygons),
        "dimensions": [round(d, 3) for d in obj.dimensions],
    }


# --------------------------------------------------------------------------
# 2. Auto-rig (heuristique, pensé pour les personnages type « toon » / bipèdes)
# --------------------------------------------------------------------------
def _analyze_shape(coords, slices=24):
    xs = [c.x for c in coords]
    zs = [c.z for c in coords]
    zmin, zmax = min(zs), max(zs)
    xmin, xmax = min(xs), max(xs)
    height = zmax - zmin
    width = xmax - xmin
    cx = (xmin + xmax) / 2
    cy = sum(c.y for c in coords) / len(coords)
    step = height / slices

    bands = [[] for _ in range(slices)]
    for c in coords:
        idx = min(slices - 1, int((c.z - zmin) / step))
        bands[idx].append(c)

    half_widths, gaps = [], []
    for band in bands:
        if len(band) < 8:  # trop peu de sommets pour conclure (maillage peu dense)
            half_widths.append(0.0)
            gaps.append(None)
            continue
        dx = sorted(abs(c.x - cx) for c in band)
        half_widths.append(dx[int(len(dx) * 0.95) - 1])
        near_center = sum(1 for d in dx if d < width * 0.05)
        gaps.append(near_center / len(dx) < 0.02)

    # Jambes : bandes basses où le centre est vide (espace entre les jambes)
    # → les hanches sont à la première bande « pleine » au-dessus de cet espace.
    hip_band = 0
    seen_gap = False
    for i in range(int(slices * 0.5)):
        if gaps[i] is None:
            continue
        if gaps[i]:
            seen_gap = True
            hip_band = i + 1
        elif seen_gap:
            hip_band = i
            break
    has_legs = hip_band > 0
    hip_z = zmin + max(hip_band, int(slices * 0.12)) * step

    # Cou : bande la plus étroite dans la moitié haute (sinon 70 % de la hauteur)
    upper = range(int(slices * 0.5), int(slices * 0.9))
    neck_band = min(upper, key=lambda i: half_widths[i] or 1e9)
    torso_ref = max(half_widths[int(slices * 0.3):int(slices * 0.6)] or [width / 2])
    if half_widths[neck_band] > torso_ref * 0.85:
        neck_z = zmin + height * 0.70
    else:
        neck_z = zmin + (neck_band + 0.5) * step

    # Bras : point le plus écarté entre hanches et cou
    side = {"L": None, "R": None}
    for c in coords:
        if hip_z < c.z < neck_z:
            key = "L" if c.x > cx else "R"
            if side[key] is None or abs(c.x - cx) > abs(side[key].x - cx):
                side[key] = c
    torso_half = sorted(half_widths[int(slices * 0.3):int(slices * 0.6)] or [width / 4])
    torso_half = torso_half[len(torso_half) // 2]

    feet_x = {}
    bottom = [c for c in coords if c.z < zmin + height * 0.08]
    for key, sign in (("L", 1), ("R", -1)):
        pts = [c.x for c in bottom if (c.x - cx) * sign > 0]
        feet_x[key] = sum(pts) / len(pts) if pts else cx + sign * width * 0.18

    return {
        "zmin": zmin, "zmax": zmax, "height": height, "width": width,
        "cx": cx, "cy": cy, "hip_z": hip_z, "neck_z": neck_z,
        "has_legs": has_legs, "hands": side, "torso_half": torso_half, "feet_x": feet_x,
    }


def step_rig():
    _ensure_object_mode()
    char = _character()
    old = bpy.data.objects.get(RIG_NAME)
    if old:
        bpy.data.objects.remove(old, do_unlink=True)
    for mod in [m for m in char.modifiers if m.type == "ARMATURE"]:
        char.modifiers.remove(mod)
    char.vertex_groups.clear()
    char.parent = None

    s = _analyze_shape(_world_coords(char))
    h, cx, cy = s["height"], s["cx"], s["cy"]
    hip_z, neck_z = s["hip_z"], s["neck_z"]
    spine_mid = hip_z + (neck_z - hip_z) * 0.5
    shoulder_z = neck_z - h * 0.06
    fwd = Vector((0, -1, 0))  # l'avant du personnage (convention glTF → Blender)

    arm_data = bpy.data.armatures.new(RIG_NAME + "_Data")
    rig = bpy.data.objects.new(RIG_NAME, arm_data)
    bpy.context.scene.collection.objects.link(rig)
    arm_data.display_type = "STICK"
    rig.show_in_front = True

    _activate(rig)
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm_data.edit_bones

    def bone(name, head, tail, parent=None, deform=True, roll_to=fwd):
        b = eb.new(name)
        b.head, b.tail = Vector(head), Vector(tail)
        b.align_roll(roll_to)
        b.use_deform = deform
        if parent:
            b.parent = eb[parent]
        return b

    bone("root", (cx, cy, 0), (cx, cy - h * 0.15, 0), deform=False, roll_to=Vector((0, 0, 1)))
    bone("hips", (cx, cy, hip_z), (cx, cy, spine_mid), "root")
    bone("spine", (cx, cy, spine_mid), (cx, cy, neck_z), "hips")
    bone("head", (cx, cy, neck_z), (cx, cy, s["zmax"]), "spine")

    for key, sign in (("L", 1), ("R", -1)):
        fx = s["feet_x"][key]
        leg_x = cx + (fx - cx) * 0.9
        top = hip_z if s["has_legs"] else hip_z * 0.9
        bone(f"leg.{key}", (leg_x, cy, top), (fx, cy, s["zmin"] + h * 0.03), "hips")
        hand = s["hands"][key]
        start_x = cx + sign * s["torso_half"] * 0.55
        if hand is not None and abs(hand.x - cx) > s["torso_half"] * 0.9:
            tail = (hand.x, hand.y, hand.z)
        else:  # pas de bras visible : petit os latéral (ailerons, mains collées...)
            tail = (cx + sign * s["torso_half"] * 1.05, cy, shoulder_z - h * 0.12)
        bone(f"arm.{key}", (start_x, cy, shoulder_z), tail, "spine", roll_to=Vector((0, 0, 1)))

    bpy.ops.object.mode_set(mode="OBJECT")

    method = _bind(char, rig)
    char.data.validate(clean_customdata=False)  # ramène les poids 1.0000001 à 1.0
    rig.data.pose_position = "POSE"
    return {
        "bones": [b.name for b in rig.data.bones],
        "skinning": method,
        "has_legs": s["has_legs"],
        "hip_height": round(hip_z, 3),
        "neck_height": round(neck_z, 3),
    }


def _bind(char, rig):
    """Poids automatiques de Blender ; si ça échoue (maillage IA non étanche), poids par proximité."""
    _deselect_all()
    char.select_set(True)
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    try:
        bpy.ops.object.parent_set(type="ARMATURE_AUTO")
    except RuntimeError:
        pass
    total = len(char.data.vertices)
    weighted = sum(1 for v in char.data.vertices if any(g.weight > 0.001 for g in v.groups))
    if total and weighted / total > 0.97:
        return "automatique (heat)"

    # Repli : chaque sommet suit les 2 os les plus proches (pondération inverse distance)
    char.vertex_groups.clear()
    if char.parent != rig:
        mat = char.matrix_world.copy()
        char.parent = rig
        char.matrix_world = mat
    if not any(m.type == "ARMATURE" for m in char.modifiers):
        mod = char.modifiers.new("Armature", "ARMATURE")
        mod.object = rig
    segs = []
    for b in rig.data.bones:
        if b.use_deform:
            segs.append((b.name, rig.matrix_world @ b.head_local, rig.matrix_world @ b.tail_local))
    groups = {name: char.vertex_groups.new(name=name) for name, _, _ in segs}

    def dist(p, a, b):
        ab = b - a
        t = max(0.0, min(1.0, (p - a).dot(ab) / (ab.length_squared or 1e-9)))
        return (p - (a + ab * t)).length

    mat = char.matrix_world
    for v in char.data.vertices:
        p = mat @ v.co
        ranked = sorted(((dist(p, a, b), n) for n, a, b in segs))[:2]
        inv = [(1.0 / max(d, 1e-4) ** 2, n) for d, n in ranked]
        norm = sum(w for w, _ in inv)
        for w, n in inv:
            groups[n].add([v.index], w / norm, "REPLACE")
    return "proximité (repli)"


# --------------------------------------------------------------------------
# 3. Animations procédurales
# --------------------------------------------------------------------------
def _key(pb, frame, rot=None, loc=None):
    if rot is not None:
        pb.rotation_mode = "XYZ"
        pb.rotation_euler = rot
        pb.keyframe_insert("rotation_euler", frame=frame)
    if loc is not None:
        pb.location = loc
        pb.keyframe_insert("location", frame=frame)


def _reset_pose(rig):
    for pb in rig.pose.bones:
        pb.rotation_mode = "XYZ"
        pb.rotation_euler = (0, 0, 0)
        pb.location = (0, 0, 0)
        pb.scale = (1, 1, 1)


def _anim_idle(rig, h, intensity):
    n = 60
    for f in range(0, n + 1, 6):
        t = 2 * math.pi * f / n
        _key(rig.pose.bones["hips"], f, loc=(0, math.sin(t) * 0.01 * h * intensity, 0))
        _key(rig.pose.bones["spine"], f, rot=(math.sin(t) * 0.04 * intensity, 0, 0))
        _key(rig.pose.bones["head"], f, rot=(0, 0, math.sin(t / 2 + 1) * 0.06 * intensity))
        for side, sgn in (("L", 1), ("R", -1)):
            _key(rig.pose.bones[f"arm.{side}"], f, rot=(sgn * math.sin(t) * 0.05 * intensity, 0, 0))
    return n


def _anim_walk(rig, h, intensity):
    """Marche « dandinée » sur place, boucle de 32 images."""
    n = 32
    swing = 0.45 * intensity
    for f in range(0, n + 1, 2):
        t = 2 * math.pi * f / n
        s = math.sin(t)
        _key(rig.pose.bones["leg.L"], f, rot=(s * swing, 0, 0))
        _key(rig.pose.bones["leg.R"], f, rot=(-s * swing, 0, 0))
        _key(rig.pose.bones["hips"], f,
             rot=(0, 0, s * 0.12 * intensity),
             loc=(0, abs(math.cos(t)) * 0.03 * h * intensity, 0))
        _key(rig.pose.bones["spine"], f, rot=(0.05 * intensity, 0, -s * 0.08 * intensity))
        _key(rig.pose.bones["head"], f, rot=(0, 0, s * 0.05 * intensity))
        _key(rig.pose.bones["arm.L"], f, rot=(0, 0, -s * 0.35 * intensity))
        _key(rig.pose.bones["arm.R"], f, rot=(0, 0, -s * 0.35 * intensity))
    return n


def _anim_jump(rig, h, intensity):
    n = 30
    hips = rig.pose.bones["hips"]
    keys = [(0, 0.0, 0.0), (6, -0.08, 0.6), (12, 0.35, -0.5), (18, 0.30, -0.3),
            (23, -0.06, 0.5), (30, 0.0, 0.0)]
    for f, up, arms in keys:
        _key(hips, f, loc=(0, up * h * intensity, 0))
        _key(rig.pose.bones["arm.L"], f, rot=(-arms * intensity, 0, 0))
        _key(rig.pose.bones["arm.R"], f, rot=(arms * intensity, 0, 0))
        tuck = max(0.0, up) * 1.2 * intensity
        _key(rig.pose.bones["leg.L"], f, rot=(tuck, 0, 0))
        _key(rig.pose.bones["leg.R"], f, rot=(tuck, 0, 0))
        _key(rig.pose.bones["spine"], f, rot=(-up * 0.4, 0, 0))
    return n


def _anim_wave(rig, h, intensity):
    n = 48
    arm = rig.pose.bones["arm.R"]
    for f in range(0, n + 1, 4):
        lift = min(1.0, f / 8) * min(1.0, (n - f) / 8)
        wave = math.sin(2 * math.pi * f / 16) * 0.35 * lift
        _key(arm, f, rot=(1.3 * lift * intensity, 0, wave * intensity))
        _key(rig.pose.bones["head"], f, rot=(0, 0, -0.12 * lift * intensity))
        _key(rig.pose.bones["spine"], f, rot=(0, 0, 0.05 * lift * intensity))
    return n


ANIMATIONS = {"idle": _anim_idle, "walk": _anim_walk, "jump": _anim_jump, "wave": _anim_wave}
ANIM_LABELS = {"idle": "Idle", "walk": "Walk", "jump": "Jump", "wave": "Wave"}


def step_animate(kinds=("idle", "walk"), intensity=1.0):
    _ensure_object_mode()
    rig = _rig()
    h = _character().dimensions.z or 1.0
    scene = bpy.context.scene
    scene.render.fps = FPS
    if rig.animation_data is None:
        rig.animation_data_create()
    ad = rig.animation_data

    made = []
    longest = 1
    for kind in kinds:
        if kind not in ANIMATIONS:
            raise ValueError(f"Animation inconnue : {kind} ({', '.join(ANIMATIONS)})")
        name = ANIM_LABELS[kind]
        old = bpy.data.actions.get(name)
        if old:
            for track in list(ad.nla_tracks):
                if track.name == name:
                    ad.nla_tracks.remove(track)
            bpy.data.actions.remove(old)
        ad.action = None
        _reset_pose(rig)
        length = ANIMATIONS[kind](rig, h, intensity)
        action = ad.action
        action.name = name
        action.use_fake_user = True
        track = ad.nla_tracks.new()
        track.name = name
        track.strips.new(name, 1, action)
        track.mute = True
        ad.action = None
        made.append(name)
        longest = max(longest, length)

    # La première animation reste active pour la prévisualisation dans Blender
    first = bpy.data.actions.get(made[0])
    ad.action = first
    _assign_slot(ad, first)
    scene.frame_start, scene.frame_end = 0, longest
    _reset_pose(rig)
    return {"animations": made, "fps": FPS}


def _assign_slot(ad, action):
    """Blender 4.4+ : une action a des « slots » ; on assigne le premier."""
    slots = getattr(action, "slots", None)
    if slots and hasattr(ad, "action_slot") and len(slots):
        try:
            ad.action_slot = slots[0]
        except (TypeError, RuntimeError):
            pass


# --------------------------------------------------------------------------
# 4. Export
# --------------------------------------------------------------------------
def step_export(path, fmt="glb"):
    _ensure_object_mode()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    _deselect_all()
    if fmt in ("glb", "gltf"):
        kwargs = dict(
            filepath=path,
            export_format="GLB" if fmt == "glb" else "GLTF_SEPARATE",
            export_animations=True,
            export_apply=False,
        )
        try:
            bpy.ops.export_scene.gltf(export_animation_mode="ACTIONS", **kwargs)
        except TypeError:
            bpy.ops.export_scene.gltf(**kwargs)
    elif fmt == "fbx":
        bpy.ops.export_scene.fbx(filepath=path, bake_anim=True, add_leaf_bones=False,
                                 bake_anim_use_nla_strips=True, bake_anim_use_all_actions=False)
    elif fmt == "obj":
        bpy.ops.wm.obj_export(filepath=path)
    else:
        raise ValueError(f"Format d'export inconnu : {fmt}")
    return {"path": path, "size_kb": round(os.path.getsize(path) / 1024, 1)}


def step_info():
    out = {"objects": [o.name for o in bpy.context.scene.objects]}
    char = bpy.data.objects.get(CHAR_NAME)
    if char:
        out["character"] = _mesh_stats(char)
    rig = bpy.data.objects.get(RIG_NAME)
    if rig:
        out["bones"] = [b.name for b in rig.data.bones]
    out["animations"] = [a.name for a in bpy.data.actions]
    return out


STEPS = {
    "import": step_import,
    "rig": step_rig,
    "animate": step_animate,
    "export": step_export,
    "info": step_info,
}


def run_step(name, args=None):
    if name not in STEPS:
        raise ValueError(f"Étape inconnue : {name}")
    return STEPS[name](**(args or {}))
