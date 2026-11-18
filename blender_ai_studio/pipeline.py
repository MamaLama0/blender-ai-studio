"""Exécute les étapes du pipeline : dans le Blender ouvert (via le pont) s'il est
connecté, sinon dans un Blender invisible en arrière-plan."""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .bridge import BlenderBridge, BridgeError
from .config import Settings

BLENDER_SIDE = Path(__file__).parent / "blender_side"
MARK = "__BAS_RESULT__"


@dataclass
class Project:
    folder: Path

    @property
    def blend(self) -> Path:
        return self.folder / "projet.blend"

    @property
    def preview(self) -> Path:
        return self.folder / "apercu.glb"

    @property
    def exports(self) -> Path:
        path = self.folder / "exports"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def new(cls, workspace: Path, name: str | None = None) -> "Project":
        raw = (name or "").strip() or f"perso {time.strftime('%d-%m %Hh%M')}"
        slug = "".join(ch if ch.isalnum() or ch in "-_ " else "-" for ch in raw).strip()[:48] or "perso"
        folder, n = workspace / slug, 2
        while folder.exists():
            folder, n = workspace / f"{slug} ({n})", n + 1
        folder.mkdir(parents=True)
        return cls(folder)


class StepError(RuntimeError):
    pass


class Cancelled(StepError):
    """La personne a cliqué sur Annuler."""


class PipelineRunner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.bridge = BlenderBridge(settings.bridge_host, settings.bridge_port)
        self._proc: subprocess.Popen | None = None
        self._cancelled = False

    def cancel(self) -> bool:
        """Arrête le Blender d'arrière-plan en cours. Renvoie False si rien à arrêter."""
        proc = self._proc
        if proc and proc.poll() is None:
            self._cancelled = True
            proc.kill()
            return True
        return False

    # ------------------------------------------------------------------
    def run(self, project: Project, step: str, args: dict | None = None) -> dict:
        args = args or {}
        if self.bridge.is_alive():
            return self._run_live(project, step, args)
        return self._run_background(project, step, args)

    def _run_live(self, project: Project, step: str, args: dict) -> dict:
        # Le Blender ouvert doit travailler sur CE projet, pas sur le précédent.
        if step != "import" and project.blend.exists():
            try:
                current = self.bridge.send("get_scene_info").get("file", "")
                if Path(current or "/").resolve() != project.blend.resolve():
                    self.bridge.send("open_file", {"filepath": str(project.blend)})
            except BridgeError as exc:
                raise StepError(str(exc)) from exc
        code = f"""
import sys, json, importlib
sys.path.insert(0, {str(BLENDER_SIDE)!r})
import bas_pipeline
importlib.reload(bas_pipeline)
_r = bas_pipeline.run_step({step!r}, json.loads({json.dumps(args)!r}))
bas_pipeline.step_export({str(project.preview)!r}, "glb")
bpy.context.preferences.filepaths.save_version = 0
bpy.ops.wm.save_as_mainfile(filepath={str(project.blend)!r})
print({MARK!r} + json.dumps(_r))
"""
        try:
            out = self.bridge.execute(code)
        except BridgeError as exc:
            raise StepError(str(exc)) from exc
        for line in out.splitlines():
            if line.startswith(MARK):
                return {"result": json.loads(line[len(MARK):]), "mode": "Blender ouvert"}
        raise StepError("Pas de résultat renvoyé par Blender")

    def _run_background(self, project: Project, step: str, args: dict) -> dict:
        blender = self.settings.blender_path
        job = {"step": step, "args": args, "save_as": str(project.blend),
               "preview": str(project.preview)}
        cmd = [blender, "-b", "--factory-startup"]
        if project.blend.exists() and step != "import":
            cmd.append(str(project.blend))
        cmd += ["--python", str(BLENDER_SIDE / "run_step.py"),
                "--", json.dumps(job)]
        self._cancelled = False
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                          env={**os.environ, "PYTHONNOUSERSITE": "1"})
            stdout, stderr = self._proc.communicate(timeout=1800)
        except FileNotFoundError as exc:
            raise StepError(f"Blender introuvable ({blender}). Règle le chemin dans Réglages.") from exc
        except subprocess.TimeoutExpired as exc:
            self._proc.kill()
            raise StepError("Blender a mis plus de 30 min, étape abandonnée.") from exc
        finally:
            self._proc = None
        if self._cancelled:
            raise Cancelled("Étape annulée.")
        for line in stdout.splitlines():
            if line.startswith(MARK):
                data = json.loads(line[len(MARK):])
                if not data["ok"]:
                    raise StepError(data["error"])
                return {"result": data["result"], "mode": "Blender en arrière-plan"}
        tail = (stderr or stdout)[-1500:]
        raise StepError(f"Blender s'est arrêté sans résultat :\n{tail}")

    # ------------------------------------------------------------------
    def check_blender(self, path: str | None = None) -> str:
        """Lance Blender sans interface et renvoie sa version (lève StepError sinon)."""
        exe = path or self.settings.blender_path
        try:
            out = subprocess.run([exe, "-b", "--factory-startup", "--python-expr",
                                  "import bpy; print('__BAS_VERSION__', bpy.app.version_string)"],
                                 capture_output=True, text=True, timeout=90)
        except FileNotFoundError as exc:
            raise StepError(f"Rien à cet emplacement : {exe}") from exc
        except subprocess.TimeoutExpired as exc:
            raise StepError("Blender ne répond pas (plus de 90 s).") from exc
        for line in out.stdout.splitlines():
            if line.startswith("__BAS_VERSION__"):
                return line.split(" ", 1)[1].strip()
        raise StepError("Ce programme ne s'est pas comporté comme Blender.")

    def open_in_blender(self, project: Project | None) -> subprocess.Popen:
        """Lance Blender avec le pont actif (et le projet s'il existe)."""
        cmd = [self.settings.blender_path]
        if project and project.blend.exists():
            cmd.append(str(project.blend))
        cmd += ["--python", str(BLENDER_SIDE / "bas_bridge.py")]
        env = {**os.environ, "BAS_BRIDGE_PORT": str(self.settings.bridge_port)}
        return subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
