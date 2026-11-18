"""Routeur de décision, d'après l'article « How To Wire Jev Into Grok Bot ».

Avant un geste coûteux ou risqué (réessayer après une erreur, exécuter du code,
relancer une génération 3D, refaire une étape déjà faite, prolonger une longue série
d'actions), l'application pose UNE question à un petit modèle de décision et reçoit
une action typée, pas de la prose :

    proceed          continuer comme prévu
    reuse_cache      un résultat existant suffit : le réutiliser, sauter le travail
    stop_retry       abandonner cette approche et dire pourquoi
    research_capped  limiter l'effort : finir vite avec ce qu'on a
    ask_human        s'arrêter et demander à la personne (jamais contourné)

Fournisseurs :
    jev     Jev de TypeSafe (pip install typesafe-sdk, clé TYPESAFE_API_KEY)
    claude  équivalent avec Claude Haiku (même question, réponse forcée en JSON)
    ollama  équivalent local avec un petit modèle Ollama

Modes :
    shadow  le routeur est consulté et sa décision notée, mais rien ne change ;
            on note aussi ce que l'application a réellement fait, pour comparer
            (rapport-routeur.md). C'est la « semaine d'observation » de l'article.
    active  la décision est appliquée (si sa confiance dépasse le seuil).

Les garde-fous en dur (budget d'actions, arrêt sur erreur répétée, validation du code)
restent actifs dans tous les cas : le routeur ne peut que rendre l'app plus prudente
ou plus économe, jamais lever une protection.

Kill switches : choisir « Aucun » dans les réglages, ou écrire « bypass jev » (ou
« sans routeur ») dans une demande à l'assistant.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .config import Settings

ACTIONS = ["proceed", "reuse_cache", "stop_retry", "research_capped", "ask_human"]

CRITERIA = {
    "proceed": "Continue as planned: the step is useful and nothing suggests it will fail or waste work.",
    "reuse_cache": "An existing result already covers this (cached_result_available is true and still valid): "
                   "reuse it and skip the work.",
    "stop_retry": "Abandon this approach: it keeps failing the same way, or retrying cannot fix the error. "
                  "Report why instead of trying again.",
    "research_capped": "Limit the effort: the task is sprawling or has used many steps; finish quickly "
                       "with what is already done and summarize.",
    "ask_human": "Pause and ask the person: the request is ambiguous, destructive, expensive or outside "
                 "what the agent can verify.",
}

INSTRUCTIONS = (
    "You route the next step of an agent that drives Blender to build, rig and animate a 3D character "
    "for a person on their own computer. Read the state (goal, task_type, last_error, repeats, context) "
    "and pick the single best action for the step that is about to run."
)

LABELS_FR = {
    "proceed": "continuer",
    "reuse_cache": "réutiliser l'existant",
    "stop_retry": "arrêter de réessayer",
    "research_capped": "limiter l'effort",
    "ask_human": "te demander",
}

BYPASS_WORDS = ("bypass jev", "bypass router", "sans routeur")


class RouterError(RuntimeError):
    pass


@dataclass
class Decision:
    id: str
    action: str                 # ce que le routeur recommande
    confidence: float
    applied: str                # ce que l'app fait de cette recommandation (mode actif)
    probabilities: dict = field(default_factory=dict)
    provider: str = "none"
    mode: str = "shadow"
    latency_ms: int = 0
    error: str = ""

    @property
    def label(self) -> str:
        return LABELS_FR.get(self.action, self.action)


def build_state(goal: str, task_type: str, last_error: str = "", repeats: int = 0,
                context: dict | None = None) -> dict:
    """Les quatre champs de l'article, plus un peu de contexte (jamais de clé ni de fichier)."""
    return {
        "goal": goal[:600],
        "task_type": task_type,
        "last_error": (last_error or "")[:600],
        "repeats": int(repeats),
        "context": context or {},
    }


# ---------------------------------------------------------------------------
# Fournisseurs
# ---------------------------------------------------------------------------
def _ask_jev(settings: Settings, state: dict) -> tuple[str, float, dict]:
    try:
        from typesafe_sdk import Choice, TypeSafeClient, TypeSafeError
    except ImportError as exc:
        raise RouterError("Le SDK de Jev n'est pas installé : pip install typesafe-sdk") from exc
    key = settings.get_secret("typesafe")
    if not key:
        raise RouterError("Pas de clé TypeSafe (Réglages → Routeur).")
    try:
        with TypeSafeClient(api_key=key, model=settings.router_jev_model,
                            timeout=settings.router_timeout) as client:
            response = client.system_one(
                state=state,
                questions={"action": Choice(instructions=INSTRUCTIONS, criteria=CRITERIA)},
            )
    except TypeSafeError as exc:
        raise RouterError(f"Jev : {exc}") from exc
    answer = response.choices["action"]
    return answer.choice, float(answer.confidence), dict(answer.probabilities)


_DECIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ACTIONS},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["action", "confidence"],
}


