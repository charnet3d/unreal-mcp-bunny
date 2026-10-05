"""Streamable-HTTP client for UE's ModelContextProtocol server, hand-rolled.

Full control over the handshake: we choose which protocol version to offer
UE (UE 5.8 whitelist: 2025-11-25 / 2025-06-18 / 2024-11-05) and we tolerate
UE replying with a single JSON body *or* an SSE stream.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional

import httpx

from .log import get_logger

log = get_logger("upstream")

# UE 5.8's GetSupportedProtocolVersions() whitelist, in preference order for us:
# 2025-06-18 is the newest one strict clients (Junie's Kotlin SDK) also accept.
OFFER_VERSIONS = ("2025-06-18", "2024-11-05", "2025-11-25")

CLIENT_INFO = {"name": "bunny-proxy", "version": "0.1.0"}


class UpstreamError(Exception):
    def __init__(self, message: str, kind: str = "unreachable", detail: str = ""):
        super().__init__(message)
        self.kind = kind
        self.detail = detail


class UpstreamSession:
    """One live streamable-HTTP session against UE's /mcp endpoint."""

    def __init__(self, url: str, timeout_s: float = 900.0):
        self.url = url
        self.timeout_s = timeout_s
        self.session_id: Optional[str] = None
        self.protocol_version: Optional[str] = None
        self.server_info: Optional[dict] = None
        self.capabilities: Optional[dict] = None
        self._notes: list[dict] = []
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=5.0),
            headers={"Accept": "application/json, text/event-stream"},
        )

    async def close(self) -> None:
        try:
            if self.session_id:
                await self._http.delete(self.url, headers={"Mcp-Session-Id": self.session_id})
        except Exception:
            pass
        await self._http.aclose()

    def _headers(self) -> dict:
        h = {"Accept": "application/json, text/event-stream"}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            h["MCP-Protocol-Version"] = self.protocol_version
        return h

    async def _parse(self, resp: httpx.Response) -> list[dict]:
        """Return every JSON-RPC message carried by a POST response (json body or SSE)."""
        ctype = resp.headers.get("content-type", "")
        text = resp.text
        if "text/event-stream" in ctype:
            msgs = []
            for block in text.split("\n\n"):
                data_lines = [ln[5:].strip() for ln in block.splitlines() if ln.startswith("data:")]
                if not data_lines:
                    continue
                payload = "\n".join(data_lines).strip()
                if not payload:
                    continue
                try:
                    msgs.append(json.loads(payload))
                except json.JSONDecodeError:
                    log.warning("upstream SSE data not JSON: %r", payload[:200])
            return msgs
        if not text.strip():
            return []
        try:
            return [json.loads(text)]
        except json.JSONDecodeError as e:
            raise UpstreamError(f"upstream returned non-JSON: {text[:200]}", kind="protocol", detail=str(e))

    async def initialize(self) -> dict:
        last_err = None
        for version in OFFER_VERSIONS:
            payload = {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "initialize",
                "params": {
                    "protocolVersion": version,
                    "capabilities": {"roots": {"listChanged": False}, "sampling": {}, "elicitation": {}},
                    "clientInfo": CLIENT_INFO,
                },
            }
            try:
                resp = await self._http.post(self.url, json=payload, headers=self._headers())
            except httpx.HTTPError as e:
                raise UpstreamError(
                    f"upstream unreachable at {self.url}: {type(e).__name__}: {e}",
                    kind="unreachable",
                    detail=str(e),
                )
            if resp.status_code in (401, 403):
                raise UpstreamError(
                    f"upstream rejected the connection (HTTP {resp.status_code}) — check UE's MCP auth settings",
                    kind="auth",
                    detail=resp.text[:300],
                )
            if resp.status_code >= 400:
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                continue
            msgs = await self._parse(resp)
            result = None
            for m in msgs:
                if isinstance(m, dict) and m.get("id") == payload["id"] and "result" in m:
                    result = m["result"]
                    break
                if isinstance(m, dict) and "result" in m:
                    result = m["result"]
                    break
            if result is None:
                last_err = f"no initialize result in response ({resp.status_code})"
                continue
            negotiated = result.get("protocolVersion")
            if negotiated not in OFFER_VERSIONS:
                last_err = f"upstream negotiated unusable version {negotiated!r}"
                continue
            sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
            self.session_id = sid
            self.protocol_version = negotiated
            self.server_info = result.get("serverInfo")
            self.capabilities = result.get("capabilities")
            # notifications/initialized (spec requires it before other requests)
            await self.notify("notifications/initialized", {})
            log.info("upstream session %s negotiated %s (server=%s)", sid, negotiated,
                     (self.server_info or {}).get("name"))
            return result
        raise UpstreamError(f"initialize handshake failed: {last_err}", kind="handshake", detail=last_err or "")

    async def request(self, method: str, params: dict | None) -> dict:
        """JSON-RPC request -> result dict (raises UpstreamError / RpcError)."""
        payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method}
        if params is not None:
            payload["params"] = params
        try:
            resp = await self._http.post(self.url, json=payload, headers=self._headers())
        except httpx.HTTPError as e:
            raise UpstreamError(f"upstream unreachable at {self.url}: {type(e).__name__}: {e}",
                                kind="unreachable", detail=str(e))
        if resp.status_code == 404:
            raise UpstreamError("upstream session expired (HTTP 404)", kind="expired")
        if resp.status_code >= 400:
            raise UpstreamError(f"upstream HTTP {resp.status_code}: {resp.text[:200]}", kind="http")
        msgs = await self._parse(resp)
        for m in msgs:
            if not isinstance(m, dict):
                continue
            if m.get("id") == payload["id"]:
                if "error" in m:
                    err = m["error"]
                    raise RpcError(err.get("code", -32000), err.get("message", "rpc error"), err.get("data"))
                return m.get("result", {})
            if m.get("method"):  # notification carried on the same stream
                self._notes.append(m)
        raise UpstreamError("upstream closed the stream without answering", kind="stream")

    async def notify(self, method: str, params: dict | None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        try:
            await self._http.post(self.url, json=payload, headers=self._headers())
        except httpx.HTTPError as e:
            log.debug("notification %s not delivered: %s", method, e)

    def drain_notes(self) -> list[dict]:
        notes, self._notes = self._notes, []
        return notes


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data
