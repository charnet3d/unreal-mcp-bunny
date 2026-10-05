"""Test suite for the bunny MCP proxy (plain-asyncio; run: python tests/test_proxy.py).

Includes a faithful mock of UE 5.8's ModelContextProtocol plugin behavior:
GetSupportedProtocolVersions() = {2025-11-25, 2025-06-18, 2024-11-05} with
fallback to 2025-11-25 for any unsupported client version (the Junie killer).
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import uuid
from pathlib import Path

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bunny.server import BunnyServer  # noqa: E402

# ---------------------------------------------------------------- fake UE


class FakeUE:
    """Mimics UE 5.8's streamable-HTTP MCP endpoint, version whitelist included."""

    WHITELIST = ("2025-11-25", "2025-06-18", "2024-11-05")

    def __init__(self):
        self.sessions: set[str] = set()
        self.calls: list = []
        self.toolset = [
            {"name": "list_toolsets", "description": "List UE toolsets",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "describe_toolset", "description": "Describe one toolset",
             "inputSchema": {"type": "object", "properties": {"toolset": {"type": "string"}}}},
            {"name": "call_tool", "description": "Call a toolset tool",
             "inputSchema": {"type": "object", "properties": {"toolset": {"type": "string"},
                                                              "tool": {"type": "string"}}}},
        ]

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/mcp":
            return httpx.Response(404)
        sid = request.headers.get("mcp-session-id")
        body = json.loads(request.content.decode() or "{}")
        method = body.get("method")
        req_id = body.get("id")

        if method == "initialize":
            offered = (body.get("params") or {}).get("protocolVersion", "")
            negotiated = offered if offered in self.WHITELIST else "2025-11-25"
            new_sid = str(uuid.uuid4())
            self.sessions.add(new_sid)
            return httpx.Response(
                200,
                headers={"mcp-session-id": new_sid},
                json={"jsonrpc": "2.0", "id": req_id, "result": {
                    "protocolVersion": negotiated,
                    "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                    "serverInfo": {"name": "UnrealEditor ModelContextProtocol", "version": "5.8"},
                }},
            )
        if sid not in self.sessions:
            return httpx.Response(404, json={"jsonrpc": "2.0", "id": req_id,
                                             "error": {"code": -32001, "message": "no session"}})
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": req_id,
                                             "result": {"tools": self.toolset}})
        if method == "resources/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": req_id,
                                             "result": {"resources": [{"uri": "ue://world", "name": "world"}]}})
        if method == "resources/templates/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": req_id,
                                             "result": {"resourceTemplates": []}})
        if method == "prompts/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": req_id,
                                             "result": {"prompts": [{"name": "spawn_actor", "description": "d"}]}})
        if method == "tools/call":
            args = (body.get("params") or {}).get("arguments") or {}
            self.calls.append({
                "name": (body.get("params") or {}).get("name"),
                "arguments": args,
                "protocol_header": request.headers.get("mcp-protocol-version"),
            })
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": json.dumps({"ok": True, "echo": args})}],
                "isError": False,
            }})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": req_id, "result": {}})


class ServerThread(threading.Thread):
    def __init__(self, app, port):
        super().__init__(daemon=True)
        self.config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self.server = uvicorn.Server(self.config)

    def run(self):
        self.server.run()

    def stop(self):
        self.server.should_exit = True


# ---------------------------------------------------------------- harness


class Client:
    """Minimal streamable-HTTP MCP client with a persistent session id."""

    def __init__(self, base: str, protocol: str = "2025-03-26"):
        self.base = base
        self.protocol = protocol
        self.sid: str | None = None
        self.id = 0

    async def rpc(self, method, params, transport, protocol=None):
        self.id += 1
        payload = {"jsonrpc": "2.0", "id": self.id, "method": method}
        if params is not None:
            payload["params"] = params
        headers = {"Accept": "application/json, text/event-stream"}
        if self.sid:
            headers["mcp-session-id"] = self.sid
        headers["mcp-protocol-version"] = protocol or self.protocol
        r = await transport.post(self.base + "/mcp", json=payload, headers=headers)
        if r.status_code == 202:
            return None
        data = r.json()
        if self.sid is None and r.headers.get("mcp-session-id"):
            self.sid = r.headers.get("mcp-session-id")
        return {"status": r.status_code, "body": data}

    async def notify(self, method, params=None, transport=None):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        headers = {"Accept": "application/json, text/event-stream"}
        if self.sid:
            headers["mcp-session-id"] = self.sid
        headers["mcp-protocol-version"] = self.protocol
        return await transport.post(self.base + "/mcp", json=payload, headers=headers)

    async def initialize(self, transport):
        r = await self.rpc("initialize", {
            "protocolVersion": self.protocol,
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1"},
        }, transport)
        await self.notify("notifications/initialized", {}, transport)
        return r


