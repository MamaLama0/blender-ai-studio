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
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Fichier introuvable : {path}")
    _ensure_object_mode()

    # On importe AVANT de vider la scène : si le fichier est illisible, rien n'est perdu.
    before = set(bpy.data.objects)
    old_actions = set(bpy.data.actions)
    try:
        IMPORTERS[ext](path)
    except RuntimeError as exc:
        detail = str(exc).strip().splitlines()[-1] if str(exc).strip() else ""
        raise RuntimeError(f"Blender n'arrive pas à lire ce fichier ({os.path.basename(path)}). {detail}") from exc
    new_objects = [o for o in bpy.data.objects if o not in before]
    meshes = [o for o in new_objects if o.type == "MESH"]
    if not meshes:
        for obj in new_objects:
            bpy.data.objects.remove(obj, do_unlink=True)
        raise RuntimeError("Le fichier ne contient aucun maillage.")

    if clear_scene:
        for obj in before:
            bpy.data.objects.remove(obj, do_unlink=True)
        for action in old_actions:
            bpy.data.actions.remove(action)

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
class _Sections:
    """Coupes horizontales du maillage (intersection triangles / plan z = cste).

    Plus fiable que de compter les sommets : un maillage décimé a de grandes faces
    sans sommet au milieu (jambes droites, flancs plats…).
    """

    def __init__(self, obj):
        import numpy as np

        self.np = np
        mesh = obj.data
        mesh.calc_loop_triangles()
        co = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
        mesh.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        mat = np.array(obj.matrix_world)
        co = co @ mat[:3, :3].T + mat[:3, 3]
        tri = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int64)
        mesh.loop_triangles.foreach_get("vertices", tri)
        self.tris = co[tri.reshape(-1, 3)]          # (T, 3 sommets, xyz)
        self.co = co
        self.zmin, self.zmax = co[:, 2].min(), co[:, 2].max()
        self.height = self.zmax - self.zmin
        self.cx = (co[:, 0].min() + co[:, 0].max()) / 2

    def segments(self, z):
        np = self.np
        pts = []
        for a, b in ((0, 1), (1, 2), (2, 0)):
            pa, pb = self.tris[:, a], self.tris[:, b]
            da, db = pa[:, 2] - z, pb[:, 2] - z
            cross = da * db < 0
            t = np.where(cross, da / np.where(cross, da - db, 1), np.nan)
            pts.append(pa[:, :2] + (pb[:, :2] - pa[:, :2]) * t[:, None])
        pts = np.stack(pts, axis=1)                  # (T, 3, 2), NaN si pas de croisement
        ok = ~np.isnan(pts[:, :, 0])
        keep = ok.sum(axis=1) == 2
        pts, ok = pts[keep], ok[keep]
        order = np.argsort(~ok, axis=1, kind="stable")[:, :2]
        return np.take_along_axis(pts, order[:, :, None], axis=1)  # (S, 2 points, xy)

    def profile(self, frac):
        """À la hauteur relative `frac` : centre dedans ?, demi-largeur du tronc, étendue."""
        np = self.np
        seg = self.segments(self.zmin + frac * self.height)
        if len(seg) < 3:
            return None
        flat = seg.reshape(-1, 2)
        cy = (flat[:, 1].min() + flat[:, 1].max()) / 2
        p, q = seg[:, 0], seg[:, 1]
        hits = (p[:, 1] - cy) * (q[:, 1] - cy) < 0
        xs = p[hits, 0] + (cy - p[hits, 1]) * (q[hits, 0] - p[hits, 0]) / (q[hits, 1] - p[hits, 1])
        right = np.sort(xs[xs > self.cx])
        left = np.sort(xs[xs < self.cx])[::-1]
        inside = len(right) % 2 == 1
        if inside and len(right) and len(left):
            torso = (right[0] - self.cx + self.cx - left[0]) / 2
        else:
            torso = 0.0
        return {
            "inside": inside,
            "torso": torso,
            "extent": float(np.abs(flat[:, 0] - self.cx).max()),
            "left_x": float(flat[flat[:, 0] < self.cx, 0].mean()) if (flat[:, 0] < self.cx).any() else None,
            "right_x": float(flat[flat[:, 0] > self.cx, 0].mean()) if (flat[:, 0] > self.cx).any() else None,
            "cy": float(cy),
        }


