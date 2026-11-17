#!/usr/bin/env bash
# Désinstalle Blender AI Studio. Tes projets (~/BlenderAIStudio) et tes réglages
# (~/.config/blender-ai-studio) sont conservés.
set -euo pipefail
APP=blender-ai-studio
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
rm -rf "$DATA/$APP"
rm -f "$HOME/.local/bin/$APP" "$DATA/applications/$APP.desktop" "$DATA/icons/hicolor/scalable/apps/$APP.svg"
echo "Blender AI Studio est désinstallé (projets et réglages conservés)."
