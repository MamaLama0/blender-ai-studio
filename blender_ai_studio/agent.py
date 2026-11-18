"""Agent IA qui pilote Blender (Claude via l'API Anthropic, ou un modèle local via Ollama).

Garde-fous inspirés du « routeur » de l'article Jev/Grok Bot, mais appliqués en dur
dans le code (pas seulement conseillés au modèle) :
  - nombre maximal d'actions par demande ;
  - la même erreur répétée N fois → on arrête et on rend la main (stop_retry) ;
  - le code Python passe par ta validation avant de tourner dans Blender (ask_human) ;
  - chaque décision est journalisée dans decisions.jsonl (pour relire après coup).
"""
from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from .bridge import BlenderBridge, BridgeError
from .config import Settings
from .router import Router

SYSTEM_PROMPT = """Tu es l'assistant 3D de « Blender AI Studio ». Tu pilotes un Blender ouvert
sur l'ordinateur de l'utilisateur à l'aide d'outils. Réponds en français, brièvement.

Conventions de la scène :
- Axe Z vers le haut, le personnage regarde vers -Y, ses pieds sont à z=0.
- Le maillage du personnage s'appelle "Character", le squelette "Character_Rig"
  (os : root, hips, spine, head, arm.L, arm.R, leg.L, leg.R ; .L = côté +X).
- Les animations sont des actions (Idle, Walk, Jump, Wave) rangées en pistes NLA.

Méthode :
1. Regarde d'abord la scène (get_scene_info) avant de modifier quoi que ce soit.
2. Pour importer, rigger, animer ou exporter, utilise run_pipeline_step plutôt que du code.
3. Pour le reste (matériaux, retouches, poses, nouvelles animations…), écris du code bpy
   court et ciblé avec execute_blender_code. Un seul objectif par appel.
4. Si un appel échoue, lis l'erreur et corrige ; ne relance jamais le même code à l'identique.
5. Si la demande est ambiguë ou risquée (supprimer des objets, écraser un fichier),
   pose une question au lieu d'agir.
"""

