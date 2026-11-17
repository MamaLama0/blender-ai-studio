"""Pont Blender AI Studio — à charger DANS Blender.

Deux façons de l'utiliser :
  1. Automatique : l'application lance `blender --python bas_bridge.py` et le pont
     démarre tout seul.
  2. Comme add-on : Édition > Préférences > Add-ons > Installer depuis le disque,
     puis panneau « AI Studio » dans la barre latérale (touche N) de la vue 3D.

Le protocole (JSON {"type": ..., "params": {...}} sur TCP 127.0.0.1:9876) est le
même que celui de l'add-on blender-mcp : le serveur MCP de blender-mcp peut donc
aussi parler à ce pont (Claude Desktop, Claude Code, Cursor...).

Sécurité : le pont n'écoute QUE sur 127.0.0.1 et exécute le code qu'on lui envoie.
Ne l'expose jamais sur le réseau.
"""

bl_info = {
    "name": "Blender AI Studio Bridge",
    "author": "blender-ai-studio",
    "version": (0, 1, 0),
    "blender": (3, 6, 0),
    "location": "Vue 3D > Barre latérale > AI Studio",
    "description": "Laisse Blender AI Studio (ou un serveur MCP) piloter Blender en local",
    "category": "Interface",
}

import io
import json
import os
import queue
import socket
import tempfile
import threading
import traceback
from contextlib import redirect_stdout

import bpy

HOST = "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("BAS_BRIDGE_PORT", "9876"))

_server = None


# --------------------------------------------------------------------------
# Commandes (exécutées sur le thread principal de Blender)
# --------------------------------------------------------------------------
def cmd_get_scene_info(params):
    scene = bpy.context.scene
    objects = []
    for obj in scene.objects[:50]:
        info = {
            "name": obj.name,
            "type": obj.type,
            "location": [round(v, 3) for v in obj.location],
            "dimensions": [round(v, 3) for v in obj.dimensions],
        }
        if obj.type == "MESH":
            info["faces"] = len(obj.data.polygons)
        if obj.type == "ARMATURE":
            info["bones"] = [b.name for b in obj.data.bones]
        objects.append(info)
    return {
        "name": scene.name,
        "object_count": len(scene.objects),
        "objects": objects,
        "frame_range": [scene.frame_start, scene.frame_end],
        "fps": scene.render.fps,
        "actions": [a.name for a in bpy.data.actions],
        "file": bpy.data.filepath,
    }


def cmd_get_object_info(params):
    obj = bpy.data.objects.get(params.get("name", ""))
    if obj is None:
        raise ValueError(f"Objet introuvable : {params.get('name')}")
    info = {
        "name": obj.name,
        "type": obj.type,
        "location": list(obj.location),
        "rotation": list(obj.rotation_euler),
        "scale": list(obj.scale),
        "dimensions": list(obj.dimensions),
        "parent": obj.parent.name if obj.parent else None,
        "materials": [s.material.name for s in obj.material_slots if s.material],
        "modifiers": [m.type for m in obj.modifiers],
    }
    if obj.type == "MESH":
        info["vertices"] = len(obj.data.vertices)
        info["faces"] = len(obj.data.polygons)
        info["vertex_groups"] = [g.name for g in obj.vertex_groups]
    return info


def cmd_execute_code(params):
    code = params.get("code", "")
    buffer = io.StringIO()
    namespace = {"bpy": bpy, "__name__": "__bas__"}
    with redirect_stdout(buffer):
        exec(compile(code, "<ai-studio>", "exec"), namespace)
    return {"executed": True, "result": buffer.getvalue()[-20000:]}


def _view3d_override():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                for region in area.regions:
                    if region.type == "WINDOW":
                        return window, area, region
    return None


def cmd_get_viewport_screenshot(params):
    target = _view3d_override()
    if target is None:
        raise RuntimeError("Aucune vue 3D ouverte (Blender lancé sans interface ?)")
    window, area, region = target
    path = params.get("filepath") or os.path.join(tempfile.gettempdir(), "bas_viewport.png")
    with bpy.context.temp_override(window=window, area=area, region=region):
        bpy.ops.screen.screenshot_area(filepath=path)
    max_size = int(params.get("max_size", 800))
    img = bpy.data.images.load(path)
    w, h = img.size
    if max(w, h) > max_size:
        ratio = max_size / max(w, h)
        img.scale(int(w * ratio), int(h * ratio))
        img.save()
    size = list(img.size)
    bpy.data.images.remove(img)
    return {"filepath": path, "width": size[0], "height": size[1]}


def cmd_save_file(params):
    path = params.get("filepath") or bpy.data.filepath
    if not path:
        raise ValueError("Aucun chemin de fichier")
    bpy.ops.wm.save_as_mainfile(filepath=path)
    return {"saved": path}


