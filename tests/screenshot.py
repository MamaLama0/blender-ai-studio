"""Ouvre l'interface hors écran, joue un scénario, et enregistre des captures.

QT_QPA_PLATFORM=offscreen QTWEBENGINE_DISABLE_SANDBOX=1 python tests/screenshot.py OUT_DIR [script.js]
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PySide6.QtCore import QTimer  # noqa: E402

from blender_ai_studio.app import build  # noqa: E402

out_dir = sys.argv[1]
os.makedirs(out_dir, exist_ok=True)
steps = [s for s in os.environ.get("SHOTS", "accueil:3000").split(",") if s]

app, window, view, backend = build()
window.resize(1440, 900)
window.show()

console = []
view.page().javaScriptConsoleMessage = lambda lvl, msg, line, src: console.append(f"{lvl} {msg} ({src}:{line})")


def run(i=0):
    if i >= len(steps):
        print("\n".join(console[-30:]))
        app.quit()
        return
    name, delay, *js = steps[i].split(":", 2)
    if js:
        view.page().runJavaScript(js[0])

    def shoot():
        view.grab().save(os.path.join(out_dir, f"{i:02d}-{name}.png"))
        run(i + 1)

    QTimer.singleShot(int(delay), shoot)


QTimer.singleShot(500, run)
app.exec()
