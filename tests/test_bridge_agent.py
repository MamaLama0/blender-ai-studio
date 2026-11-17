"""Teste le pont Blender (dans `bpy`) + l'agent avec une IA simulée.

python tests/test_bridge_agent.py
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "blender_ai_studio" / "blender_side"))
sys.path.insert(0, str(ROOT / "tests"))

import bpy  # noqa: E402

import bas_bridge  # noqa: E402
from blender_ai_studio.agent import Agent  # noqa: E402
from blender_ai_studio.bridge import BlenderBridge  # noqa: E402
from blender_ai_studio.config import Settings  # noqa: E402
from blender_ai_studio.pipeline import PipelineRunner, Project  # noqa: E402

PORT = 9911
results = {}


def scripted(responses):
    """Remplace l'appel à Claude par une suite de réponses écrites à l'avance."""
    it = iter(responses)
    return lambda: next(it)


def tool(name, inp, tid):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def client_side(tmp):
    try:
        settings = Settings(bridge_port=PORT, workspace=tmp, confirm_code=True, max_same_error=2)
        bridge = BlenderBridge(port=PORT)
        assert bridge.is_alive(), "pont muet"

        # 1. Pipeline en direct via le pont
        project = Project.new(Path(tmp), "pont")
        runner = PipelineRunner(settings)
        out = runner.run(project, "import", {"path": "/tmp/dummy.glb", "target_faces": 3000})
        assert out["mode"] == "Blender ouvert", out
        runner.run(project, "rig")
        out = runner.run(project, "animate", {"kinds": ["walk"]})
        assert out["result"]["animations"] == ["Walk"]
        assert project.preview.exists() and project.blend.exists()

        # 2. Agent : regarde la scène, fait valider du code, puis répond
        events, asked = [], []
        agent = Agent(settings, on_event=events.append,
                      confirm=lambda code, why: asked.append(why) or True,
                      run_step=lambda s, a: runner.run(project, s, a), log_dir=Path(tmp))
        code = ("mat = bpy.data.materials.new('Argile')\n"
                "bpy.data.objects['Character'].data.materials.append(mat)\n"
                "print('materiaux:', len(bpy.data.objects['Character'].data.materials))")
        agent._claude_call = scripted([
            {"stop_reason": "tool_use", "content": [tool("get_scene_info", {}, "t1")]},
            {"stop_reason": "tool_use", "content": [
                {"type": "text", "text": "J'ajoute un matériau."},
                tool("execute_blender_code", {"code": code, "explanation": "Ajoute un matériau argile"}, "t2")]},
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "C'est fait."}]},
        ])
        agent.ask("Mets-lui un matériau argile")
        kinds = [e["kind"] for e in events]
        assert asked == ["Ajoute un matériau argile"], asked
        assert any(e["kind"] == "tool_result" and "materiaux: 1" in e["text"] for e in events), events
        assert kinds[-1] == "assistant" and events[-1]["text"] == "C'est fait."

        # 3. Garde-fou : la même erreur deux fois → arrêt
        events.clear()
        bad = {"code": "bpy.data.objects['Fantome'].location.x = 1", "explanation": "x"}
        agent._claude_call = scripted([
            {"stop_reason": "tool_use", "content": [tool("execute_blender_code", bad, "t3")]},
            {"stop_reason": "tool_use", "content": [tool("execute_blender_code", bad, "t4")]},
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "ne doit pas arriver"}]},
        ])
        agent.ask("Bouge le fantôme")
        assert events[-1]["kind"] == "guard", events[-1]
        log = (Path(tmp) / "decisions.jsonl").read_text()
        assert "stop_retry" in log

        # 4. Refus de l'utilisateur
        events.clear()
        agent.confirm = lambda code, why: False
        agent._claude_call = scripted([
            {"stop_reason": "tool_use", "content": [tool("execute_blender_code", bad, "t5")]},
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "D'accord."}]},
        ])
        agent.ask("essaie")
        assert any("refusé" in e.get("text", "") for e in events), events
        results["ok"] = True
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        results["error"] = exc


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    if not os.path.exists("/tmp/dummy.glb"):
        import test_pipeline_bpy
        test_pipeline_bpy.build_dummy("/tmp/dummy.glb")
        bpy.ops.wm.read_factory_settings(use_empty=True)

    server = bas_bridge.BridgeServer(PORT)
    server.start()
    tmp = tempfile.mkdtemp()
    t = threading.Thread(target=client_side, args=(tmp,), daemon=True)
    t.start()
    # Le module bpy n'a pas de boucle d'événements : on fait tourner la pompe à la main.
    while t.is_alive():
        server._pump()
        time.sleep(0.02)
    server.stop()
    if "error" in results:
        raise SystemExit(f"ÉCHEC : {results['error']}")
    print("OK — pont, pipeline en direct, agent, garde-fous")


if __name__ == "__main__":
    main()
