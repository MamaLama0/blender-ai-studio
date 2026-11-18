"""Tests du routeur de décision (Jev ou équivalent), sans réseau.

Un faux module `typesafe_sdk` vérifie qu'on appelle Jev exactement comme l'article :
client en gestionnaire de contexte, modèle jev-latest, system_one(state, questions)
avec une question Choice « action » à cinq options, lecture de choices["action"].

python tests/test_router.py
"""
import io
import json
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from blender_ai_studio import router as R  # noqa: E402
from blender_ai_studio.agent import Agent  # noqa: E402
from blender_ai_studio.config import Settings  # noqa: E402

calls = []


def check(label, cond):
    print(("OK   " if cond else "ÉCHEC"), label)
    if not cond:
        raise SystemExit(1)


# ---------------------------------------------------------------- faux SDK TypeSafe
class FakeTypeSafeError(Exception):
    pass


class FakeChoice:
    def __init__(self, instructions=None, criteria=None):
        self.instructions, self.criteria = instructions, criteria


class FakeAnswer:
    def __init__(self, choice, confidence, probabilities):
        self.choice, self.confidence, self.probabilities = choice, confidence, probabilities


class FakeClient:
    next_answer = ("stop_retry", 0.82)
    fail = False

    def __init__(self, api_key=None, model=None, timeout=None, **kw):
        calls.append({"api_key": api_key, "model": model, "timeout": timeout})
        self.entered = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *a):
        calls[-1]["closed"] = True

    def system_one(self, state, questions, **kw):
        assert self.entered, "le client doit être utilisé en gestionnaire de contexte"
        calls[-1]["state"] = state
        calls[-1]["questions"] = questions
        if FakeClient.fail:
            raise FakeTypeSafeError("429 rate limited")
        action, conf = FakeClient.next_answer
        probs = {a: (conf if a == action else (1 - conf) / 4) for a in R.ACTIONS}
        return types.SimpleNamespace(choices={"action": FakeAnswer(action, conf, probs)})


fake_sdk = types.ModuleType("typesafe_sdk")
fake_sdk.TypeSafeClient = FakeClient
fake_sdk.Choice = FakeChoice
fake_sdk.TypeSafeError = FakeTypeSafeError
sys.modules["typesafe_sdk"] = fake_sdk


def settings(**kw):
    s = Settings(workspace=tempfile.mkdtemp(), **kw)
    s._fallback_typesafe_key = "ts-test-key"
    s.get_secret = lambda name: {"typesafe": "ts-test-key", "anthropic": "sk-test"}.get(name, "")
    s.get_api_key = lambda: "sk-test"
    return s