RESULTS = []


def check(name: str, cond: bool, detail: str = ""):
    RESULTS.append((name, cond, detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


def make_cfg(upstream_port: int, tmp_dir: Path) -> dict:
    from bunny.config import default_config

    cfg = default_config()
    cfg.update({
        "port": 0,
        "upstream_url": f"http://127.0.0.1:{upstream_port}/mcp",
        "poll_interval_s": 4,
        "auto_refresh_on_connect": True,
        # host isolation: never adopt the machine's real running editor into the mock config
        "auto_adopt_project": False,
        "_root": str(tmp_dir),
        "_config_path": str(tmp_dir / "config.json"),
    })
    return cfg


async def main():
    tmp_dir = ROOT / "data" / "testrun"
    if tmp_dir.exists():
        import shutil

        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)
    # host isolation: with project_path/editor_binary empty, launch/build are
    # unconfigured; mock roots must never scan this machine's real editor.

    # ---------- 1. reproduce the raw Junie failure against the mock UE ----------
    ue = FakeUE()
    ue_thread = ServerThread(ue_app(ue), 8113)
    ue_thread.start()
    await asyncio.sleep(1.2)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ue_app(ue))) as _c:
        pass  # (transport sanity)

    ue_http = httpx.AsyncClient(base_url="http://127.0.0.1:8113")
    raw = await ue_http.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "junie-kotlin-sdk", "version": "?"}},
    })
    negotiated = raw.json()["result"]["protocolVersion"]
    check("mock UE reproduces Junie killer (2025-03-26 -> 2025-11-25)",
          negotiated == "2025-11-25", f"got {negotiated}")
    await ue_http.aclose()

    # ---------- 2. proxy with NO cached catalog, UE live ----------
    cfg = make_cfg(8113, tmp_dir)
    from bunny.server import build_app

    app0, state0 = build_app(cfg)
    proxy_thread = ServerThread(app0, 8114)
    proxy_thread.start()
    await asyncio.sleep(1.5)

    transport = httpx.AsyncClient(timeout=60)
    junie = Client("http://127.0.0.1:8114", protocol="2025-03-26")
    init = await junie.initialize(transport)
    check("proxy echoes client version 2025-03-26 (Junie fix)",
          init["body"]["result"]["protocolVersion"] == "2025-03-26",
          json.dumps(init["body"]["result"]))

    tl = await junie.rpc("tools/list", {}, transport)
    names = [t["name"] for t in tl["body"]["result"]["tools"]]
    proxy_names = {t["name"] for t in tl["body"]["result"]["tools"] if t["name"].startswith("proxy__")}
    expected_proxy = {"proxy__ue_status", "proxy__ue_launch", "proxy__ue_wait_ready", "proxy__ue_build",
                      "proxy__ue_kill", "proxy__ue_refresh_cache", "proxy__cache_stats",
                      "proxy__ue_configure", "proxy__ue_adopt_project", "proxy__ue_enable_toolset",
                      "proxy__ue_sync_toolsets", "proxy__ue_engines"}
    check("tools/list serves all proxy__ tools", expected_proxy <= proxy_names, str(sorted(proxy_names)))

    # initial run caches from live UE
    await asyncio.sleep(1.0)
    tl = await junie.rpc("tools/list", {}, transport)
    names = [t["name"] for t in tl["body"]["result"]["tools"]]
    check("ue_ tools cached & listed (ue_list_toolsets etc.)",
          {"ue_list_toolsets", "ue_describe_toolset", "ue_call_tool"} <= set(names), str(names))

    # call routed upstream, demangled
    call = await junie.rpc("tools/call", {"name": "ue_call_tool",
                                          "arguments": {"toolset": "EditorToolset", "tool": "x"}}, transport)
    text = call["body"]["result"]["content"][0]["text"]
    check("ue_ call routes to UE (demangled, echoed)",
          "ok" in text and ue.calls and ue.calls[-1]["name"] == "call_tool", str(ue.calls[-1:]))
    check("upstream carries negotiated mcp-protocol-version header",
          ue.calls and ue.calls[-1]["protocol_header"] == "2025-06-18",
          str(ue.calls[-1:]))

    # unprefixed fallback
    call2 = await junie.rpc("tools/call", {"name": "call_tool", "arguments": {"a": 1}}, transport)
    check("unprefixed cached name accepted", call2["body"]["result"]["content"][0]["text"].find("ok") >= 0)

    # resources/prompts from cache
    rl = await junie.rpc("resources/list", {}, transport)
    check("resources cached & served", rl["body"]["result"]["resources"][0]["uri"] == "ue://world")
    pl = await junie.rpc("prompts/list", {}, transport)
    check("prompts cached & served", pl["body"]["result"]["prompts"][0]["name"] == "spawn_actor")

    # proxy__ue_status
    st = await junie.rpc("tools/call", {"name": "proxy__ue_status", "arguments": {}}, transport)
    body = json.loads(st["body"]["result"]["content"][0]["text"])
    check("proxy__ue_status sees UE alive", body["ue_mcp_alive"] is True and body["cached_tools"] == 3,
          json.dumps(body)[:300])
    check("proxy__ue_status passes the window/session block through",
          body.get("window") and "process_session" in body["window"]
          and "windows_possible" in body["window"] and "note" in body["window"],
          json.dumps(body.get("window")))

    # other protocol versions honored
    for v in ("2024-11-05", "2025-06-18", "2025-11-25", "2026-07-28"):
        c = Client("http://127.0.0.1:8114", protocol=v)
        r = await c.initialize(transport)
        check(f"proxy echoes {v}", r["body"]["result"]["protocolVersion"] == v)
    c = Client("http://127.0.0.1:8114", protocol="2099-01-01")
    r = await c.initialize(transport)
    check("unknown client version falls back to 2025-06-18",
          r["body"]["result"]["protocolVersion"] == "2025-06-18")

    # ---------- 3. UE goes down: proxy stays up, tools stay listed, calls actionable ----------
    ue_thread.stop()
    await asyncio.sleep(1.5)
    # force probe refresh
    st = await junie.rpc("tools/call", {"name": "proxy__ue_status", "arguments": {}}, transport)
    body = json.loads(st["body"]["result"]["content"][0]["text"])
    check("proxy__ue_status reports UE down", body["ue_mcp_alive"] is False, json.dumps(body)[:200])

    tl = await junie.rpc("tools/list", {}, transport)
    names = [t["name"] for t in tl["body"]["result"]["tools"]]
    check("tools/list STILL serves cached ue_ tools with UE down",
          {"ue_list_toolsets", "ue_call_tool"} <= set(names), str(names))

    call = await junie.rpc("tools/call", {"name": "ue_call_tool", "arguments": {}}, transport)
    res = call["body"]["result"]
    offline = json.loads(res["content"][0]["text"])
    check("ue_ call offline -> isError + actionable launch hint",
          res.get("isError") is True and "proxy__ue_launch" in res["content"][0]["text"],
          json.dumps(res)[:300])

    # proxy tools keep working with UE down
    st = await junie.rpc("tools/call", {"name": "proxy__cache_stats", "arguments": {}}, transport)
    body = json.loads(st["body"]["result"]["content"][0]["text"])
    check("proxy__cache_stats works offline", body["tools"] == 3)

    # launch not configured -> actionable, no crash
    st = await junie.rpc("tools/call", {"name": "proxy__ue_launch", "arguments": {}}, transport)
    res = st["body"]["result"]
    check("proxy__ue_launch unconfigured -> actionable error",
          res.get("isError") is True and "project_path" in res["content"][0]["text"])

    # build ok -> proxy auto-relaunches UE (auto_launch_after_build default true).
    # Mock the build/launch outcomes: the handler logic is what's under test.
    orig_build, orig_launch = state0.ue.build, state0.ue.launch
    async def fake_build(blocking=True):
        return {"ok": True, "status": "build_ok", "seconds": 1.0, "message": "mock build succeeded"}
    async def fake_launch(wait_ready=True, timeout_s=None):
        return {"ok": True, "status": "ready", "message": "mock editor relaunched", "pid": 4242}
    state0.ue.build, state0.ue.launch = fake_build, fake_launch
    st = await junie.rpc("tools/call", {"name": "proxy__ue_build", "arguments": {}}, transport)
    body = json.loads(st["body"]["result"]["content"][0]["text"])
    check("build ok -> auto-relaunch UE (auto_launch_after_build)",
          body.get("auto_launch", {}).get("ok") is True, json.dumps(body)[:200])
    st = await junie.rpc("tools/call", {"name": "proxy__ue_build",
                                        "arguments": {"auto_launch_after_build": False}}, transport)
    body = json.loads(st["body"]["result"]["content"][0]["text"])
    check("auto_launch_after_build=false per-call override respected",
          "auto_launch" not in body, json.dumps(body)[:200])
    state0.ue.build, state0.ue.launch = orig_build, orig_launch

    # enable_toolset with no active project -> actionable, no crash
    st = await junie.rpc("tools/call", {"name": "proxy__ue_enable_toolset",
                                        "arguments": {"plugin": "LiveCodingToolset"}}, transport)
    body = json.loads(st["body"]["result"]["content"][0]["text"])
    check("proxy__ue_enable_toolset without project -> actionable error",
          body.get("ok") is False and body.get("status") == "not_configured", json.dumps(body)[:200])
    st = await junie.rpc("tools/call", {"name": "proxy__ue_enable_toolset", "arguments": {}}, transport)
    body = json.loads(st["body"]["result"]["content"][0]["text"])
    check("proxy__ue_enable_toolset without plugin name -> actionable error",
          body.get("ok") is False and body.get("status") == "need_plugin_name", json.dumps(body)[:200])

    # initialize works with UE down (harness can (re)connect)
    c2 = Client("http://127.0.0.1:8114", protocol="2025-03-26")
    r = await c2.initialize(transport)
    check("initialize succeeds with UE closed", r["status"] == 200)

    # unknown session id -> 404 jsonrpc error
    headers = {"Accept": "application/json"}
    r = await transport.post("http://127.0.0.1:8114/mcp",
                             json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
                             headers={**headers, "mcp-session-id": "bogus"})
    check("unknown session id -> 404", r.status_code == 404)

    # GET/SSE probe: Junie's Kotlin SDK opens the server-initiated stream FIRST
    # and treats anything but 200/405 as fatal ("Expected status code 200 but
    # was 404" killed the connection). 405 = "no stream offered", JSON-only.
    g = await transport.get("http://127.0.0.1:8114/mcp", headers={"Accept": "text/event-stream"})
    check("GET /mcp -> 405, never 404 (SSE-first clients)", g.status_code == 405, f"got {g.status_code}")
    g2 = await transport.get("http://127.0.0.1:8114/mcp",
                             headers={"Accept": "text/event-stream", "mcp-session-id": "bogus"})
    check("GET /mcp unknown sid -> 405 (not 404)", g2.status_code == 405, f"got {g2.status_code}")
    check("405 advertises Allow", "allow" in {k.lower() for k in g.headers.keys()})

    # health endpoint
    h = await transport.get("http://127.0.0.1:8114/health")
    check("health endpoint live", h.status_code == 200 and h.json()["cached_tools"] == 3)

    # cache persistence: new BunnyServer reads same disk cache
    server2 = BunnyServer(make_cfg(8113, tmp_dir))
    check("cache persists on disk across restarts", len(server2.state.cache.tools) == 3,
          str(server2.state.cache.data.get("captured_at")))

    await transport.aclose()
    proxy_thread.stop()

    # ---------- 4. cold start: no cache, UE offline ----------
    tmp2 = tmp_dir / "cold"
    tmp2.mkdir()
    server3 = BunnyServer(make_cfg(8113, tmp2))  # upstream port dead now
    app_thread = ServerThread(server3.app, 8115)
    app_thread.start()
    await asyncio.sleep(1.2)
    t2 = httpx.AsyncClient(timeout=30)
    c3 = Client("http://127.0.0.1:8115", protocol="2025-03-26")
    r = await c3.initialize(t2)
    check("cold start (no cache, UE dead): initialize ok", r["status"] == 200)
    tl = await c3.rpc("tools/list", {}, t2)
    names = [t["name"] for t in tl["body"]["result"]["tools"]]
    check("cold start: only proxy__ tools listed", all(n.startswith("proxy__") for n in names), str(names))
    await t2.aclose()
    app_thread.stop()

    await asyncio.sleep(0.5)
    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{total} checks passed")
    if passed != total:
        print("FAILED:", [n for n, ok, _ in RESULTS if not ok])
        sys.exit(1)


def ue_app(ue: FakeUE):
    """Raw ASGI adapter in front of FakeUE (no FastAPI: avoids PEP-563 annotation issues)."""

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        path = scope.get("path", "/")
        req = httpx.Request(method=scope["method"],
                            url=f"http://{headers.get('host', 'localhost')}{path}",
                            headers=headers, content=body)
        resp = await ue.handle(req)
        await send({"type": "http.response.start", "status": resp.status_code,
                    "headers": [(k.lower().encode(), v.encode()) for k, v in resp.headers.items()]})
        await send({"type": "http.response.body", "body": resp.content})

    return app


if __name__ == "__main__":
    asyncio.run(main())
