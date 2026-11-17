"""Client du pont Blender (côté application)."""
from __future__ import annotations

import json
import socket
from typing import Any


class BridgeError(RuntimeError):
    pass


class BlenderBridge:
    def __init__(self, host: str = "127.0.0.1", port: int = 9876, timeout: float = 600.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    def send(self, kind: str, params: dict | None = None, timeout: float | None = None) -> Any:
        payload = json.dumps({"type": kind, "params": params or {}}).encode("utf-8")
        try:
            with socket.create_connection((self.host, self.port), timeout=3) as sock:
                sock.settimeout(timeout or self.timeout)
                sock.sendall(payload)
                buffer = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buffer += chunk
                    try:
                        response = json.loads(buffer.decode("utf-8"))
                        break
                    except (ValueError, UnicodeDecodeError):
                        continue
                else:  # pragma: no cover
                    response = None
        except OSError as exc:
            raise BridgeError(f"Blender injoignable sur {self.host}:{self.port} ({exc})") from exc
        if not buffer:
            raise BridgeError("Blender a fermé la connexion sans répondre")
        if response.get("status") != "success":
            raise BridgeError(response.get("message", "Erreur inconnue côté Blender"))
        return response.get("result")

    def is_alive(self) -> bool:
        try:
            self.send("ping", timeout=2)
            return True
        except BridgeError:
            return False

    def execute(self, code: str) -> str:
        return self.send("execute_code", {"code": code}).get("result", "")