def cmd_open_file(params):
    bpy.ops.wm.open_mainfile(filepath=params["filepath"])
    return {"opened": params["filepath"]}


def cmd_ping(params):
    return {"pong": True, "blender": bpy.app.version_string}


def _disabled(name):
    # Le serveur MCP de blender-mcp interroge ces intégrations : on répond « désactivé ».
    return lambda params: {"enabled": False, "message": f"{name} n'est pas géré par ce pont."}


COMMANDS = {
    "get_polyhaven_status": _disabled("Poly Haven"),
    "get_hyper3d_status": _disabled("Hyper3D"),
    "get_sketchfab_status": _disabled("Sketchfab"),
    "get_hunyuan3d_status": _disabled("Hunyuan3D"),
    "ping": cmd_ping,
    "get_scene_info": cmd_get_scene_info,
    "get_object_info": cmd_get_object_info,
    "execute_code": cmd_execute_code,
    "get_viewport_screenshot": cmd_get_viewport_screenshot,
    "save_file": cmd_save_file,
    "open_file": cmd_open_file,
}


# --------------------------------------------------------------------------
# Serveur TCP (thread) + exécution sur le thread principal (timer)
# --------------------------------------------------------------------------
class BridgeServer:
    def __init__(self, port=DEFAULT_PORT):
        self.port = port
        self.running = False
        self.sock = None
        self.jobs = queue.Queue()

    def start(self):
        if self.running:
            return
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((HOST, self.port))
        self.sock.listen(4)
        self.sock.settimeout(1.0)
        self.running = True
        threading.Thread(target=self._accept_loop, daemon=True).start()
        if not bpy.app.timers.is_registered(self._pump):
            bpy.app.timers.register(self._pump, persistent=True)
        print(f"[AI Studio] Pont actif sur {HOST}:{self.port}")

    def stop(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        if bpy.app.timers.is_registered(self._pump):
            bpy.app.timers.unregister(self._pump)
        print("[AI Studio] Pont arrêté")

    # thread réseau -------------------------------------------------------
    def _accept_loop(self):
        while self.running:
            try:
                client, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._client_loop, args=(client,), daemon=True).start()

    def _client_loop(self, client):
        client.settimeout(None)
        buffer = b""
        try:
            while self.running:
                chunk = client.recv(65536)
                if not chunk:
                    break
                buffer += chunk
                try:
                    request = json.loads(buffer.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue  # message incomplet, on attend la suite
                buffer = b""
                done = threading.Event()
                box = {}
                self.jobs.put((request, box, done))
                done.wait()
                client.sendall(json.dumps(box["response"]).encode("utf-8"))
        except OSError:
            pass
        finally:
            client.close()

    # thread principal ------------------------------------------------------
    def _pump(self):
        while not self.jobs.empty():
            request, box, done = self.jobs.get()
            kind = request.get("type")
            handler = COMMANDS.get(kind)
            try:
                if handler is None:
                    raise ValueError(f"Commande inconnue : {kind}")
                box["response"] = {"status": "success", "result": handler(request.get("params") or {})}
            except Exception as exc:  # renvoyé au client, Blender ne plante pas
                box["response"] = {
                    "status": "error",
                    "message": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc()[-4000:],
                }
            done.set()
        return 0.05


# --------------------------------------------------------------------------
# Interface (panneau N) quand c'est installé comme add-on
# --------------------------------------------------------------------------
class BAS_OT_toggle(bpy.types.Operator):
    bl_idname = "bas.toggle_bridge"
    bl_label = "Démarrer / arrêter le pont"

    def execute(self, context):
        global _server
        if _server and _server.running:
            _server.stop()
            _server = None
        else:
            _server = BridgeServer(DEFAULT_PORT)
            _server.start()
        return {"FINISHED"}


class BAS_PT_panel(bpy.types.Panel):
    bl_label = "AI Studio"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "AI Studio"

    def draw(self, context):
        running = bool(_server and _server.running)
        self.layout.label(text=f"Pont : {'actif' if running else 'arrêté'} (port {DEFAULT_PORT})")
        self.layout.operator(BAS_OT_toggle.bl_idname, text="Arrêter" if running else "Démarrer")


_classes = (BAS_OT_toggle, BAS_PT_panel)


def register():
    for cls in _classes:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            pass  # déjà enregistré


def unregister():
    global _server
    if _server:
        _server.stop()
        _server = None
    for cls in reversed(_classes):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass


if __name__ == "__main__":
    # Lancé via `blender --python bas_bridge.py` : on démarre directement.
    register()
    _server = BridgeServer(DEFAULT_PORT)
    try:
        _server.start()
    except OSError as exc:
        print(f"[AI Studio] Impossible d'ouvrir le port {DEFAULT_PORT} : {exc}")
