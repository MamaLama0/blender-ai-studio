"""Réglages de l'application, stockés dans ~/.config/blender-ai-studio/config.json.

La clé API n'est jamais écrite dans ce fichier si le trousseau système (keyring,
via GNOME Keyring / KWallet) est disponible.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "blender-ai-studio"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"
KEYRING_SERVICE = APP_NAME


def find_blender() -> str:
    """Cherche Blender : binaire système, Flatpak, puis dossiers courants."""
    for name in ("blender",):
        path = shutil.which(name)
        if path:
            return path
    if shutil.which("flatpak"):
        flatpak_bin = Path("/var/lib/flatpak/exports/bin/org.blender.Blender")
        user_bin = Path.home() / ".local/share/flatpak/exports/bin/org.blender.Blender"
        for candidate in (flatpak_bin, user_bin):
            if candidate.exists():
                return str(candidate)
    for candidate in sorted(Path.home().glob("blender*/blender"), reverse=True):
        if candidate.exists():
            return str(candidate)
    return "blender"


@dataclass
class Settings:
    blender_path: str = field(default_factory=find_blender)
    bridge_host: str = "127.0.0.1"
    bridge_port: int = 9876

    # IA qui pilote Blender : "claude" ou "ollama"
    llm_provider: str = "claude"
    claude_model: str = "claude-sonnet-5-5"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:14b"

    # Garde-fous de l'agent (repris de l'idée du « routeur » de l'article)
    confirm_code: bool = True       # demander avant d'exécuter du code dans Blender
    max_steps: int = 12             # nombre max d'actions par demande
    max_same_error: int = 2         # même erreur N fois → on arrête et on te demande

    # Génération 3D
    generator: str = "trellis"        # trellis | hunyuan_local | custom_command | import_only
    trellis_repo: str = str(Path.home() / "TRELLIS")
    trellis_python: str = str(Path.home() / "miniconda3/envs/trellis/bin/python")
    hunyuan_url: str = "http://127.0.0.1:8080"
    hunyuan_texture: bool = True
    custom_command: str = ""          # ex: python mon_script.py --image {image} --out {output}

    # Pipeline
    target_faces: int = 20000
    workspace: str = str(Path.home() / "BlenderAIStudio")

    # Clé API en clair uniquement si keyring est indisponible
    _fallback_api_key: str = ""

    @classmethod
    def load(cls) -> "Settings":
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text())
                known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
                return cls(**known)
            except (json.JSONDecodeError, TypeError):
                pass
        return cls()

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False))
        os.chmod(CONFIG_FILE, 0o600)

    # --- clé API -----------------------------------------------------------
    def get_api_key(self) -> str:
        env = os.environ.get("ANTHROPIC_API_KEY")
        if env:
            return env
        try:
            import keyring

            value = keyring.get_password(KEYRING_SERVICE, "anthropic")
            if value:
                return value
        except Exception:
            pass
        return self._fallback_api_key

    def set_api_key(self, key: str) -> str:
        """Enregistre la clé. Renvoie où elle a été rangée."""
        try:
            import keyring

            keyring.set_password(KEYRING_SERVICE, "anthropic", key)
            self._fallback_api_key = ""
            return "trousseau système"
        except Exception:
            self._fallback_api_key = key
            return "fichier de config (droits 600)"

    @property
    def workspace_path(self) -> Path:
        path = Path(self.workspace).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path