TOOLS = [
    {
        "name": "get_scene_info",
        "description": "Liste les objets de la scène Blender (type, position, dimensions, os, actions).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_object_info",
        "description": "Détails d'un objet : transformations, matériaux, modificateurs, groupes de sommets.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "run_pipeline_step",
        "description": (
            "Étapes prêtes à l'emploi. step=import (args: path, target_faces, height), "
            "rig (aucun arg), animate (args: kinds=[idle, walk, jump, wave], intensity), "
            "export (args: path, fmt=glb|fbx|obj)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "step": {"type": "string", "enum": ["import", "rig", "animate", "export", "info"]},
                "args": {"type": "object"},
            },
            "required": ["step"],
        },
    },
    {
        "name": "execute_blender_code",
        "description": (
            "Exécute du Python (bpy) dans Blender. Utilise print() pour renvoyer des infos. "
            "L'utilisateur peut devoir valider le code avant exécution."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "explanation": {"type": "string", "description": "Ce que fait le code, en une phrase."},
            },
            "required": ["code", "explanation"],
        },
    },
    {
        "name": "get_viewport_screenshot",
        "description": "Capture la vue 3D de Blender pour voir le résultat.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


class AgentError(RuntimeError):
    pass


def check_provider(settings: Settings, api_key: str = "") -> str:
    """Bouton « Tester » : vérifie la clé / le serveur et la présence du modèle."""
    if settings.llm_provider == "claude":
        key = api_key or settings.get_api_key()
        if not key:
            raise AgentError("Aucune clé API Claude.")
        req = urllib.request.Request("https://api.anthropic.com/v1/models?limit=100",
                                     headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
        try:
            data = json.loads(urllib.request.urlopen(req, timeout=15).read())
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AgentError("Clé API refusée par Anthropic.") from exc
            raise AgentError(f"API Claude : HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise AgentError(f"API Claude injoignable : {exc.reason}") from exc
        ids = [m.get("id", "") for m in data.get("data", [])]
        if ids and settings.claude_model not in ids:
            raise AgentError(f"Clé valide, mais {settings.claude_model} n'est pas dans la liste de tes modèles.")
        return f"Clé valide, {settings.claude_model} disponible."
    base = settings.ollama_url.rstrip("/")
    try:
        data = json.loads(urllib.request.urlopen(f"{base}/api/tags", timeout=5).read())
    except (urllib.error.URLError, OSError) as exc:
        raise AgentError(f"Ollama ne répond pas sur {base}. Lance « ollama serve ».") from exc
    names = {m.get("name", "") for m in data.get("models", [])}
    wanted = settings.ollama_model
    if wanted not in names and f"{wanted}:latest" not in names:
        raise AgentError(f"Ollama répond, mais le modèle {wanted} n'est pas téléchargé : ollama pull {wanted}")
    return f"Ollama répond, {wanted} est prêt."


def _norm_error(text: str) -> str:
    """Normalise un message d'erreur pour détecter les répétitions."""
    text = re.sub(r"0x[0-9a-f]+|\d+(\.\d+)?", "#", text.lower())
    return text.strip()[:200]


class Agent:
    def __init__(
        self,
        settings: Settings,
        on_event: Callable[[dict], None],
        confirm: Callable[[str, str], bool],
        run_step: Callable[[str, dict], dict],
        log_dir: Path,
        router: "Router | None" = None,
    ):
        self.settings = settings
        self.router = router or Router(settings, log_dir, on_event)
        self.on_event = on_event
        self.confirm = confirm
        self.run_step = run_step
        self.bridge = BlenderBridge(settings.bridge_host, settings.bridge_port)
        self.log_file = log_dir / "decisions.jsonl"
        # Un fichier par fournisseur : les formats de messages de Claude et d'Ollama diffèrent.
        self.history_file = log_dir / f"conversation-{settings.llm_provider}.json"
        self.messages: list[dict] = self._load_history()
        self.stop_requested = False

    # ------------------------------------------------------------------
    def reset(self):
        self.messages = []
        self.history_file.unlink(missing_ok=True)

    def ask(self, user_text: str) -> None:
        self.stop_requested = False
        self.goal = user_text
        if self.router.check_bypass(user_text):
            self.on_event({"kind": "guard", "text": "Routeur désactivé pour cette demande (bypass)."})
        snapshot = len(self.messages)
        try:
            self._loop(user_text)
        except Exception:
            # On ne garde pas un historique à moitié écrit (appel d'outil sans résultat…)
            del self.messages[snapshot:]
            raise
        finally:
            self._save_history()

    def _load_history(self) -> list[dict]:
        try:
            data = json.loads(self.history_file.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save_history(self):
        """Garde le fil de la conversation entre deux lancements (sans les captures d'écran)."""
        def strip(block):
            if isinstance(block, dict) and block.get("type") == "image":
                return {"type": "text", "text": "[capture retirée]"}
            if isinstance(block, dict) and isinstance(block.get("content"), list):
                return {**block, "content": [strip(b) for b in block["content"]]}
            return block

        clean = []
        for msg in self.messages[-60:]:
            content = msg.get("content")
            if isinstance(content, list):
                msg = {**msg, "content": [strip(b) for b in content]}
            clean.append(msg)
        # L'historique ne doit pas commencer par des résultats d'outils orphelins
        while clean and clean[0].get("role") != "user" or (
                clean and isinstance(clean[0].get("content"), list)
                and any(b.get("type") == "tool_result" for b in clean[0]["content"])):
            clean.pop(0)
            if not clean:
                break
        try:
            self.history_file.write_text(json.dumps(clean, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------
    def _log(self, **entry):
        entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with self.log_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _run_tool(self, name: str, args: dict, req: dict) -> tuple[str, bool, bytes | None]:
        """Renvoie (texte du résultat, erreur ?, image png éventuelle)."""
        try:
            if name == "get_scene_info":
                return json.dumps(self.bridge.send("get_scene_info"), ensure_ascii=False), False, None
            if name == "get_object_info":
                return json.dumps(self.bridge.send("get_object_info", args), ensure_ascii=False), False, None
            if name == "run_pipeline_step":
                out = self.run_step(args.get("step", ""), args.get("args") or {})
                return json.dumps(out, ensure_ascii=False), False, None
            if name == "execute_blender_code":
                return self._execute_code(args, req)
            if name == "get_viewport_screenshot":
                info = self.bridge.send("get_viewport_screenshot", {"max_size": 800})
                png = Path(info["filepath"]).read_bytes()
                return "Capture de la vue 3D jointe.", False, png
            return f"Outil inconnu : {name}", True, None
        except BridgeError as exc:
            return f"Erreur Blender : {exc}", True, None
        except Exception as exc:  # l'erreur repart au modèle, l'app ne plante pas
            return f"Erreur : {type(exc).__name__}: {exc}", True, None

    def _execute_code(self, args: dict, req: dict) -> tuple[str, bool, None]:
        code = args.get("code", "")
        explanation = args.get("explanation", "")
        req["code_runs"] += 1
        # Point de contrôle du routeur n°1 : avant d'exécuter du code dans Blender
        decision = self.router.route(
            self.goal, "code", req["last_error"], req["code_runs"] - 1,
            {"explanation": explanation[:300], "code_lines": code.count("\n") + 1,
             "steps_used": req["step"], "max_steps": self.settings.max_steps})
        applied = decision.applied
        if applied == "stop_retry":
            self.router.outcome(decision, "stop_retry")
            req["wrap_up"] = True
            req["vetoed"] = True  # refus du routeur lui-même : pas une erreur à « réessayer »
            return ("Le routeur de décision recommande d'abandonner cette approche. N'exécute plus de code : "
                    "explique en une ou deux phrases ce qui bloque et ce que tu proposes."), True, None
        if applied == "reuse_cache":
            self.router.outcome(decision, "reuse_cache")
            return ("Le routeur indique que ce résultat existe déjà dans la scène : vérifie avec get_scene_info "
                    "et réutilise-le au lieu de le refaire."), False, None
        must_ask = self.settings.confirm_code or applied == "ask_human"
        if must_ask and not self.confirm(code, explanation):
            self._log(tool="execute_blender_code", decision="ask_human", approved=False)
            self.router.outcome(decision, "ask_human", "code refusé par toi")
            return "L'utilisateur a refusé ce code. Demande-lui ce qu'il préfère.", True, None
        out = self.bridge.execute(code)
        self.router.outcome(decision, "proceed", "validé par toi" if must_ask else "")
        if applied == "research_capped":
            req["wrap_up"] = True
        return (out or "(exécuté, aucune sortie)")[-8000:], False, None

    def _after_error(self, req: dict, text: str) -> bool:
        """Après une erreur d'outil : garde-fou en dur + routeur. True = on s'arrête."""
        key = _norm_error(text)
        errors = req["errors"]
        errors[key] = errors.get(key, 0) + 1
        req["last_error"] = text[:600]
        hard_stop = errors[key] >= self.settings.max_same_error
        # Point de contrôle n°2 : avant de laisser l'IA réessayer
        decision = self.router.route(self.goal, "retry", text, errors[key] - 1,
                                     {"steps_used": req["step"], "max_steps": self.settings.max_steps,
                                      "distinct_errors": len(errors)})
        if hard_stop:
            self._log(decision="stop_retry", error=text[:300])
            self.router.outcome(decision, "stop_retry", "garde-fou : même erreur répétée")
            self.on_event({"kind": "guard", "text": (
                "J'arrête : la même erreur revient. Dis-moi comment tu veux continuer.\n" + text[:400])})
            return True
        if decision.applied in ("stop_retry", "ask_human"):
            self.router.outcome(decision, decision.applied)
            self.on_event({"kind": "guard", "text": (
                f"Le routeur conseille de {decision.label} ({round(decision.confidence * 100)} %). "
                "Dis-moi comment tu veux continuer.\n" + text[:300])})
            return True
        if decision.applied == "research_capped":
            req["wrap_up"] = True
        self.router.outcome(decision, "proceed")
        return False

    def _checkpoint_long_run(self, req: dict) -> bool:
        """Point de contrôle n°3 : à mi-budget, faut-il continuer ? True = on s'arrête."""
        if req["step"] != max(4, self.settings.max_steps // 2) or req["wrap_up"]:
            return False
        decision = self.router.route(self.goal, "long_run", req["last_error"], req["step"],
                                     {"steps_used": req["step"], "max_steps": self.settings.max_steps,
                                      "errors_so_far": sum(req["errors"].values()),
                                      "code_runs": req["code_runs"]})
        if decision.applied == "ask_human":
            self.router.outcome(decision, "ask_human")
            self.on_event({"kind": "guard", "text": (
                f"Le routeur préfère te demander avant de continuer ({req['step']} actions déjà faites). "
                "Je continue ?")})
            return True
        if decision.applied in ("research_capped", "stop_retry"):
            req["wrap_up"] = True
            self.router.outcome(decision, decision.applied)
        else:
            self.router.outcome(decision, "proceed")
        return False

    # ------------------------------------------------------------------
    # Boucle commune (Claude ou Ollama)
    # ------------------------------------------------------------------
    def _loop(self, user_text: str):
        claude = self.settings.llm_provider == "claude"
        last = self.messages[-1] if self.messages else None
        if claude and last and last["role"] == "user" and isinstance(last["content"], list):
            # Une demande précédente s'est arrêtée juste après des résultats d'outils
            last["content"].append({"type": "text", "text": user_text})
        else:
            self.messages.append({"role": "user", "content": user_text})
        req = {"errors": {}, "last_error": "", "code_runs": 0, "step": 0, "wrap_up": False}
        for step in range(self.settings.max_steps):
            req["step"] = step
            if self.stop_requested:
                self.on_event({"kind": "guard", "text": "Arrêté à ta demande."})
                return
            if self._checkpoint_long_run(req):
                return
            final = req["wrap_up"]
            if final:
                self.on_event({"kind": "guard", "text": "Le routeur demande de conclure : dernière réponse."})
            calls = self._model_turn(claude, final)
            if final or not calls:
                return
            stop = False
            results = []
            for call_id, name, args in calls:
                self.on_event({"kind": "tool", "name": name, "input": args})
                text, is_err, png = self._run_tool(name, args, req)
                self._log(tool=name, step=step, error=is_err, result=text[:300])
                self.on_event({"kind": "tool_result", "name": name, "error": is_err, "text": text[:2000]})
                results.append((call_id, name, text, is_err, png))
                if is_err and not req.pop("vetoed", False) and self._after_error(req, text):
                    stop = True
            self._push_results(claude, results)
            if stop:
                return
        self._log(decision="max_steps")
        self.on_event({"kind": "guard", "text": (
            f"J'ai atteint la limite de {self.settings.max_steps} actions pour cette demande. "
            "Relance si tu veux que je continue.")})

    def _model_turn(self, claude: bool, final: bool) -> list[tuple[str, str, dict]]:
        """Un appel au modèle. `final` : réponse en texte seulement (plus d'outils)."""
        if claude:
            resp = self._claude_call(final=final)
            content = resp.get("content", [])
            self.messages.append({"role": "assistant", "content": content})
            for block in content:
                if block.get("type") == "text" and block["text"].strip():
                    self.on_event({"kind": "assistant", "text": block["text"]})
            if resp.get("stop_reason") != "tool_use":
                return []
            return [(b["id"], b["name"], b.get("input") or {}) for b in content if b.get("type") == "tool_use"]
        msg = self._ollama_call(final=final).get("message", {})
        if final:
            msg.pop("tool_calls", None)
        self.messages.append(msg)
        if msg.get("content", "").strip():
            self.on_event({"kind": "assistant", "text": msg["content"]})
        calls = []
        for call in msg.get("tool_calls") or []:
            fn = call.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            calls.append(("", fn.get("name", ""), args))
        return calls

    def _push_results(self, claude: bool, results):
        if claude:
            blocks = []
            for call_id, _name, text, is_err, png in results:
                content: list[dict] = [{"type": "text", "text": text}]
                if png:
                    content.append({"type": "image", "source": {
                        "type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()}})
                blocks.append({"type": "tool_result", "tool_use_id": call_id, "content": content,
                               "is_error": is_err})
            self.messages.append({"role": "user", "content": blocks})
        else:
            for _id, name, text, _err, _png in results:
                self.messages.append({"role": "tool", "tool_name": name, "content": text})

    # ------------------------------------------------------------------
    # Appels aux modèles
    # ------------------------------------------------------------------
    def _claude_call(self, final: bool = False) -> dict:
        key = self.settings.get_api_key()
        if not key:
            raise AgentError("Pas de clé API Claude. Ajoute-la dans Réglages.")
        self._trim_images()
        tools = [dict(t) for t in TOOLS]
        tools[-1]["cache_control"] = {"type": "ephemeral"}  # outils + consignes mis en cache
        body = {
            "model": self.settings.claude_model,
            "max_tokens": 4096,
            "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            "tools": tools,
            "messages": self.messages,
        }
        if final:
            body["tool_choice"] = {"type": "none"}  # conclure en texte, sans nouvel appel d'outil
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(body).encode(),
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
        )
        try:
            return json.loads(urllib.request.urlopen(req, timeout=300).read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise AgentError(f"API Claude : HTTP {exc.code} — {detail}") from exc
        except urllib.error.URLError as exc:
            raise AgentError(f"API Claude injoignable : {exc.reason}") from exc

    def _trim_images(self):
        """Ne garde que la dernière capture d'écran : les anciennes coûtent cher pour rien."""
        seen_last = False
        for msg in reversed(self.messages):
            if msg["role"] != "user" or not isinstance(msg["content"], list):
                continue
            for block in msg["content"]:
                if block.get("type") != "tool_result" or not isinstance(block.get("content"), list):
                    continue
                kept = []
                for part in block["content"]:
                    if part.get("type") == "image":
                        if seen_last:
                            kept.append({"type": "text", "text": "[ancienne capture retirée]"})
                            continue
                        seen_last = True
                    kept.append(part)
                block["content"] = kept

    def _ollama_call(self, final: bool = False) -> dict:
        tools = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
            for t in TOOLS if t["name"] != "get_viewport_screenshot"]
        body = {"model": self.settings.ollama_model, "stream": False,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + self.messages}
        if not final:
            body["tools"] = tools
        req = urllib.request.Request(f"{self.settings.ollama_url.rstrip('/')}/api/chat",
                                     data=json.dumps(body).encode(),
                                     headers={"content-type": "application/json"})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=600).read())
        except urllib.error.HTTPError as exc:
            raise AgentError(f"Ollama : HTTP {exc.code} — {exc.read().decode(errors='replace')[:300]}") from exc
        except urllib.error.URLError as exc:
            raise AgentError(f"Ollama injoignable ({self.settings.ollama_url}). Lance `ollama serve`.") from exc
