"""Génération image/texte → modèle 3D.

Moteurs :
  - hunyuan_local  : serveur API de Hunyuan3D-2 (python api_server.py --port 8080)
                     ⚠ licence Tencent : non valable dans l'UE, au Royaume-Uni et en Corée du Sud.
  - custom_command : n'importe quel script en ligne de commande. Préréglage fourni pour
                     TRELLIS (licence MIT) : servers/trellis_generate.py
  - import_only    : pas de génération, on importe un fichier existant.
"""
from __future__ import annotations

import base64
import json
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from .config import Settings

SERVERS_DIR = Path(__file__).parent / "servers"
TRELLIS_TEMPLATE = (
    "{trellis_python} " + str(SERVERS_DIR / "trellis_generate.py") +
    " --repo {trellis_repo} --image {image} --text {text} --out {output}"
)


class GenerationError(RuntimeError):
    pass


def ready(settings: Settings) -> tuple[bool, str]:
    """Vérification rapide (sans rien lancer) pour la check-list de démarrage."""
    gen = settings.generator
    if gen == "import_only":
        return True, "Import de fichiers 3D uniquement"
    if gen == "trellis":
        if Path(settings.trellis_python).expanduser().exists():
            return True, "TRELLIS"
        return False, "Python de TRELLIS introuvable"
    if gen == "hunyuan_local":
        return bool(settings.hunyuan_url.strip()), "Serveur Hunyuan3D"
    if gen == "custom_command":
        if settings.custom_command.strip():
            return True, "Commande personnalisée"
        return False, "Commande vide"
    return False, "Moteur inconnu"