def main():
    tmp = Path(tempfile.mkdtemp())

    # 1. Routeur coupé : « continuer », rien n'est appelé ni journalisé
    r = R.Router(settings(router_provider="none"), tmp)
    d = r.route("but", "retry", "err", 2)
    check("coupé → continuer sans appel", d.applied == "proceed" and not calls
          and not (tmp / "decisions.jsonl").exists())

    # 2. Jev en observation : appelé comme dans l'article, décision notée mais pas appliquée
    events = []
    st = settings(router_provider="jev", router_mode="shadow")
    r = R.Router(st, tmp, events.append)
    d = r.route("Ajoute un chapeau", "retry", "NameError: hat", 2, {"steps_used": 3})
    c = calls[-1]
    check("Jev : modèle jev-latest + clé", c["model"] == "jev-latest" and c["api_key"] == "ts-test-key")
    check("Jev : les 4 champs de l'article", {"goal", "task_type", "last_error", "repeats"} <= set(c["state"]))
    q = c["questions"]["action"]
    check("Jev : question Choice « action » à 5 options", isinstance(q, FakeChoice)
          and list(q.criteria) == R.ACTIONS)
    check("Jev : client refermé", c.get("closed"))
    check("observation : recommandation notée, pas appliquée",
          d.action == "stop_retry" and d.applied == "proceed" and abs(d.confidence - 0.82) < 1e-9)
    check("observation : probabilités gardées", set(d.probabilities) == set(R.ACTIONS))
    check("événement émis pour l'interface", events and events[-1]["kind"] == "router"
          and events[-1]["label"] == "arrêter de réessayer")
    r.outcome(d, "proceed", "l'app a réessayé")

    # 3. Mode actif : appliqué ; confiance trop basse → comportement normal ; ask_human toujours
    st.router_mode = "active"
    d = R.Router(st, tmp).route("but", "retry", "e", 1)
    check("actif : décision appliquée", d.applied == "stop_retry")
    FakeClient.next_answer = ("reuse_cache", 0.40)
    d = R.Router(st, tmp).route("but", "rerun_step", "", 0)
    check("actif : confiance 40 % < seuil → continuer", d.applied == "proceed")
    FakeClient.next_answer = ("ask_human", 0.30)
    d = R.Router(st, tmp).route("but", "code", "", 0)
    check("actif : « te demander » jamais contourné, même à 30 %", d.applied == "ask_human")

    # 4. Panne de Jev : l'app continue (les garde-fous en dur restent)
    FakeClient.fail = True
    d = R.Router(st, tmp).route("but", "retry", "e", 1)
    check("panne → continuer + erreur notée", d.applied == "proceed" and "429" in d.error)
    FakeClient.fail = False

    # 5. Bypass dans la demande
    r = R.Router(st, tmp)
    n = len(calls)
    r.check_bypass("bypass jev, ajoute un chapeau")
    d = r.route("but", "retry", "e", 1)
    check("« bypass jev » → routeur muet", d.applied == "proceed" and len(calls) == n)

    # 6. Équivalent Claude Haiku : réponse forcée par un outil « decide »
    sent = {}

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data)
        sent.update(body)
        if "anthropic" in req.full_url:
            payload = {"content": [{"type": "tool_use", "name": "decide",
                                    "input": {"action": "research_capped", "confidence": 0.7}}]}
        else:
            payload = {"message": {"content": json.dumps({"action": "reuse_cache", "confidence": 0.9})}}
        return io.BytesIO(json.dumps(payload).encode())

    R.urllib.request.urlopen = fake_urlopen
    d = R.Router(settings(router_provider="claude", router_mode="active"), tmp).route("b", "long_run", "", 6)
    check("Claude Haiku : modèle + outil forcé", sent["model"] == "claude-haiku-5-5"
          and sent["tool_choice"] == {"type": "tool", "name": "decide"})
    check("Claude Haiku : décision lue", d.applied == "research_capped")

    # 7. Équivalent local Ollama : sortie JSON contrainte par schéma
    d = R.Router(settings(router_provider="ollama", router_mode="active"), tmp).route("b", "generation", "", 0,
                                                                                    {"cached_result_available": True})
    check("Ollama : format JSON imposé", sent["format"]["properties"]["action"]["enum"] == R.ACTIONS)
    check("Ollama : décision lue", d.applied == "reuse_cache")

    # 8. Rapport : décisions, accord, désaccords
    proj = Path(st.workspace) / "pingouin"
    proj.mkdir()
    (tmp / "decisions.jsonl").rename(proj / "decisions.jsonl")
    text = R.report(Path(st.workspace))
    check("rapport : compte et désaccord", "Décisions" in text and "l'app a réessayé" in text
          and "arrêter de réessayer" in text)

    # 9. Agent + routeur actif (faux Blender, IA simulée)
    class FakeBridge:
        def __init__(self):
            self.ran = []

        def execute(self, code):
            self.ran.append(code)
            if "boom" in code:
                raise R.RouterError("KeyError: 'Fantome'")
            return "ok"

        def send(self, kind, params=None):
            return {"objects": ["Character"]}

    answers = []

    def ask(_settings, state):
        answers.append(state["task_type"])
        action, conf = script.pop(0)
        return action, conf, {}

    def run_agent(model_turns, confirm_code=False, text="Ajoute un chapeau", route_script=()):
        global script
        script = list(route_script)
        st2 = settings(router_provider="jev", router_mode="active", confirm_code=confirm_code)
        events.clear()
        asked = []
        logdir = Path(tempfile.mkdtemp())
        router = R.Router(st2, logdir, events.append, ask=ask)
        ag = Agent(st2, on_event=events.append, confirm=lambda c, w: asked.append(w) or True,
                   run_step=lambda s, a: {}, log_dir=logdir, router=router)
        ag.bridge = FakeBridge()
        finals = []
        it = iter(model_turns)

        def fake_call(final=False):
            finals.append(final)
            return next(it)

        ag._claude_call = fake_call
        ag.ask(text)
        return ag, asked, finals

    def code(tid, src, why="x"):
        return {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": tid, "name": "execute_blender_code", "input": {"code": src, "explanation": why}}]}

    done = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Fini."}]}

    # a. avant le code : « arrêter » → le code ne tourne pas, l'IA doit conclure sans outil
    ag, asked, finals = run_agent([code("t1", "print(1)"), done], route_script=[("stop_retry", 0.9)])
    check("code : routeur consulté avant exécution", answers[-1] == "code")
    check("code : « arrêter » → rien exécuté", ag.bridge.ran == [])
    check("code : dernière réponse forcée en texte", finals == [False, True])

    # b. avant le code : « te demander » force la validation même si elle est désactivée
    ag, asked, finals = run_agent([code("t1", "print(1)", "pose un chapeau"), done],
                                  route_script=[("ask_human", 0.6)])
    check("code : « te demander » → validation demandée", asked == ["pose un chapeau"] and ag.bridge.ran)

    # c. après une erreur : « arrêter de réessayer » coupe avant le garde-fou en dur
    ag, asked, finals = run_agent([code("t1", "boom()"), code("t2", "boom()"), done],
                                  route_script=[("proceed", 0.9), ("stop_retry", 0.8)])
    check("réessai : routeur consulté après l'erreur", answers[-1] == "retry")
    check("réessai : arrêt dès la 1re erreur", len(ag.bridge.ran) == 1
          and any(e.get("kind") == "guard" and "routeur" in e.get("text", "").lower() for e in events))

    # d. garde-fou en dur toujours actif si le routeur dit « continuer »
    ag, asked, finals = run_agent([code("t1", "boom()"), code("t2", "boom()"), code("t3", "boom()"), done],
                                  route_script=[("proceed", 0.9)] * 6)
    check("garde-fou en dur malgré « continuer »", len(ag.bridge.ran) == 2)

    # e. bypass : aucune consultation
    n = len(answers)
    run_agent([code("t1", "print(1)"), done], text="bypass jev : ajoute un chapeau")
    check("agent : « bypass jev » respecté", len(answers) == n)

    print("Tout est bon.")


if __name__ == "__main__":
    main()
