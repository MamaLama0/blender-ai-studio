"""Le « cerveau » de l'interface : exposé à la page web via QWebChannel.

Le JavaScript appelle les slots ; les résultats et la progression reviennent
par le signal `event` (JSON).
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import QFileDialog

from . import generators
from .agent import Agent, AgentError
from .bridge import BlenderBridge
from .config import Settings
from .hardware import detect_gpu
from .pipeline import PipelineRunner, Project, StepError

MODEL_FILTER = "Modèles 3D (*.glb *.gltf *.obj *.fbx *.stl *.ply)"
IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.webp)"


class Backend(QObject):
    event = Signal(str)
    _post = Signal(str)

    def __init__(self, settings: Settings, file_server, window=None):
        super().__init__()
        self.settings = settings
        self.file_server = file_server
        self.window = window
        self.gpu = detect_gpu()
        self.runner = PipelineRunner(settings)
        self.project: Project | None = None
        self.busy = False
        self._confirm_waits: dict[str, tuple[threading.Event, dict]] = {}
        self.agent: Agent | None = None
        self._post.connect(self.event.emit)  # renvoie les événements des threads vers l'UI
        self._load_last_project()

    # ------------------------------------------------------------------
    # outils internes
    # ------------------------------------------------------------------
    def emit(self, kind: str, **data):
        data["kind"] = kind
        self._post.emit(json.dumps(data, ensure_ascii=False, default=str))

    def log(self, text: str, level: str = "info"):
        self.emit("log", text=text, level=level)

    def _job(self, label: str, fn, *args):
        if self.busy:
            self.emit("toast", text="Une tâche est déjà en cours. Attends qu'elle se termine.", level="warn")
            return
        self.busy = True
        self.emit("busy", on=True, label=label)

        def work():
            try:
                fn(*args)
            except (StepError, generators.GenerationError, AgentError, ValueError) as exc:
                self.log(str(exc), "error")
                self.emit("toast", text=str(exc).splitlines()[0], level="error")
            except Exception as exc:  # pragma: no cover - filet de sécurité
                self.log(f"{type(exc).__name__}: {exc}", "error")
                self.emit("toast", text=f"Erreur inattendue : {exc}", level="error")
            finally:
                self.busy = False
                self.emit("busy", on=False, label="")
                self.emit("state", state=self._state())

        threading.Thread(target=work, daemon=True).start()

    def _ensure_project(self, name: str | None = None) -> Project:
        if self.project is None:
            self.project = Project.new(self.settings.workspace_path, name)
            self._remember_project()
        return self.project

    def _remember_project(self):
        marker = self.settings.workspace_path / ".dernier-projet"
        if self.project:
            marker.write_text(str(self.project.folder))

    def _load_last_project(self):
        marker = self.settings.workspace_path / ".dernier-projet"
        if marker.exists():
            folder = Path(marker.read_text().strip())
            if folder.exists():
                self.project = Project(folder)

    def _preview_url(self) -> str | None:
        if self.project and self.project.preview.exists():
            stamp = int(self.project.preview.stat().st_mtime * 1000)
            return self.file_server.url_for(self.project.preview) + f"?v={stamp}"
        return None

    def _state(self) -> dict:
        settings = asdict(self.settings)
        settings.pop("_fallback_api_key", None)
        info_file = self.project.folder / "etat.json" if self.project else None
        info = json.loads(info_file.read_text()) if info_file and info_file.exists() else {}
        return {
            "settings": settings,
            "has_api_key": bool(self.settings.get_api_key()),
            "gpu": {"label": self.gpu.label(), "vendor": self.gpu.vendor,
                    "vram": self.gpu.vram_gb, "advice": self.gpu.advice, "local_ok": self.gpu.local_3d_ok},
            "blender_live": BlenderBridge(self.settings.bridge_host, self.settings.bridge_port).is_alive(),
            "project": {
                "name": self.project.folder.name if self.project else None,
                "folder": str(self.project.folder) if self.project else None,
                "preview": self._preview_url(),
                "info": info,
            },
            "busy": self.busy,
        }

    def _save_info(self, **updates):
        project = self._ensure_project()
        path = project.folder / "etat.json"
        info = json.loads(path.read_text()) if path.exists() else {}
        info.update(updates)
        path.write_text(json.dumps(info, ensure_ascii=False, indent=2))

    def _step(self, step: str, args: dict | None = None) -> dict:
        project = self._ensure_project()
        t0 = time.time()
        out = self.runner.run(project, step, args or {})
        self.log(f"{step} : terminé en {time.time() - t0:.1f} s ({out['mode']})")
        self.emit("preview", url=self._preview_url())
        return out["result"]

    # ------------------------------------------------------------------
    # slots appelés par la page
    # ------------------------------------------------------------------
    @Slot(result=str)
    def getState(self) -> str:
        return json.dumps(self._state(), ensure_ascii=False, default=str)

    @Slot(str)
    def newProject(self, name: str):
        self.project = Project.new(self.settings.workspace_path, name or "perso")
        self._remember_project()
        self.agent = None
        self.emit("project_changed")
        self.emit("state", state=self._state())
        self.emit("toast", text=f"Nouveau projet : {self.project.folder.name}")

    @Slot(result=str)
    def listProjects(self) -> str:
        items = []
        for folder in self.settings.workspace_path.iterdir():
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            info_file = folder / "etat.json"
            info = json.loads(info_file.read_text()) if info_file.exists() else {}
            done = sum(bool(info.get(k)) for k in ("mesh", "rig", "animations", "last_export"))
            items.append({
                "name": folder.name,
                "folder": str(folder),
                "modified": folder.stat().st_mtime,
                "steps_done": done,
                "current": bool(self.project and self.project.folder == folder),
            })
        items.sort(key=lambda p: p["modified"], reverse=True)
        return json.dumps(items[:50], ensure_ascii=False)

    @Slot(str)
    def openProject(self, folder: str):
        path = Path(folder)
        if not path.is_dir() or not path.resolve().is_relative_to(self.settings.workspace_path.resolve()):
            self.emit("toast", text="Projet introuvable.", level="error")
            return
        self.project = Project(path)
        self._remember_project()
        self.agent = None
        if self.runner.bridge.is_alive() and self.project.blend.exists():
            try:
                self.runner.bridge.send("open_file", {"filepath": str(self.project.blend)})
            except Exception as exc:  # Blender ouvert mais occupé : pas grave
                self.log(f"Blender n'a pas pu ouvrir le projet : {exc}", "error")
        self.emit("project_changed")
        self.emit("state", state=self._state())
        self.emit("toast", text=f"Projet ouvert : {path.name}")

    @Slot(str, result=str)
    def pickFile(self, kind: str) -> str:
        flt = IMAGE_FILTER if kind == "image" else MODEL_FILTER
        title = "Choisir une image" if kind == "image" else "Choisir un modèle 3D"
        path, _ = QFileDialog.getOpenFileName(self.window, title, str(Path.home()), flt)
        return path or ""

    @Slot(str, result=str)
    def adoptImage(self, path: str) -> str:
        """Copie l'image choisie dans le projet pour pouvoir l'afficher dans la page."""
        project = self._ensure_project(Path(path).stem)
        target = project.folder / "sources" / Path(path).name
        target.parent.mkdir(parents=True, exist_ok=True)
        if Path(path).resolve() != target.resolve():
            shutil.copyfile(path, target)
        return json.dumps({"path": str(target), "url": self.file_server.url_for(target)})

    @Slot(str, str, result=str)
    def saveDropped(self, name: str, data_b64: str) -> str:
        """Fichier glissé-déposé dans la page : on l'enregistre dans le projet."""
        project = self._ensure_project()
        safe = "".join(ch for ch in Path(name).name if ch.isalnum() or ch in "._-") or "fichier"
        target = project.folder / "sources" / safe
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(data_b64.split(",")[-1]))
        return str(target)

    @Slot(str)
    def generate(self, payload: str):
        req = json.loads(payload)

        def work():
            project = self._ensure_project(req.get("name"))
            out = project.folder / "sources" / f"genere-{time.strftime('%H%M%S')}.glb"
            out.parent.mkdir(parents=True, exist_ok=True)
            self.log("Génération du modèle 3D…")
            generators.generate(self.settings, out, image=req.get("image") or None,
                                text=req.get("text") or None, log=self.log)
            self._import(str(out))

        self._job("Génération 3D", work)

    def _import(self, path: str):
        stats = self._step("import", {"path": path, "target_faces": int(self.settings.target_faces),
                                      "height": 1.0})
        self._save_info(source=path, mesh=stats, rig=None, animations=[])
        self.emit("toast", text=f"Modèle prêt : {stats['faces']:,} faces".replace(",", " "))

    @Slot(str)
    def importModel(self, path: str):
        self._job("Import et nettoyage", self._import, path)

    @Slot()
    def rig(self):
        def work():
            res = self._step("rig")
            self._save_info(rig=res)
            self.emit("toast", text=f"Squelette posé ({len(res['bones'])} os, poids {res['skinning']})")

        self._job("Pose du squelette", work)

    @Slot(str)
    def animate(self, payload: str):
        req = json.loads(payload)

        def work():
            res = self._step("animate", {"kinds": req.get("kinds") or ["idle", "walk"],
                                         "intensity": float(req.get("intensity", 1.0))})
            self._save_info(animations=res["animations"])
            self.emit("toast", text="Animations : " + ", ".join(res["animations"]))

        self._job("Animation", work)

    @Slot(str)
    def exportModel(self, fmt: str):
        def work():
            project = self._ensure_project()
            safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in project.folder.name)
            path = project.exports / f"{safe.strip('-') or 'perso'}.{fmt}"
            res = self._step("export", {"path": str(path), "fmt": fmt})
            self._save_info(last_export=res["path"])
            if fmt == "glb":
                self._write_web_page(project, path)
            self.emit("toast", text=f"Exporté : {Path(res['path']).name} ({res['size_kb']} Ko)")
            self.emit("exported", path=res["path"])

        self._job("Export", work)

    def _write_web_page(self, project: Project, glb: Path):
        """Petite page three.js prête à mettre en ligne, à côté du .glb exporté."""
        template = (Path(__file__).parent / "ui" / "web_export.html").read_text(encoding="utf-8")
        (project.exports / "index.html").write_text(template.replace("{{MODEL}}", glb.name),
                                                     encoding="utf-8")

    @Slot()
    def openBlender(self):
        try:
            self.runner.open_in_blender(self.project)
            self.emit("toast", text="Blender démarre avec le pont activé…")
        except FileNotFoundError:
            self.emit("toast", text="Blender introuvable : règle son chemin dans Réglages.", level="error")

        def wait_bridge():
            for _ in range(60):
                time.sleep(1)
                if self.runner.bridge.is_alive():
                    self.emit("state", state=self._state())
                    self.emit("toast", text="Blender est connecté.")
                    return

        threading.Thread(target=wait_bridge, daemon=True).start()

    @Slot()
    def openFolder(self):
        if self.project:
            subprocess.Popen(["xdg-open", str(self.project.folder)])

    @Slot()
    def refresh(self):
        self.emit("state", state=self._state())

    @Slot(str, result=str)
    def saveSettings(self, payload: str) -> str:
        data = json.loads(payload)
        api_key = data.pop("api_key", "")
        for key, value in data.items():
            if key in self.settings.__dataclass_fields__ and not key.startswith("_"):
                current = getattr(self.settings, key)
                if isinstance(current, bool):
                    value = bool(value)
                elif isinstance(current, int):
                    value = int(value)
                setattr(self.settings, key, value)
        where = ""
        if api_key:
            where = self.settings.set_api_key(api_key.strip())
        self.settings.save()
        self.runner = PipelineRunner(self.settings)
        self.agent = None
        self.emit("state", state=self._state())
        return f"Réglages enregistrés{' — clé rangée dans le ' + where if where else ''}."

    # ------------------------------------------------------------------
    # chat avec l'agent
    # ------------------------------------------------------------------
    def _get_agent(self) -> Agent:
        if self.agent is None:
            project = self._ensure_project()

            def run_step(step, args):
                return self._step(step, args)

            self.agent = Agent(self.settings, on_event=self._agent_event,
                               confirm=self._confirm, run_step=run_step, log_dir=project.folder)
        return self.agent

    def _agent_event(self, event: dict):
        # Les événements de l'agent ont leur propre « kind » (assistant, tool…) : on le
        # range dans « sub » pour ne pas écraser le type de message envoyé à la page.
        self.emit("agent", **{**{k: v for k, v in event.items() if k != "kind"}, "sub": event["kind"]})

    def _confirm(self, code: str, explanation: str) -> bool:
        cid = uuid.uuid4().hex
        waiter = threading.Event()
        box = {"approved": False}
        self._confirm_waits[cid] = (waiter, box)
        self.emit("confirm", id=cid, code=code, explanation=explanation)
        waiter.wait(timeout=600)
        self._confirm_waits.pop(cid, None)
        return box["approved"]

    @Slot(str, bool)
    def confirmReply(self, cid: str, approved: bool):
        if cid in self._confirm_waits:
            waiter, box = self._confirm_waits[cid]
            box["approved"] = approved
            waiter.set()

    @Slot(str)
    def chat(self, text: str):
        if not self.runner.bridge.is_alive():
            self.emit("agent", sub="guard", text=(
                "Blender n'est pas connecté. Clique sur « Ouvrir dans Blender » puis renvoie ta demande."))
            return
        agent = self._get_agent()
        self._job("L'IA travaille dans Blender", agent.ask, text)

    @Slot()
    def stopAgent(self):
        if self.agent:
            self.agent.stop_requested = True
        for waiter, box in list(self._confirm_waits.values()):
            box["approved"] = False
            waiter.set()

    @Slot()
    def resetChat(self):
        if self.agent:
            self.agent.reset()
        self.emit("toast", text="Conversation effacée.")