def _analyze_shape(obj):
    sec = _Sections(obj)
    zmin, h, cx = sec.zmin, sec.height, sec.cx
    co = sec.co

    # Jambes : en montant depuis le sol, le centre est « dehors » (entre les jambes)
    # jusqu'à l'entrejambe. Les hanches sont juste au-dessus.
    fracs = [0.03 + i * 0.01 for i in range(48)]          # 3 % → 50 % de la hauteur
    profiles = {f: sec.profile(f) for f in fracs}
    last_gap = None
    for f in fracs:
        pr = profiles[f]
        if pr is None:
            continue
        if not pr["inside"] and pr["left_x"] is not None and pr["right_x"] is not None:
            last_gap = f
        elif last_gap is not None and pr["inside"]:
            break
    has_legs = last_gap is not None and last_gap >= 0.05
    hip_frac = (last_gap + 0.02) if has_legs else 0.12
    hip_z = zmin + hip_frac * h

    # Tronc : demi-largeur au milieu du corps (première paroi rencontrée depuis le centre,
    # donc les bras écartés ne comptent pas)
    torso_vals = sorted(p["torso"] for f in (0.35, 0.4, 0.45, 0.5, 0.55)
                        if (p := sec.profile(max(f, hip_frac + 0.05))) and p["torso"] > 0)
    torso_half = torso_vals[len(torso_vals) // 2] if torso_vals else float(abs(co[:, 0] - cx).max()) / 2

    # Cou : rétrécissement suivi d'une tête plus large au-dessus (sinon 70 % de la hauteur)
    neck_frac = 0.70
    widths = {}
    for i in range(56, 93):
        pr = sec.profile(i / 100)
        widths[i] = pr["torso"] if pr and pr["torso"] > 0 else None
    best = None
    for i in range(58, 86):
        w = widths.get(i)
        above = [widths[j] for j in range(i + 2, i + 13) if widths.get(j)]
        if not w or len(above) < 5:
            continue
        bulge = sum(above) / len(above) / w
        if w < torso_half * 0.85 and bulge > 1.15 and (best is None or w < best[1]):
            best = (i, w)
    if best:
        neck_frac = best[0] / 100
    neck_z = zmin + neck_frac * h

    # Bras : point le plus écarté entre les hanches et un peu au-dessus du cou
    mask = (co[:, 2] > hip_z) & (co[:, 2] < neck_z + 0.05 * h)
    hands = {"L": None, "R": None}
    for key, sign in (("L", 1), ("R", -1)):
        side = co[mask & ((co[:, 0] - cx) * sign > 0)]
        if len(side):
            far = side[abs(side[:, 0] - cx).argmax()]
            if abs(far[0] - cx) > torso_half * 1.25:
                hands[key] = Vector(far)

    # Pieds : centre des parties gauche/droite tout en bas
    bottom = sec.profile(0.03) or {}
    feet_x = {
        "L": bottom.get("right_x") if bottom.get("right_x") is not None else cx + torso_half * 0.5,
        "R": bottom.get("left_x") if bottom.get("left_x") is not None else cx - torso_half * 0.5,
    }
    cy = bottom.get("cy", float(co[:, 1].mean())) if has_legs else float((co[:, 1].min() + co[:, 1].max()) / 2)
    mid = sec.profile(0.5)
    if mid:
        cy = mid["cy"]

    return {
        "zmin": zmin, "zmax": sec.zmax, "height": h, "cx": cx, "cy": cy,
        "hip_z": hip_z, "neck_z": neck_z, "has_legs": has_legs, "hands": hands,
        "torso_half": torso_half, "feet_x": feet_x,
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

    s = _analyze_shape(char)
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
        # Pivot de la jambe assez haut pour que le pied balance visiblement,
        # même quand les pattes sont minuscules (pingouin, peluche…).
        top = max(hip_z, s["zmin"] + h * 0.2)
        bone(f"leg.{key}", (leg_x, cy, top), (fx, cy, s["zmin"] + h * 0.03), "hips")
        hand = s["hands"][key]
        start_x = cx + sign * s["torso_half"] * 0.55
        if hand is not None:
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
        "has_legs": bool(s["has_legs"]),
        "hip_height": round(float(hip_z), 3),
        "neck_height": round(float(neck_z), 3),
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
        # Même signe des deux côtés = mouvement symétrique (les axes X locaux sont en miroir)
        for side in ("L", "R"):
            _key(rig.pose.bones[f"arm.{side}"], f, rot=(math.sin(t) * 0.05 * intensity, 0, 0))
    return n


def _anim_walk(rig, h, intensity):
    """Marche « dandinée » sur place, boucle de 32 images."""
    n = 32
    swing = 0.45 * intensity
    for f in range(0, n + 1, 2):
        t = 2 * math.pi * f / n
        s = math.sin(t)
        # Le pied qui avance se soulève (l'os pointe vers le bas : -Y local = vers le haut)
        lift = 0.06 * h * intensity
        _key(rig.pose.bones["leg.L"], f, rot=(s * swing, 0, 0), loc=(0, -max(0.0, s) * lift, 0))
        _key(rig.pose.bones["leg.R"], f, rot=(-s * swing, 0, 0), loc=(0, -max(0.0, -s) * lift, 0))
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
        _key(rig.pose.bones["arm.L"], f, rot=(arms * intensity, 0, 0))
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
        # Chaque action devient une « take » FBX (Unity/Unreal les voient comme des clips)
        bpy.ops.export_scene.fbx(filepath=path, bake_anim=True, add_leaf_bones=False,
                                 bake_anim_use_nla_strips=False, bake_anim_use_all_actions=True,
                                 bake_anim_force_startend_keying=True)
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
