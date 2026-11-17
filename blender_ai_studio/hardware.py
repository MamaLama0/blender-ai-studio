"""Détection de la carte graphique pour choisir où tourne la génération 3D."""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class GPUInfo:
    vendor: str          # nvidia | amd | intel | none
    name: str
    vram_gb: float       # 0 si inconnu

    @property
    def local_3d_ok(self) -> bool:
        """Hunyuan3D 2 mini tourne avec ~6 Go, la chaîne complète demande ~12 Go (CUDA)."""
        return self.vendor == "nvidia" and self.vram_gb >= 6

    @property
    def advice(self) -> str:
        if self.vendor == "nvidia" and self.vram_gb >= 12:
            return "Génération 3D locale complète possible (forme + texture)."
        if self.vendor == "nvidia" and self.vram_gb >= 6:
            return "Génération locale possible avec Hunyuan3D-2mini (forme ; texture lente)."
        if self.vendor == "amd":
            return ("Carte AMD : les générateurs 3D visent CUDA. Utilise un serveur distant "
                    "ou importe des modèles ; Blender, le rig et l'animation marchent très bien.")
        return ("Pas de GPU NVIDIA détecté : utilise un serveur de génération distant ou importe "
                "des modèles. Tout le reste du pipeline fonctionne.")

    def label(self) -> str:
        if self.vendor == "none":
            return "Aucun GPU dédié détecté"
        vram = f" · {self.vram_gb:.0f} Go" if self.vram_gb else ""
        return f"{self.name}{vram}"


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def detect_gpu() -> GPUInfo:
    if shutil.which("nvidia-smi"):
        out = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
        if out.strip():
            name, _, mem = out.strip().splitlines()[0].partition(",")
            try:
                vram = float(mem.strip()) / 1024
            except ValueError:
                vram = 0.0
            return GPUInfo("nvidia", name.strip(), vram)

    if shutil.which("rocm-smi"):
        out = _run(["rocm-smi", "--showmeminfo", "vram", "--csv"])
        match = re.search(r",(\d{6,})", out)
        if match:
            return GPUInfo("amd", "GPU AMD (ROCm)", int(match.group(1)) / 1024**3)

    lspci = _run(["lspci"]) if shutil.which("lspci") else ""
    for line in lspci.splitlines():
        if not re.search(r"VGA|3D controller|Display controller", line):
            continue
        low = line.lower()
        desc = line.split(":", 2)[-1].strip()
        if "nvidia" in low:
            return GPUInfo("nvidia", desc + " (pilote NVIDIA non chargé ?)", 0.0)
        if "amd" in low or "ati " in low or "radeon" in low:
            return GPUInfo("amd", desc, 0.0)
        if "intel" in low:
            return GPUInfo("intel", desc, 0.0)
    return GPUInfo("none", "", 0.0)
