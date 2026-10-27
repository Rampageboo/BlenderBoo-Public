"""Blocking one-request-per-connection client for the Blender loopback bridge."""

from __future__ import annotations

import json
import os
import socket
from typing import Any, Mapping
from uuid import uuid4

BRIDGE_HOST = "127.0.0.1"
DEFAULT_BRIDGE_PORT = 9877
MAX_REQUEST_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 4_194_304
_PROCESS_CLIENT_ID = str(uuid4())


class BridgeClientError(RuntimeError):
    """Raised when the local Blender bridge cannot return a trustworthy response."""


def _request_too_large_result(
    tool: str, request_bytes: int
) -> dict[str, Any]:
    return {
        "ok": False,
        "tool": tool,
        "data": {
            "received_bytes_at_least": request_bytes,
            "request_limit_bytes": MAX_REQUEST_BYTES,
            "execution_started": False,
            "automatic_retry_safe": True,
            "recovery": {
                "action": "reduce_request",
                "message": (
                    "Pass an allowed file path instead of inline content, or use a smaller "
                    "filtered page. The rejected call did not reach Blender."
                ),
            },
        },
        "error": {
            "type": "BridgeRequestTooLargeError",
            "message": "Request exceeds the size limit",
            "traceback": "",
        },
        "warnings": [],
    }


class BridgeClient:
    def __init__(
        self,
        token: str,
        port: int = DEFAULT_BRIDGE_PORT,
        connect_timeout: float = 5.0,
        client_id: str | None = None,
    ) -> None:
        if not isinstance(token, str) or not 32 <= len(token) <= 4096:
            raise ValueError("Bridge token must be a string from 32 to 4096 characters")
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError("Bridge port must be an integer from 1024 to 65535")
        if not isinstance(connect_timeout, (int, float)) or not 0.1 <= float(connect_timeout) <= 30:
            raise ValueError("connect_timeout must be from 0.1 to 30 seconds")
        resolved_client_id = _PROCESS_CLIENT_ID if client_id is None else client_id
        if not isinstance(resolved_client_id, str) or not resolved_client_id:
            raise ValueError("client_id must be a non-empty string")
        self._token = token
        self.client_id = resolved_client_id
        self.port = port
        self.connect_timeout = float(connect_timeout)

    @classmethod
    def from_env(cls) -> "BridgeClient":
        token = os.environ.get("CBT_BRIDGE_TOKEN")
        if token is None:
            raise BridgeClientError("CBT_BRIDGE_TOKEN is required; do not store it in the repository")
        raw_port = os.environ.get("CBT_BRIDGE_PORT", str(DEFAULT_BRIDGE_PORT))
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise BridgeClientError("CBT_BRIDGE_PORT must be an integer") from exc
        return cls(token, port, client_id=os.environ.get("CBT_CLIENT_ID"))

    def call(self, tool: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(tool, str) or not tool:
            raise ValueError("tool must be a non-empty string")
        if params is not None and not isinstance(params, Mapping):
            raise ValueError("params must be a mapping or null")
        request = json.dumps(
            {
                "token": self._token,
                "client_id": self.client_id,
                "tool": tool,
                "params": dict(params) if params is not None else None,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        if len(request) > MAX_REQUEST_BYTES:
            return _request_too_large_result(tool, len(request))
        try:
            with socket.create_connection((BRIDGE_HOST, self.port), timeout=self.connect_timeout) as sock:
                sock.sendall(request)
                sock.shutdown(socket.SHUT_WR)
                # Tool execution has no client-side deadline. Returning a timeout after Blender
                # starts a mutating call would make the final scene state ambiguous.
                sock.settimeout(None)
                response = self._read_response(sock)
        except OSError as exc:
            raise BridgeClientError(
                f"Could not communicate with Blender bridge at {BRIDGE_HOST}:{self.port}: {exc}"
            ) from exc
        try:
            decoded = json.loads(response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeClientError("Blender bridge returned invalid UTF-8 JSON") from exc
        required = {"ok", "tool", "data", "error", "warnings"}
        if not isinstance(decoded, dict) or not required <= set(decoded) or set(decoded) - required - {"meta"}:
            raise BridgeClientError("Blender bridge returned an invalid result envelope")
        return decoded

    @staticmethod
    def _read_response(sock: socket.socket) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = sock.recv(min(65_536, MAX_RESPONSE_BYTES + 1 - size))
            if not chunk:
                raise BridgeClientError("Blender bridge closed without a complete response")
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise BridgeClientError("Blender bridge response exceeds the size limit")
            if b"\n" in chunk:
                payload = b"".join(chunks)
                line, remainder = payload.split(b"\n", 1)
                if remainder:
                    raise BridgeClientError("Blender bridge returned unexpected trailing data")
                return line
