"""Bêta-test du « cerveau » de l'interface (sans page web) : annulation, enchaînement,
boutons Tester, projets, historique du chat.

QT_QPA_PLATFORM=offscreen python tests/test_backend.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["HOME"] = tempfile.mkdtemp()           # réglages isolés
os.environ.pop("XDG_CONFIG_HOME", None)

from PySide6.QtCore import QCoreApplication  # noqa: E402

from blender_ai_studio.backend import Backend  # noqa: E402
from blender_ai_studio.config import Settings  # noqa: E402

FAKE = str(ROOT / "tests" / "fake_blender.py")
SLOW = str(ROOT / "tests" / "slow_blender.sh")
MODEL = "/tmp/dummy.glb"


class FakeServer:
    def url_for(self, path):
        return "http://x/" + str(path)


app = QCoreApplication.instance() or QCoreApplication([])
events = []


def make_backend(**overrides):
    ws = tempfile.mkdtemp()
    st = Settings(blender_path=FAKE, workspace=ws, bridge_port=9799, generator="import_only", **overrides)
    b = Backend(st, FakeServer())
    b.event.connect(lambda raw: events.append(json.loads(raw)))
    return b


def wait(pred, timeout=120.0):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.05)
    return False


def kinds(kind):
    return [e for e in events if e["kind"] == kind]


def toasts():
    return [e["text"] for e in kinds("toast")]


def check(label, cond):
    print(("OK   " if cond else "ÉCHEC"), label)
    if not cond:
        raise SystemExit(1)


def main():
    if not os.path.exists(MODEL):
        sys.path.insert(0, str(ROOT / "tests"))
        import test_pipeline_bpy
        test_pipeline_bpy.build_dummy(MODEL)

    # 1. Import enchaîné : nom du projet tiré du fichier, 4 étapes, export GLB
    b = make_backend()
    b.importModelAuto(MODEL, json.dumps({"kinds": ["idle", "walk"], "intensity": 1}))
    check("enchaînement terminé", wait(lambda: not b.busy and any("Personnage prêt" in t for t in toasts())))
    check("projet nommé d'après le fichier", b.project.folder.name == "dummy")
    labels = [e.get("label") for e in kinds("busy") if e.get("on")]
    check("progression 1/4 → 4/4 affichée", any("4/4" in (l or "") for l in labels))
    info = json.loads((b.project.folder / "etat.json").read_text())
    check("état : maillage, os, animations, export", info["mesh"] and info["rig"] and
          info["animations"] == ["Idle", "Walk"] and info["last_export"].endswith(".glb"))
    check("page web à côté du GLB", (b.project.exports / "index.html").exists())
    check("étapes signalées pour ouvrir la suivante",
          [e["step"] for e in kinds("step_done")] == ["import", "rig", "animate", "export"])

    # 2. Fichier absent : message clair, bouton vers rien de cassé
    events.clear()
    b.importModel("/tmp/nexiste-pas.glb")
    check("fichier absent refusé", wait(lambda: any("introuvable" in t for t in toasts()), 10))

    # 3. Annuler un Blender bloqué
    events.clear()
    b.settings.blender_path = SLOW
    b.runner.settings.blender_path = SLOW
    b.rig()
    wait(lambda: b.runner._proc is not None, 10)
    time.sleep(0.5)
    t0 = time.time()
    b.cancel()
    check("annulation rapide", wait(lambda: not b.busy, 10) and time.time() - t0 < 5)
    check("toast « Tâche annulée »", "Tâche annulée." in toasts())
    b.settings.blender_path = FAKE
    b.runner.settings.blender_path = FAKE

    # 4. Annuler une génération (commande perso qui dort)
    events.clear()
    b.settings.generator = "custom_command"
    b.settings.custom_command = "sleep 60 && echo {output}"
    b.generate(json.dumps({"text": "un pingouin"}))
    time.sleep(1.0)
    b.cancel()
    check("génération annulée", wait(lambda: not b.busy, 10) and "Tâche annulée." in toasts())
    b.settings.generator = "import_only"

    # 5. Boutons « Tester »
    events.clear()
    b.testConnection("blender", json.dumps({"blender_path": FAKE}))
    b.testConnection("generator", json.dumps({"generator": "trellis", "trellis_python": "/nope/python"}))
    b.testConnection("ai", json.dumps({"llm_provider": "ollama", "ollama_url": "http://127.0.0.1:9"}))
    check("3 résultats de test", wait(lambda: len(kinds("test_result")) == 3, 120))
    res = {e["test"]: e for e in kinds("test_result")}
    check("Blender OK + version", res["blender"]["ok"] and "Blender" in res["blender"]["message"])
    check("TRELLIS absent signalé", not res["generator"]["ok"] and "introuvable" in res["generator"]["message"])
    check("Ollama absent signalé", not res["ai"]["ok"] and "ollama serve" in res["ai"]["message"])
    check("le test n'enregistre rien", b.settings.generator == "import_only")

    # 6. Chat sans Blender : message gardé dans l'historique du projet
    events.clear()
    b.chat("ajoute un chapeau")
    wait(lambda: kinds("agent"), 5)
    hist = json.loads(b.getChatHistory())
    check("historique : demande + réponse", [h["sub"] for h in hist] == ["user", "guard"])
    b.resetChat()
    check("historique effacé", json.loads(b.getChatHistory()) == [])

    # 7. Préférences d'interface
    b.setUiPrefs(json.dumps({"skeleton": True, "speed": 2}))
    check("préférences relues", json.loads(b.getUiPrefs())["speed"] == 2)

    # 8. Renommer puis mettre à la corbeille
    folder = str(b.project.folder)
    check("renommer", b.renameProject(folder, "pingouin final") == "" and b.project.folder.name == "pingouin final")
    check("nom déjà pris refusé", b.renameProject(str(b.project.folder), "pingouin final") != "")
    projects = json.loads(b.listProjects())
    check("liste des projets", [p["name"] for p in projects] == ["pingouin final"])
    check("corbeille", b.deleteProject(str(b.project.folder)) == "" and b.project is None)
    check("liste vide ensuite", json.loads(b.listProjects()) == [])
    check("refus hors du dossier des projets", b.deleteProject("/tmp") != "")

    # 9. Check-list de démarrage
    st = json.loads(b.getState())
    check("check-list présente", set(st["checks"]) == {"blender", "ai", "generator"})
    check("Blender trouvé", st["checks"]["blender"]["ok"])
    check("routeur coupé par défaut", st["router"]["provider"] == "none")

    # 10. Routeur (Jev simulé) branché sur la génération et les étapes
    events.clear()
    r = make_backend(router_provider="jev", router_mode="active")
    r.settings.generator = "custom_command"
    r.settings.custom_command = f"cp {MODEL} {{output}}"
    plan = {"generation": ["proceed", "reuse_cache"], "rerun_step": ["reuse_cache", "ask_human"]}
    seen = []

    def fake_jev(_settings, state):
        seen.append((state["task_type"], state["context"].get("cached_result_available")))
        return plan[state["task_type"]].pop(0), 0.9, {}

    r.router_ask_override = fake_jev
    answers = {"ask": False}
    r.event.connect(lambda raw: (lambda e: r.confirmReply(e["id"], answers["ask"])
                                 if e["kind"] == "ask" else None)(json.loads(raw)))

    def run(fn, *a):
        n = len(kinds("busy"))
        fn(*a)
        wait(lambda: len(kinds("busy")) > n, 5)
        return wait(lambda: not r.busy, 120)

    run(r.generate, json.dumps({"text": "un pingouin cowboy", "name": "pingouin"}))
    logs = [e["text"] for e in kinds("log")]
    check("1re génération : routeur consulté, commande lancée",
          seen[0] == ("generation", False) and any(t.startswith("$ cp") for t in logs))
    cache = json.loads((r.settings.workspace_path / ".generations.json").read_text())
    check("génération mise en cache", len(cache) == 1 and list(cache.values())[0]["successes"] == 1)

    events.clear()
    run(r.generate, json.dumps({"text": "Un pingouin cowboy "}))
    logs = [e["text"] for e in kinds("log")]
    check("2e génération identique : le routeur voit le cache", seen[1] == ("generation", True))
    check("« réutiliser » → pas de nouvelle génération",
          not any(t.startswith("$ cp") for t in logs) and any("réutilise" in t for t in toasts()))

    run(r.exportModel, "glb")                       # 1er export : rien à décider
    events.clear()
    run(r.exportModel, "glb")                       # 2e export, rien n'a changé
    check("export inchangé : le routeur le voit à jour", seen[2] == ("rerun_step", True))
    check("« réutiliser » → export sauté", any("déjà à jour" in t for t in toasts()))

    run(r.rig)                                      # 1er squelette
    events.clear()
    run(r.rig)                                      # refaire ? le routeur demande → on répond non
    check("« te demander » → fenêtre oui/non", len(kinds("ask")) == 1)
    log = (r.project.folder / "decisions.jsonl").read_text()
    check("refus noté pour le rapport", '"actual": "ask_human"' in log)
    report = r.routerReport()
    check("rapport du routeur écrit", "Décisions" in report
          and (r.settings.workspace_path / "rapport-routeur.md").exists())
    print("Tout est bon.")


if __name__ == "__main__":
    main()
