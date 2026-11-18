"""Fenêtre principale : une page web (ui/) affichée dans Qt WebEngine."""
from __future__ import annotations

import mimetypes
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

UI_DIR = Path(__file__).parent / "ui"

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("model/gltf-binary", ".glb")
mimetypes.add_type("font/woff2", ".woff2")


class LocalFileServer:
    """Petit serveur HTTP sur 127.0.0.1 (port aléatoire) pour l'interface et les modèles.

    Les modules JavaScript et le chargement des .glb ne marchent pas depuis file://,
    d'où ce serveur. Un jeton secret dans l'URL empêche les autres sites ouverts sur
    la machine d'y lire quoi que ce soit.
    """

    def __init__(self, workspace: Path, extra_files: dict[str, bytes]):
        self.workspace = workspace.resolve()
        self.token = secrets.token_urlsafe(16)
        self.extra = extra_files
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parts = unquote(urlparse(self.path).path).lstrip("/").split("/", 1)
                if len(parts) < 2 or parts[0] != server.token:
                    return self.send_error(403)
                rel = parts[1]
                if rel in server.extra:
                    return self._send(server.extra[rel], "text/javascript")
                if rel.startswith("ws/"):
                    base, target = server.workspace, (server.workspace / rel[3:]).resolve()
                else:
                    base, target = UI_DIR.resolve(), (UI_DIR / (rel or "index.html")).resolve()
                if not target.is_relative_to(base) or not target.is_file():
                    return self.send_error(404)
                ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                self._send(target.read_bytes(), ctype)

            def _send(self, body: bytes, ctype: str):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}/{self.token}/"

    def url_for(self, path: Path) -> str:
        rel = Path(path).resolve().relative_to(self.workspace)
        return self.base + "ws/" + quote(str(rel))


def main() -> int:
    app, window, _view, _backend = build()
    window.show()
    return app.exec()


def build():
    # Wayland/X11 : Qt choisit seul. Le GPU de la page est désactivable si besoin :
    #   BAS_SOFTWARE_GL=1 blender-ai-studio
    if os.environ.get("BAS_SOFTWARE_GL"):
        os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                              "--ignore-gpu-blocklist --use-gl=angle --use-angle=swiftshader "
                              "--enable-unsafe-swiftshader")
    else:
        # Certains pilotes Linux sont « blocklistés » par Chromium : l'aperçu 3D en a besoin.
        os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--ignore-gpu-blocklist")

    from PySide6.QtCore import QFile, QIODevice, QUrl
    from PySide6.QtGui import QIcon
    from PySide6.QtWebChannel import QWebChannel
    from PySide6.QtWebEngineCore import QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication, QMainWindow

    from .backend import Backend
    from .config import Settings

    app = QApplication(sys.argv)
    app.setApplicationName("Blender AI Studio")
    app.setDesktopFileName("blender-ai-studio")
    icon_path = UI_DIR / "icon.svg"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    settings = Settings.load()
    settings.save()

    qwc = QFile(":/qtwebchannel/qwebchannel.js")
    qwc.open(QIODevice.ReadOnly)
    qwebchannel_js = bytes(qwc.readAll())
    qwc.close()

    server = LocalFileServer(settings.workspace_path, {"qwebchannel.js": qwebchannel_js})

    from PySide6.QtCore import QSettings

    class MainWindow(QMainWindow):
        """Retient la taille et la position de la fenêtre d'un lancement à l'autre."""

        def __init__(self):
            super().__init__()
            self.store = QSettings("blender-ai-studio", "fenetre")
            geometry = self.store.value("geometry")
            if geometry is None or not self.restoreGeometry(geometry):
                self.resize(1440, 900)

        def closeEvent(self, event):
            self.store.setValue("geometry", self.saveGeometry())
            super().closeEvent(event)

    window = MainWindow()
    window.setWindowTitle("Blender AI Studio")
    window.setMinimumSize(900, 600)
    view = QWebEngineView(window)
    view.page().setBackgroundColor(Qt_color("#16181d"))
    web_settings = view.settings()
    web_settings.setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, True)
    web_settings.setAttribute(QWebEngineSettings.WebAttribute.Accelerated2dCanvasEnabled, True)

    backend = Backend(settings, server, window)
    channel = QWebChannel(view.page())
    channel.registerObject("backend", backend)
    view.page().setWebChannel(channel)
    view.setUrl(QUrl(server.base + "index.html"))
    window.setCentralWidget(view)
    return app, window, view, backend


def Qt_color(hex_value: str):
    from PySide6.QtGui import QColor

    return QColor(hex_value)


if __name__ == "__main__":
    raise SystemExit(main())
