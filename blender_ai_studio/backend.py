"""Le « cerveau » de l'interface : exposé à la page web via QWebChannel.

Le JavaScript appelle les slots ; les résultats et la progression reviennent
par le signal `event` (JSON).
"""
from __future__ import annotations

import base64
import copy
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
from .config import CONFIG_DIR, Settings
from .hardware import detect_gpu
from .pipeline import Cancelled, PipelineRunner, Project, StepError

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
        self.cancel_event = threading.Event()
        self._job_started = time.time()
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

    def toast(self, text: str, level: str = "info", action: str | None = None):
        """action : « settings », « folder », « blender » → bouton dans la notification."""
        self.emit("toast", text=text, level=level, action=action)

    def _progress(self, label: str):
        self.emit("busy", on=True, label=label, started=self._job_started)

    def _check_cancel(self):
        if self.cancel_event.is_set():
            raise Cancelled("Annulé.")

    def _job(self, label: str, fn, *args):
        if self.busy:
            self.toast("Une tâche est déjà en cours. Attends qu'elle se termine ou annule-la.", "warn")
            return
        self.busy = True
        self.cancel_event = threading.Event()
        self._job_started = time.time()
        self._progress(label)

        def work():
            try:
                fn(*args)
            except Cancelled:
                self.log("Tâche annulée.")
                self.toast("Tâche annulée.")
            except (StepError, generators.GenerationError, AgentError, ValueError) as exc:
                if self.cancel_event.is_set():  # l'erreur vient de l'arrêt demandé
                    self.log("Tâche annulée.")
                    self.toast("Tâche annulée.")
                    return
                message = str(exc)
                self.log(message, "error")
                low = message.lower()
                action = None
                if ("réglages" in low or "clé api" in low
                        or ("introuvable" in low and not low.startswith("fichier introuvable"))):
                    action = "settings"
                elif "blender n'est pas connecté" in low or "injoignable sur 127.0.0.1" in low:
                    action = "blender"
                self.toast(message.splitlines()[0], "error", action)
            except Exception as exc:  # pragma: no cover - filet de sécurité
                self.log(f"{type(exc).__name__}: {exc}", "error")
                self.toast(f"Erreur inattendue : {exc}", "error")
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

    def _checks(self) -> dict:
        """Check-list de démarrage, calculée sans rien lancer."""
        blender = self.settings.blender_path
        blender_ok = bool(shutil.which(blender) or Path(blender).expanduser().is_file())
        if self.settings.llm_provider == "claude":
            ai_ok, ai_label = bool(self.settings.get_api_key()), "Clé API Claude"
        else:
            ai_ok, ai_label = True, f"Ollama ({self.settings.ollama_model})"
        gen_ok, gen_label = generators.ready(self.settings)
        return {
            "blender": {"ok": blender_ok, "label": "Blender trouvé" if blender_ok else "Blender introuvable"},
            "ai": {"ok": ai_ok, "label": ai_label if ai_ok else "Clé API Claude manquante"},
            "generator": {"ok": gen_ok, "label": gen_label},
        }

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
            "checks": self._checks(),
        }

    def _save_info(self, **updates):
        project = self._ensure_project()
        path = project.folder / "etat.json"
        info = json.loads(path.read_text()) if path.exists() else {}
        info.update(updates)
        path.write_text(json.dumps(info, ensure_ascii=False, indent=2))

    STEP_NAMES = {"import": "Import et nettoyage", "rig": "Squelette", "animate": "Animations",
                  "export": "Export", "info": "Infos"}

    def _step(self, step: str, args: dict | None = None) -> dict:
        self._check_cancel()
        project = self._ensure_project()
        t0 = time.time()
        out = self.runner.run(project, step, args or {})
        self.log(f"{self.STEP_NAMES.get(step, step)} : terminé en {time.time() - t0:.1f} s ({out['mode']})")
        self.emit("preview", url=self._preview_url())
        self.emit("step_done", step=step)
        return out["result"]

    # ------------------------------------------------------------------
    # slots appelés par la page
    # ------------------------------------------------------------------
    @Slot(result=str)
    def getState(self) -> str:
        return json.dumps(self._state(), ensure_ascii=False, default=str)

    @Slot(str)
    def newProject(self, name: str):
        self.project = Project.new(self.settings.workspace_path, name or "")
        self._remember_project()
        self.agent = None
        self.emit("project_changed")
        self.emit("state", state=self._state())
        self.toast(f"Nouveau projet : {self.project.folder.name}")

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
        self.toast(f"Projet ouvert : {path.name}")

    def _project_dir(self, folder: str) -> Path | None:
        path = Path(folder)
        root = self.settings.workspace_path.resolve()
        if path.is_dir() and path.resolve().is_relative_to(root) and path.resolve() != root:
            return path
        return None

    @Slot(str, str, result=str)
    def renameProject(self, folder: str, new_name: str) -> str:
        path = self._project_dir(folder)
        name = "".join(ch if ch.isalnum() or ch in "-_ " else "-" for ch in new_name).strip()[:48]
        if not path or not name:
            return "Nom invalide."
        target = path.parent / name
        if target.exists():
            return "Un projet porte déjà ce nom."
        if self.busy and self.project and self.project.folder == path:
            return "Attends la fin de la tâche en cours."
        path.rename(target)
        if self.project and self.project.folder == path:
            self.project = Project(target)
            self._remember_project()
            self.agent = None
            self.emit("state", state=self._state())
        return ""

    @Slot(str, result=str)
    def deleteProject(self, folder: str) -> str:
        """Met le projet à la corbeille du bureau (récupérable), sinon dans .corbeille/."""
        path = self._project_dir(folder)
        if not path:
            return "Projet introuvable."
        if self.project and self.project.folder == path:
            if self.busy:
                return "Attends la fin de la tâche en cours."
            self.project = None
            (self.settings.workspace_path / ".dernier-projet").unlink(missing_ok=True)
            self.agent = None
        trashed = False
        if shutil.which("gio"):
            trashed = subprocess.run(["gio", "trash", str(path)], capture_output=True).returncode == 0
        if not trashed:
            bin_dir = self.settings.workspace_path / ".corbeille"
            bin_dir.mkdir(exist_ok=True)
            shutil.move(str(path), str(bin_dir / f"{path.name} {time.strftime('%Y%m%d-%H%M%S')}"))
        self.emit("project_changed")
        self.emit("state", state=self._state())
        return ""

    @Slot(result=str)
    def getUiPrefs(self) -> str:
        try:
            return (CONFIG_DIR / "interface.json").read_text(encoding="utf-8")
        except OSError:
            return "{}"

    @Slot(str)
    def setUiPrefs(self, payload: str):
        try:
            json.loads(payload)
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            (CONFIG_DIR / "interface.json").write_text(payload, encoding="utf-8")
        except (ValueError, OSError):
            pass

    @Slot(result=str)
    def getChatHistory(self) -> str:
        if not self.project:
            return "[]"
        path = self.project.folder / "chat.jsonl"
        if not path.exists():
            return "[]"
        lines = path.read_text(encoding="utf-8").splitlines()[-200:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return json.dumps(out, ensure_ascii=False)

    def _transcript(self, entry: dict):
        if not self.project:
            return
        with (self.project.folder / "chat.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    @Slot(str, result=str)
    def saveCapture(self, data_url: str) -> str:
        project = self._ensure_project()
        folder = project.folder / "captures"
        folder.mkdir(exist_ok=True)
        target = folder / f"capture-{time.strftime('%Y%m%d-%H%M%S')}.png"
        target.write_bytes(base64.b64decode(data_url.split(",")[-1]))
        self.toast(f"Capture enregistrée : {target.name}", action="folder")
        return str(target)

    @Slot(str, str)
    def testConnection(self, kind: str, payload: str):
        """Bouton « Tester » des réglages, avec les valeurs du formulaire (pas encore enregistrées)."""
        trial = copy.deepcopy(self.settings)
        for key, value in json.loads(payload or "{}").items():
            if key in trial.__dataclass_fields__ and not key.startswith("_") and value not in (None, ""):
                current = getattr(trial, key)
                if isinstance(current, bool):
                    value = bool(value)
                elif isinstance(current, int):
                    value = int(value)
                setattr(trial, key, value)
        api_key = json.loads(payload or "{}").get("api_key") or trial.get_api_key()

        def work():
            try:
                if kind == "blender":
                    version = PipelineRunner(trial).check_blender()
                    message = f"Blender {version} fonctionne."
                elif kind == "ai":
                    from .agent import check_provider
                    message = check_provider(trial, api_key)
                elif kind == "generator":
                    message = generators.check(trial)
                else:
                    raise ValueError(kind)
                self.emit("test_result", test=kind, ok=True, message=message)
            except Exception as exc:  # le message part tel quel dans le formulaire
                self.emit("test_result", test=kind, ok=False, message=str(exc).splitlines()[0])

        threading.Thread(target=work, daemon=True).start()

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
            fresh = self.project is None
            project = self._ensure_project(req.get("name"))
            if fresh:
                self.emit("state", state=self._state())
            out = project.folder / "sources" / f"genere-{time.strftime('%H%M%S')}.glb"
            out.parent.mkdir(parents=True, exist_ok=True)
            self.log("Génération du modèle 3D…")
            generators.generate(self.settings, out, image=req.get("image") or None,
                                text=req.get("text") or None, log=self.log, cancel=self.cancel_event)
            self._import(str(out), req.get("auto"))

        self._job("Génération 3D", work)

    def _import(self, path: str, auto: dict | None = None):
        if not Path(path).is_file():
            raise StepError(f"Fichier introuvable : {path}")
        if self.project is None:
            # Le projet prend le nom du fichier : « penguin.glb » → projet « penguin »
            self._ensure_project(Path(path).stem.replace("_", " "))
            self.emit("state", state=self._state())
        total = 4 if auto else 1
        if auto:
            self._progress(f"Import et nettoyage (1/{total})")
        stats = self._step("import", {"path": path, "target_faces": int(self.settings.target_faces),
                                      "height": 1.0})
        self._save_info(source=path, mesh=stats, rig=None, animations=[], last_export=None)
        if not auto:
            self.toast(f"Modèle prêt : {stats['faces']:,} faces".replace(",", " "))
            return
        # Enchaînement automatique : squelette → animations → export GLB
        self._progress(f"Squelette (2/{total})")
        self._do_rig(quiet=True)
        self._progress(f"Animations (3/{total})")
        self._do_animate(auto.get("kinds") or ["idle", "walk"], float(auto.get("intensity", 1.0)), quiet=True)
        self._progress(f"Export GLB (4/{total})")
        self._do_export("glb", quiet=True)
        self.toast("Personnage prêt : modélisé, squeletté, animé et exporté.", action="folder")

    @Slot(str)
    def importModel(self, path: str):
        self._job("Import et nettoyage", self._import, path)

    @Slot(str, str)
    def importModelAuto(self, path: str, auto: str):
        self._job("Import et nettoyage", self._import, path, json.loads(auto))

    def _do_rig(self, quiet=False):
        res = self._step("rig")
        self._save_info(rig=res)
        if not quiet:
            self.toast(f"Squelette posé ({len(res['bones'])} os, poids {res['skinning']})")

    @Slot()
    def rig(self):
        self._job("Pose du squelette", self._do_rig)

    def _do_animate(self, kinds, intensity, quiet=False):
        res = self._step("animate", {"kinds": kinds, "intensity": intensity})
        self._save_info(animations=res["animations"])
        if not quiet:
            self.toast("Animations : " + ", ".join(res["animations"]))

    @Slot(str)
    def animate(self, payload: str):
        req = json.loads(payload)
        self._job("Animation", self._do_animate, req.get("kinds") or ["idle", "walk"],
                  float(req.get("intensity", 1.0)))

    def _do_export(self, fmt, quiet=False):
        project = self._ensure_project()
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in project.folder.name)
        path = project.exports / f"{safe.strip('-') or 'perso'}.{fmt}"
        res = self._step("export", {"path": str(path), "fmt": fmt})
        self._save_info(last_export=res["path"])
        if fmt == "glb":
            self._write_web_page(project, path)
        if not quiet:
            self.toast(f"Exporté : {Path(res['path']).name} ({res['size_kb']} Ko)", action="folder")
        self.emit("exported", path=res["path"])

    @Slot(str)
    def exportModel(self, fmt: str):
        self._job("Export", self._do_export, fmt)

    @Slot()
    def cancel(self):
        """Bouton Annuler : arrête la génération, le Blender d'arrière-plan ou l'IA."""
        if not self.busy:
            return
        self.cancel_event.set()
        killed = self.runner.cancel()
        self.stopAgent()
        if not killed and self.runner.bridge.is_alive():
            self.log("L'étape en cours dans Blender ouvert va se terminer, la suite est annulée.")

    def _write_web_page(self, project: Project, glb: Path):
        """Petite page three.js prête à mettre en ligne, à côté du .glb exporté."""
        template = (Path(__file__).parent / "ui" / "web_export.html").read_text(encoding="utf-8")
        (project.exports / "index.html").write_text(template.replace("{{MODEL}}", glb.name),
                                                     encoding="utf-8")

    @Slot()
    def openBlender(self):
        try:
            self.runner.open_in_blender(self.project)
            self.toast("Blender démarre avec le pont activé…")
        except FileNotFoundError:
            self.toast("Blender introuvable : règle son chemin dans Réglages.", "error", "settings")
            return

        def wait_bridge():
            for _ in range(60):
                time.sleep(1)
                if self.runner.bridge.is_alive():
                    self.emit("state", state=self._state())
                    self.toast("Blender est connecté.")
                    return
            self.toast("Blender ne s'est pas connecté au bout d'une minute. Vérifie qu'il s'est bien ouvert.",
                       "warn")

        threading.Thread(target=wait_bridge, daemon=True).start()

    @Slot()
    def openFolder(self):
        if self.project:
            target = self.project.folder / "exports"
            subprocess.Popen(["xdg-open", str(target if target.exists() else self.project.folder)])

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
        payload = {**{k: v for k, v in event.items() if k != "kind"}, "sub": event["kind"]}
        self.emit("agent", **payload)
        self._transcript(payload)

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
        self._ensure_project()
        self._transcript({"sub": "user", "text": text})
        if not self.runner.bridge.is_alive():
            self._agent_event({"kind": "guard", "text": (
                "Blender n'est pas connecté. Clique sur « Ouvrir dans Blender », puis renvoie ta demande.")})
            return
        if self.settings.llm_provider == "claude" and not self.settings.get_api_key():
            self._agent_event({"kind": "guard", "text": "Ajoute ta clé API Claude dans Réglages pour discuter."})
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
        if self.project:
            self._get_agent().reset()
            (self.project.folder / "chat.jsonl").unlink(missing_ok=True)
        self.toast("Conversation effacée.")
