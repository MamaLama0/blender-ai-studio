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
    ):
        self.settings = settings
        self.on_event = on_event
        self.confirm = confirm
        self.run_step = run_step
        self.bridge = BlenderBridge(settings.bridge_host, settings.bridge_port)
        self.log_file = log_dir / "decisions.jsonl"
        self.messages: list[dict] = []
        self.stop_requested = False

    # ------------------------------------------------------------------
    def reset(self):
        self.messages = []

    def ask(self, user_text: str) -> None:
        self.stop_requested = False
        if self.settings.llm_provider == "claude":
            self._loop_claude(user_text)
        else:
            self._loop_ollama(user_text)

    # ------------------------------------------------------------------
    def _log(self, **entry):
        entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with self.log_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _run_tool(self, name: str, args: dict) -> tuple[str, bool, bytes | None]:
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
                code = args.get("code", "")
                explanation = args.get("explanation", "")
                if self.settings.confirm_code and not self.confirm(code, explanation):
                    self._log(tool=name, decision="ask_human", approved=False)
                    return "L'utilisateur a refusé ce code. Demande-lui ce qu'il préfère.", True, None
                out = self.bridge.execute(code)
                return (out or "(exécuté, aucune sortie)")[-8000:], False, None
            if name == "get_viewport_screenshot":
                info = self.bridge.send("get_viewport_screenshot", {"max_size": 800})
                png = Path(info["filepath"]).read_bytes()
                return "Capture de la vue 3D jointe.", False, png
            return f"Outil inconnu : {name}", True, None
        except BridgeError as exc:
            return f"Erreur Blender : {exc}", True, None
        except Exception as exc:  # l'erreur repart au modèle, l'app ne plante pas
            return f"Erreur : {type(exc).__name__}: {exc}", True, None

    def _guard(self, errors: dict[str, int], text: str) -> bool:
        """True si on doit arrêter (même erreur répétée)."""
        key = _norm_error(text)
        errors[key] = errors.get(key, 0) + 1
        if errors[key] >= self.settings.max_same_error:
            self._log(decision="stop_retry", error=text[:300])
            self.on_event({"kind": "guard", "text": (
                "J'arrête : la même erreur revient. Dis-moi comment tu veux continuer.\n" + text[:400])})
            return True
        return False

    # ------------------------------------------------------------------
    # Claude (API Anthropic)
    # ------------------------------------------------------------------
    def _claude_call(self) -> dict:
        key = self.settings.get_api_key()
        if not key:
            raise AgentError("Pas de clé API Claude. Ajoute-la dans Réglages.")
        body = {
            "model": self.settings.claude_model,
            "max_tokens": 4096,
            "system": SYSTEM_PROMPT,
            "tools": TOOLS,
            "messages": self.messages,
        }
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

    def _loop_claude(self, user_text: str):
        self.messages.append({"role": "user", "content": user_text})
        errors: dict[str, int] = {}
        for step in range(self.settings.max_steps):
            if self.stop_requested:
                self.on_event({"kind": "guard", "text": "Arrêté à ta demande."})
                return
            resp = self._claude_call()
            content = resp.get("content", [])
            self.messages.append({"role": "assistant", "content": content})
            for block in content:
                if block.get("type") == "text" and block["text"].strip():
                    self.on_event({"kind": "assistant", "text": block["text"]})
            calls = [b for b in content if b.get("type") == "tool_use"]
            if resp.get("stop_reason") != "tool_use" or not calls:
                return
            results = []
            stop = False
            for call in calls:
                self.on_event({"kind": "tool", "name": call["name"], "input": call.get("input", {})})
                text, is_err, png = self._run_tool(call["name"], call.get("input") or {})
                self._log(tool=call["name"], step=step, error=is_err, result=text[:300])
                self.on_event({"kind": "tool_result", "name": call["name"], "error": is_err, "text": text[:2000]})
                blocks: list[dict] = [{"type": "text", "text": text}]
                if png:
                    blocks.append({"type": "image", "source": {
                        "type": "base64", "media_type": "image/png",
                        "data": base64.b64encode(png).decode()}})
                results.append({"type": "tool_result", "tool_use_id": call["id"],
                                "content": blocks, "is_error": is_err})
                if is_err and self._guard(errors, text):
                    stop = True
            self.messages.append({"role": "user", "content": results})
            if stop:
                return
        self._log(decision="max_steps")
        self.on_event({"kind": "guard", "text": (
            f"J'ai atteint la limite de {self.settings.max_steps} actions pour cette demande. "
            "Relance si tu veux que je continue.")})

    # ------------------------------------------------------------------
    # Ollama (modèle local)
    # ------------------------------------------------------------------
    def _ollama_call(self) -> dict:
        tools = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
            for t in TOOLS if t["name"] != "get_viewport_screenshot"]
        body = {"model": self.settings.ollama_model, "stream": False, "tools": tools,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + self.messages}
        req = urllib.request.Request(f"{self.settings.ollama_url.rstrip('/')}/api/chat",
                                     data=json.dumps(body).encode(),
                                     headers={"content-type": "application/json"})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=600).read())
        except urllib.error.HTTPError as exc:
            raise AgentError(f"Ollama : HTTP {exc.code} — {exc.read().decode(errors='replace')[:300]}") from exc
        except urllib.error.URLError as exc:
            raise AgentError(f"Ollama injoignable ({self.settings.ollama_url}). Lance `ollama serve`.") from exc

    def _loop_ollama(self, user_text: str):
        self.messages.append({"role": "user", "content": user_text})
        errors: dict[str, int] = {}
        for step in range(self.settings.max_steps):
            if self.stop_requested:
                self.on_event({"kind": "guard", "text": "Arrêté à ta demande."})
                return
            msg = self._ollama_call().get("message", {})
            self.messages.append(msg)
            if msg.get("content", "").strip():
                self.on_event({"kind": "assistant", "text": msg["content"]})
            calls = msg.get("tool_calls") or []
            if not calls:
                return
            stop = False
            for call in calls:
                fn = call.get("function", {})
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                self.on_event({"kind": "tool", "name": fn.get("name"), "input": args})
                text, is_err, _ = self._run_tool(fn.get("name", ""), args)
                self._log(tool=fn.get("name"), step=step, error=is_err, result=text[:300])
                self.on_event({"kind": "tool_result", "name": fn.get("name"), "error": is_err, "text": text[:2000]})
                self.messages.append({"role": "tool", "tool_name": fn.get("name"), "content": text})
                if is_err and self._guard(errors, text):
                    stop = True
            if stop:
                return
        self._log(decision="max_steps")
        self.on_event({"kind": "guard", "text": f"Limite de {self.settings.max_steps} actions atteinte."})