def check(settings: Settings) -> str:
    """Test réel du moteur (bouton « Tester » des réglages). Lève GenerationError si KO."""
    gen = settings.generator
    if gen == "import_only":
        return "Rien à tester : tu importes tes modèles."
    if gen == "trellis":
        py = Path(settings.trellis_python).expanduser()
        if not py.exists():
            raise GenerationError(f"Python introuvable : {py}")
        repo = str(Path(settings.trellis_repo).expanduser())
        code = ("import sys; sys.path.insert(0, %r); import torch, trellis; "
                "print('CUDA' if torch.cuda.is_available() else 'NOCUDA')" % repo)
        try:
            out = subprocess.run([str(py), "-c", code], capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired as exc:
            raise GenerationError("Le Python de TRELLIS ne répond pas.") from exc
        if out.returncode != 0:
            raise GenerationError("TRELLIS ne se charge pas : " + (out.stderr.strip().splitlines() or ["?"])[-1])
        if "NOCUDA" in out.stdout:
            raise GenerationError("TRELLIS est installé, mais PyTorch ne voit pas de GPU CUDA.")
        return "TRELLIS est prêt, GPU CUDA détecté."
    if gen == "hunyuan_local":
        base = settings.hunyuan_url.rstrip("/")
        try:
            urllib.request.urlopen(f"{base}/status/test", timeout=5).read()
        except urllib.error.HTTPError:
            pass  # le serveur répond (même une erreur prouve qu'il tourne)
        except (urllib.error.URLError, OSError) as exc:
            raise GenerationError(f"Aucun serveur sur {base}.") from exc
        return f"Le serveur Hunyuan3D répond sur {base}."
    if gen == "custom_command":
        if not settings.custom_command.strip():
            raise GenerationError("La commande est vide.")
        exe = shlex.split(settings.custom_command)[0]
        import shutil
        if not (shutil.which(exe) or Path(exe).expanduser().exists()):
            raise GenerationError(f"Programme introuvable : {exe}")
        return f"Programme trouvé : {exe}"
    raise GenerationError("Moteur inconnu.")


def generate(settings: Settings, out_path: Path, image: str | None = None, text: str | None = None,
             log: Callable[[str], None] = print, cancel: threading.Event | None = None) -> Path:
    cancel = cancel or threading.Event()
    if not image and not text:
        raise GenerationError("Donne une image ou une description.")
    if settings.generator == "hunyuan_local":
        if not image:
            raise GenerationError("Hunyuan3D part d'une image (son texte → 3D est désactivé par défaut). "
                                  "Ajoute une image, ou choisis TRELLIS pour partir d'un texte.")
        return _hunyuan(settings, out_path, image, text, log, cancel)
    if settings.generator == "trellis":
        if not Path(settings.trellis_python).expanduser().exists():
            raise GenerationError(
                f"Python de TRELLIS introuvable : {settings.trellis_python}\n"
                "Installe TRELLIS (voir README, GPU NVIDIA ≥ 16 Go) ou change de moteur "
                "dans Réglages. Tu peux aussi importer un fichier 3D.")
        return _custom(settings, out_path, image, text, log, cancel, template=TRELLIS_TEMPLATE)
    if settings.generator == "custom_command":
        return _custom(settings, out_path, image, text, log, cancel)
    raise GenerationError("Aucun moteur de génération choisi : importe un fichier 3D, "
                          "ou choisis un moteur dans Réglages.")


# ---------------------------------------------------------------------------
def _hunyuan(settings, out_path, image, text, log, cancel):
    payload: dict = {"texture": settings.hunyuan_texture, "face_count": 40000, "seed": 1234}
    if image:
        payload["image"] = base64.b64encode(Path(image).read_bytes()).decode()
    else:
        payload["text"] = text  # seulement si le serveur a le texte→image activé
    base = settings.hunyuan_url.rstrip("/")
    log(f"Envoi à Hunyuan3D ({base})…")
    req = urllib.request.Request(f"{base}/send", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        uid = json.loads(urllib.request.urlopen(req, timeout=30).read())["uid"]
    except (urllib.error.URLError, OSError, KeyError) as exc:
        raise GenerationError(
            f"Serveur Hunyuan3D injoignable sur {base}. Lance-le avec :\n"
            "python api_server.py --port 8080 --enable_tex") from exc

    start = time.time()
    while time.time() - start < 1800:
        if cancel.wait(3):
            raise GenerationError("Génération annulée.")
        try:
            status = json.loads(urllib.request.urlopen(f"{base}/status/{uid}", timeout=30).read())
        except (urllib.error.URLError, OSError) as exc:
            raise GenerationError(f"Connexion perdue avec Hunyuan3D : {exc}") from exc
        if status.get("status") == "completed":
            out_path.write_bytes(base64.b64decode(status["model_base64"]))
            log(f"Modèle reçu en {time.time() - start:.0f} s")
            return out_path
        log(f"Génération en cours… {time.time() - start:.0f} s")
    raise GenerationError("Hunyuan3D n'a pas répondu en 30 min.")


def _custom(settings, out_path, image, text, log, cancel, template=None):
    template = (template or settings.custom_command).strip()
    if not template:
        raise GenerationError("Commande de génération vide (Réglages → Moteur → Commande).")
    values = {
        "image": shlex.quote(image or ""),
        "text": shlex.quote(text or ""),
        "output": shlex.quote(str(out_path)),
        "trellis_python": shlex.quote(str(Path(settings.trellis_python).expanduser())),
        "trellis_repo": shlex.quote(str(Path(settings.trellis_repo).expanduser())),
    }
    cmd = template.format(**values)
    log(f"$ {cmd}")
    proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, start_new_session=True)

    def watch():  # Annuler → on tue tout le groupe de processus (shell + python)
        cancel.wait()
        if proc.poll() is None:
            import os
            import signal
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    threading.Thread(target=watch, daemon=True).start()
    for line in proc.stdout:  # type: ignore[union-attr]
        line = line.rstrip()
        if line:
            log(line[-300:])
    code = proc.wait()
    if cancel.is_set():
        raise GenerationError("Génération annulée.")
    cancel.set()  # libère le fil de surveillance
    if code != 0:
        raise GenerationError(f"La commande a échoué (code {code}).")
    if not out_path.exists():
        raise GenerationError(f"La commande n'a pas créé {out_path}")
    return out_path