def _llm_prompt(state: dict) -> str:
    options = "\n".join(f"- {name}: {text}" for name, text in CRITERIA.items())
    return (f"{INSTRUCTIONS}\n\nActions:\n{options}\n\nState:\n{json.dumps(state, ensure_ascii=False)}\n\n"
            "Answer with the action and your confidence from 0 to 1.")


def _ask_claude(settings: Settings, state: dict) -> tuple[str, float, dict]:
    key = settings.get_api_key()
    if not key:
        raise RouterError("Pas de clé API Claude pour le routeur.")
    body = {
        "model": settings.router_claude_model,
        "max_tokens": 200,
        "tools": [{"name": "decide", "description": "Return the routing decision.",
                   "input_schema": _DECIDE_SCHEMA}],
        "tool_choice": {"type": "tool", "name": "decide"},
        "messages": [{"role": "user", "content": _llm_prompt(state)}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=settings.router_timeout).read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RouterError(f"Routeur Claude : {exc}") from exc
    for block in data.get("content", []):
        if block.get("type") == "tool_use":
            out = block.get("input", {})
            return out.get("action", ""), float(out.get("confidence", 0)), {}
    raise RouterError("Routeur Claude : pas de décision dans la réponse.")


def _ask_ollama(settings: Settings, state: dict) -> tuple[str, float, dict]:
    body = {
        "model": settings.router_ollama_model, "stream": False, "format": _DECIDE_SCHEMA,
        "messages": [{"role": "user", "content": _llm_prompt(state)}],
        "options": {"temperature": 0},
    }
    req = urllib.request.Request(f"{settings.ollama_url.rstrip('/')}/api/chat",
                                 data=json.dumps(body).encode(), headers={"content-type": "application/json"})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=max(settings.router_timeout, 30)).read())
        out = json.loads(data.get("message", {}).get("content", "{}"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RouterError(f"Routeur Ollama : {exc}") from exc
    return out.get("action", ""), float(out.get("confidence", 0)), {}


PROVIDERS: dict[str, Callable[[Settings, dict], tuple[str, float, dict]]] = {
    "jev": _ask_jev,
    "claude": _ask_claude,
    "ollama": _ask_ollama,
}


# ---------------------------------------------------------------------------
# Routeur
# ---------------------------------------------------------------------------
class Router:
    def __init__(self, settings: Settings, log_dir: Path | None,
                 on_event: Callable[[dict], None] | None = None,
                 ask: Callable[[Settings, dict], tuple[str, float, dict]] | None = None):
        self.settings = settings
        self.log_dir = log_dir
        self.on_event = on_event or (lambda e: None)
        self._ask = ask  # pour les tests : remplace l'appel réseau
        self.bypassed = False

    @property
    def enabled(self) -> bool:
        return self.settings.router_provider in PROVIDERS and not self.bypassed

    def check_bypass(self, text: str) -> bool:
        """« bypass jev » dans une demande : le routeur se tait pour cette demande."""
        self.bypassed = any(word in text.lower() for word in BYPASS_WORDS)
        return self.bypassed

    def route(self, goal: str, task_type: str, last_error: str = "", repeats: int = 0,
              context: dict | None = None) -> Decision:
        """Toujours une décision : si le routeur est coupé ou en panne, c'est « proceed »
        (les garde-fous en dur continuent de protéger)."""
        provider = self.settings.router_provider
        mode = self.settings.router_mode
        decision = Decision(id=uuid.uuid4().hex[:12], action="proceed", confidence=1.0,
                            applied="proceed", provider=provider, mode=mode)
        if not self.enabled:
            return decision
        state = build_state(goal, task_type, last_error, repeats, context)
        t0 = time.time()
        try:
            ask = self._ask or PROVIDERS[provider]
            action, confidence, probabilities = ask(self.settings, state)
            if action not in ACTIONS:
                raise RouterError(f"action inconnue : {action!r}")
            decision.action, decision.confidence, decision.probabilities = action, confidence, probabilities
        except Exception as exc:  # le routeur ne doit jamais bloquer l'app
            decision.error = str(exc)[:300]
        decision.latency_ms = int((time.time() - t0) * 1000)

        if decision.error:
            decision.applied = "proceed"
        elif mode != "active":
            decision.applied = "proceed"           # observation : on note, on ne touche à rien
        elif decision.action == "ask_human":
            decision.applied = "ask_human"         # jamais contourné, quelle que soit la confiance
        elif decision.confidence < self.settings.router_min_confidence:
            decision.applied = "proceed"           # pas assez sûr : on garde le comportement normal
        else:
            decision.applied = decision.action

        self._log({"type": "route", "id": decision.id, "task_type": task_type, "state": state,
                   **{k: v for k, v in asdict(decision).items() if k != "id"}})
        self.on_event({"kind": "router", "task_type": task_type, **asdict(decision), "label": decision.label})
        return decision

    def outcome(self, decision: Decision, actual: str, note: str = ""):
        """Ce que l'app a réellement fait, pour le rapport (comparaison routeur / réalité)."""
        if decision.provider not in PROVIDERS:
            return
        self._log({"type": "outcome", "id": decision.id, "actual": actual, "note": note[:200]})

    def _log(self, entry: dict):
        if not self.log_dir:
            return
        entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with (self.log_dir / "decisions.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass


def check(settings: Settings) -> str:
    """Bouton « Tester » : une vraie décision sur un cas d'école."""
    if settings.router_provider not in PROVIDERS:
        return "Routeur coupé : seuls les garde-fous en dur s'appliquent."
    state = build_state("Add a cowboy hat to the character", "retry",
                        "NameError: name 'hat' is not defined", 3)
    t0 = time.time()
    action, confidence, _ = PROVIDERS[settings.router_provider](settings, state)
    ms = int((time.time() - t0) * 1000)
    if action not in ACTIONS:
        raise RouterError(f"Réponse inattendue : {action!r}")
    return f"Le routeur répond en {ms} ms : « {LABELS_FR[action]} » ({pct(confidence)}) sur un cas test."


# ---------------------------------------------------------------------------
# Rapport (le report.md de l'article)
# ---------------------------------------------------------------------------
def report(workspace: Path) -> str:
    """Compare, pour chaque décision, ce que le routeur recommandait et ce que l'app a fait."""
    routes, outcomes = {}, {}
    for log in sorted(workspace.glob("*/decisions.jsonl")):
        project = log.parent.name
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("type") == "route":
                entry["project"] = project
                routes[entry["id"]] = entry
            elif entry.get("type") == "outcome":
                outcomes[entry["id"]] = entry

    lines = [f"# Rapport du routeur ({time.strftime('%d/%m/%Y %H:%M')})", ""]
    if not routes:
        lines += ["Aucune décision enregistrée pour l'instant. Choisis un routeur dans Réglages,",
                  "en mode observation, et utilise l'app normalement quelques jours."]
        return "\n".join(lines) + "\n"

    ok = [r for r in routes.values() if not r.get("error")]
    errors = len(routes) - len(ok)
    counts = Counter(r["action"] for r in ok)
    by_task = Counter(r["task_type"] for r in routes.values())
    mean_conf = sum(r["confidence"] for r in ok) / len(ok) if ok else 0
    latencies = sorted(r.get("latency_ms", 0) for r in ok)
    median_ms = latencies[len(latencies) // 2] if latencies else 0
    providers = Counter(r.get("provider") for r in routes.values())

    lines += [
        f"- Décisions : **{len(routes)}** ({', '.join(f'{p} : {n}' for p, n in providers.items())})",
        f"- Confiance moyenne : **{pct(mean_conf)}**, temps de réponse médian : **{median_ms} ms**",
        f"- Pannes du routeur (l'app a continué normalement) : **{errors}**",
        "",
        "## Ce que le routeur recommande",
        "",
        "| Action | Nombre |",
        "|---|---|",
        *[f"| {LABELS_FR.get(a, a)} | {counts.get(a, 0)} |" for a in ACTIONS],
        "",
        "## À quels moments",
        "",
        "| Moment | Nombre |",
        "|---|---|",
        *[f"| {TASK_LABELS.get(t, t)} | {n} |" for t, n in by_task.most_common()],
        "",
        "## Désaccords : le routeur aurait fait autrement",
        "",
    ]
    disagreements = []
    for rid, r in routes.items():
        actual = outcomes.get(rid, {}).get("actual")
        if r.get("error") or actual is None or actual == r["action"]:
            continue
        disagreements.append((r, actual, outcomes[rid].get("note", "")))
    if not disagreements:
        lines.append("Aucun : ce que le routeur recommandait correspond à ce que l'app a fait.")
    else:
        lines += ["| Quand | Projet | Moment | Routeur | Réalité | Détail |", "|---|---|---|---|---|---|"]
        for r, actual, note in disagreements[-50:]:
            goal = r["state"].get("goal", "")[:60].replace("|", "/")
            lines.append(f"| {r['ts'][5:16].replace('T', ' ')} | {r['project']} | "
                         f"{TASK_LABELS.get(r['task_type'], r['task_type'])} | "
                         f"{LABELS_FR.get(r['action'], r['action'])} ({pct(r['confidence'])}) | "
                         f"{LABELS_FR.get(actual, actual)} | {goal}{' — ' + note if note else ''} |")
    agree = sum(1 for rid, r in routes.items()
                if not r.get("error") and outcomes.get(rid, {}).get("actual") == r["action"])
    compared = sum(1 for rid, r in routes.items() if not r.get("error") and rid in outcomes)
    if compared:
        lines += ["", f"Accord routeur / réalité : **{agree}/{compared}** ({pct(agree / compared)})."]
    lines += ["", "Passe en mode actif quand les désaccords te paraissent être de bonnes idées du routeur."]
    return "\n".join(lines) + "\n"


def pct(x: float) -> str:
    """0.82 → « 82 % » (espace insécable, à la française)."""
    return f"{round(x * 100)}\u00a0%"


TASK_LABELS = {
    "retry": "réessayer après une erreur",
    "code": "exécuter du code",
    "generation": "générer un modèle 3D",
    "rerun_step": "refaire une étape",
    "long_run": "longue série d'actions",
}
