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
        stamp = time.strftime("%Y%m%d-%H%M%S")
        slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (name or "perso"))[:40]
        folder = workspace / f"{slug}-{stamp}"
        folder.mkdir(parents=True, exist_ok=True)
        return cls(folder)


class StepError(RuntimeError):
    pass


class PipelineRunner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.bridge = BlenderBridge(settings.bridge_host, settings.bridge_port)

    # ------------------------------------------------------------------
    def run(self, project: Project, step: str, args: dict | None = None) -> dict:
        args = args or {}
        if self.bridge.is_alive():
            return self._run_live(project, step, args)
        return self._run_background(project, step, args)

    def _run_live(self, project: Project, step: str, args: dict) -> dict:
        code = f"""
import sys, json, importlib
sys.path.insert(0, {str(BLENDER_SIDE)!r})
import bas_pipeline
importlib.reload(bas_pipeline)
_r = bas_pipeline.run_step({step!r}, json.loads({json.dumps(args)!r}))
bas_pipeline.step_export({str(project.preview)!r}, "glb")
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
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                                  env={**os.environ, "PYTHONNOUSERSITE": "1"})
        except FileNotFoundError as exc:
            raise StepError(f"Blender introuvable ({blender}). Règle le chemin dans Réglages.") from exc
        except subprocess.TimeoutExpired as exc:
            raise StepError("Blender a mis plus de 30 min, étape abandonnée.") from exc
        for line in proc.stdout.splitlines():
            if line.startswith(MARK):
                data = json.loads(line[len(MARK):])
                if not data["ok"]:
                    raise StepError(data["error"])
                return {"result": data["result"], "mode": "Blender en arrière-plan"}
        tail = (proc.stderr or proc.stdout)[-1500:]
        raise StepError(f"Blender s'est arrêté sans résultat :\n{tail}")

    # ------------------------------------------------------------------
    def open_in_blender(self, project: Project | None) -> subprocess.Popen:
        """Lance Blender avec le pont actif (et le projet s'il existe)."""
        cmd = [self.settings.blender_path]
        if project and project.blend.exists():
            cmd.append(str(project.blend))
        cmd += ["--python", str(BLENDER_SIDE / "bas_bridge.py")]
        env = {**os.environ, "BAS_BRIDGE_PORT": str(self.settings.bridge_port)}
        return subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
