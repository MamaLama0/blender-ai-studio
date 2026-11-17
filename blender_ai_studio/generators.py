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


def generate(settings: Settings, out_path: Path, image: str | None = None, text: str | None = None,
             log: Callable[[str], None] = print) -> Path:
    if not image and not text:
        raise GenerationError("Donne une image ou une description.")
    if settings.generator == "hunyuan_local":
        if not image:
            raise GenerationError("Hunyuan3D part d'une image (son texte → 3D est désactivé par défaut). "
                                  "Ajoute une image, ou choisis TRELLIS pour partir d'un texte.")
        return _hunyuan(settings, out_path, image, text, log)
    if settings.generator == "trellis":
        if not Path(settings.trellis_python).expanduser().exists():
            raise GenerationError(
                f"Python de TRELLIS introuvable : {settings.trellis_python}\n"
                "Installe TRELLIS (voir README, GPU NVIDIA ≥ 16 Go) ou change de moteur "
                "dans Réglages. Tu peux aussi importer un fichier 3D.")
        return _custom(settings, out_path, image, text, log, template=TRELLIS_TEMPLATE)
    if settings.generator == "custom_command":
        return _custom(settings, out_path, image, text, log)
    raise GenerationError("Aucun moteur de génération choisi : importe un fichier 3D, "
                          "ou choisis un moteur dans Réglages.")


# ---------------------------------------------------------------------------
def _hunyuan(settings, out_path, image, text, log):
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
        time.sleep(3)
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


def _custom(settings, out_path, image, text, log, template=None):
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
                            text=True)
    for line in proc.stdout:  # type: ignore[union-attr]
        line = line.rstrip()
        if line:
            log(line[-300:])
    if proc.wait() != 0:
        raise GenerationError(f"La commande a échoué (code {proc.returncode}).")
    if not out_path.exists():
        raise GenerationError(f"La commande n'a pas créé {out_path}")
    return out_path
