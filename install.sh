#!/usr/bin/env bash
# Installe Blender AI Studio pour l'utilisateur courant (Fedora, et la plupart des Linux).
#   ./install.sh            installe ou met à jour
#   ./install.sh --no-blender   ne propose pas d'installer Blender
set -euo pipefail

APP=blender-ai-studio
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/$APP"
VENV="$DATA/venv"
BIN="$HOME/.local/bin"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"

say()  { printf '\033[1;33m▸\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# --- Python -----------------------------------------------------------------
PY=$(command -v python3 || true)
[ -n "$PY" ] || fail "python3 introuvable. Fedora : sudo dnf install python3"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || fail "Python 3.10 ou plus est requis."
"$PY" -c 'import venv, ensurepip' 2>/dev/null || {
  say "Le module venv manque."
  if command -v dnf >/dev/null; then sudo dnf install -y python3-pip
  elif command -v apt-get >/dev/null; then sudo apt-get install -y python3-venv python3-pip
  else fail "Installe le paquet python3-venv de ta distribution."; fi
}

# --- Blender ----------------------------------------------------------------
if [ "${1:-}" != "--no-blender" ] && ! command -v blender >/dev/null \
   && ! flatpak info org.blender.Blender >/dev/null 2>&1; then
  say "Blender n'est pas installé."
  read -r -p "  L'installer maintenant ? [O/n] " answer
  if [[ ! "$answer" =~ ^[nN] ]]; then
    if command -v dnf >/dev/null; then sudo dnf install -y blender
    elif command -v apt-get >/dev/null; then sudo apt-get install -y blender
    elif command -v pacman >/dev/null; then sudo pacman -S --needed blender
    elif command -v flatpak >/dev/null; then flatpak install -y flathub org.blender.Blender
    else say "Installe Blender à la main : https://www.blender.org/download/"; fi
  fi
fi

# --- Environnement Python de l'appli ----------------------------------------
say "Création de l'environnement dans $VENV"
mkdir -p "$DATA"
"$PY" -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip --quiet
say "Installation (PySide6 et Qt WebEngine, ~250 Mo la première fois)…"
"$VENV/bin/pip" install --upgrade "$SRC" --quiet

# --- Lanceur, icône, entrée de menu -----------------------------------------
mkdir -p "$BIN" "$APPS" "$ICONS"
ln -sf "$VENV/bin/$APP" "$BIN/$APP"
cp "$SRC/blender_ai_studio/ui/icon.svg" "$ICONS/$APP.svg"
cat > "$APPS/$APP.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Blender AI Studio
GenericName=Personnages 3D avec l'IA
Comment=Créer, rigger et animer des personnages 3D avec l'IA et Blender
Exec=$BIN/$APP
Icon=$APP
Terminal=false
Categories=Graphics;3DGraphics;
Keywords=blender;3d;ia;rig;animation;
StartupWMClass=blender-ai-studio
EOF
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q "$(dirname "$(dirname "$ICONS")")" 2>/dev/null || true

say "Installé. Lance « Blender AI Studio » depuis le menu, ou : $APP"
case ":$PATH:" in *":$BIN:"*) ;; *) say "Ajoute $BIN à ton PATH pour lancer depuis le terminal." ;; esac
