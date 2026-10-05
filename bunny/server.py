"""bunny proxy server: FastAPI app speaking Streamable-HTTP MCP to harnesses.

What it fixes / provides:
  * Protocol shim — answers `initialize` with whatever protocolVersion the
    client asked for (Junie's 2025-03-26 included), while speaking a UE-safe
    version (2025-06-18) upstream. UE's 2025-11-25 never reaches strict clients.
  * Always-on — the proxy is its own MCP server. Closing/restarting UE never
    breaks harness connections; cached tool definitions stay visible.
  * UE tools are cached to disk (data/tools_cache.json) on connect / on demand,
    served with `ue_`-prefixed names even while UE is offline.
  * proxy__* tools let a harness launch the editor, wait for MCP readiness,
    build the project, and refresh the cache.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import __version__
from .cache import Cache
from .config import load_config, write_client_config, emit_client_config, AGENTS, SERVER_NAME, scan_engines, _version_key
from .log import get_logger
from .ue import UEBridge
from .upstream import RpcError, UpstreamError, UpstreamSession
from .workspace import (detect_peer_workspace, env_workspace, roots_workspace,
                        workspace_from_path)

log = get_logger("server")

SESSION_HEADER = "mcp-session-id"
PROTOCOL_VERSION_HEADER = "mcp-protocol-version"
WORKSPACE_HEADER = "x-harness-workspace"  # harnesses may pin their workspace per request

# protocol versions we happily echo back to clients
ACCEPTED_CLIENT_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28")

METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

JSONRPC_ERROR_CODES = {
    "invalid_request": -32600,
    "method_not_found": -32601,
    "invalid_params": -32602,
    "internal_error": -32603,
    "parse_error": -32700,
}


def mangle(name: str) -> str:
    return "ue_" + name


def demangle(name: str) -> str:
    return name[3:]


def _tool(name: str, description: str, schema: dict, annotations: dict | None = None) -> dict:
    t = {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": {}, "required": [], **schema},
    }
    if annotations:
        t["annotations"] = annotations
    return t


def proxy_tool_defs(cfg: dict) -> list[dict]:
    return [
        _tool(
            "proxy__ue_status",
            "Report whether Unreal Engine's MCP server is reachable, whether the editor process "
            "is running, and whether this proxy can launch/build the project. Call this first.",
            {},
            {"title": "UE status", "readOnlyHint": True, "openWorldHint": True},
        ),
        _tool(
            "proxy__ue_launch",
            "Start the Unreal Editor for the configured project (with ModelContextProtocol.StartServer) "
            "if it is not already running, and wait until UE's MCP endpoint is reachable. After this "
            "returns ok=true, ue_* tools route to the live engine.",
            {
                "properties": {
                    "wait_ready": {"type": "boolean", "description": "block until MCP reachable (default true)"},
                    "timeout_s": {"type": "number", "description": "max seconds to wait for readiness"},
                }
            },
            {"title": "Launch UE editor", "openWorldHint": True},
        ),
        _tool(
            "proxy__ue_wait_ready",
            "Block until Unreal's MCP endpoint is reachable (e.g. after you started the engine manually "
            "or it is mid-live-coding). Returns ok=true when ue_* tools will route, ok=false on timeout.",
            {"properties": {"timeout_s": {"type": "number", "description": "max seconds to wait (default from config)"}}},
            {"title": "Wait for UE MCP", "readOnlyHint": True, "openWorldHint": True},
        ),
        _tool(
            "proxy__ue_build",
            "Build the project's editor target via Build.bat (blocking). Works with UE closed — "
            "use after editing C++ to recompile before relaunching the engine. On a successful "
            "blocking build with UE closed, the proxy relaunches the editor and waits for its MCP "
            "server automatically (auto_launch_after_build; per-call override supported). For an "
            "in-place recompile with UE OPEN, do NOT use this: use UE's Live Coding toolset via "
            "the ue_* tools instead.",
            {
                "properties": {
                    "blocking": {"type": "boolean", "description": "wait for completion (default true)"},
                    "auto_launch_after_build": {"type": "boolean",
                                                "description": "relaunch UE after a successful build (default from config, true)"},
                }
            },
            {"title": "Build UE project", "openWorldHint": True},
        ),
        _tool(
            "proxy__ue_kill",
            "Terminate the running Unreal Editor (WITHOUT saving). Use to clear a crashed/hung editor "
            "before relaunching. Requires confirmed=true.",
            {"properties": {"confirmed": {"type": "boolean", "description": "must be true to execute"}}},
            {"title": "Kill UE editor", "destructiveHint": True, "openWorldHint": True},
        ),
        _tool(
            "proxy__ue_refresh_cache",
            "Refresh the cached copy of UE's MCP tool/resource/prompt catalog (needs UE running). "
            "Run after enabling more toolset plugins so their tools stay exposed while UE is closed.",
            {},
            {"title": "Refresh UE tool cache", "readOnlyHint": True, "openWorldHint": True},
        ),
        _tool(
            "proxy__cache_stats",
            "Show the proxy's cached UE catalog counts, capture time, proxy version and config summary.",
            {},
            {"title": "Proxy cache stats", "readOnlyHint": True},
        ),
        _tool(
            "proxy__ue_configure",
            "Set project_path / editor_binary / engine_root / build_target in the proxy config "
            "(written to config.json). Call with action='scan' first to list engine installs found on disk.",
            {
                "properties": {
                    "action": {"type": "string", "enum": ["set", "scan"], "description": "scan = list engine installs; set = write values"},
                    "project_path": {"type": "string", "description": "absolute path to the .uproject"},
                    "editor_binary": {"type": "string", "description": "absolute path to the GUI UnrealEditor.exe (UnrealEditor-Cmd.exe is rejected for launches — it is for command/test runs that close the engine after)"},
                    "engine_root": {"type": "string", "description": "engine root folder (contains Engine/) — optional if editor_binary given"},
                    "build_target": {"type": "string", "description": "editor target name, e.g. MyGameEditor (default: derived from project name)"},
                }
            },
            {"title": "Configure proxy", "openWorldHint": True},
        ),
        _tool(
            "proxy__ue_declare_workspace",
            "Declare the workspace folder (or .uproject path) the agent is currently working in. "
            "bunny then launches/builds THAT project when no editor is running — not the last-adopted "
            "one. Call right after switching workspaces/projects. The declaration is per MCP session "
            "and persists while the session lives; `sticky=true` also adopts immediately.",
            {
                "properties": {
                    "workspace": {"type": "string",
                                  "description": "absolute path to the workspace folder (containing the .uproject) or to the .uproject itself"},
                    "sticky": {"type": "boolean",
                               "description": "also adopt now (kills build/launch staleness at once); default false = adopt lazily on next launch/build"},
                }
            },
            {"title": "Declare harness workspace", "openWorldHint": True},
        ),
        _tool(
            "proxy__ue_adopt_project",
            "Detect the .uproject of the currently running Unreal Editor and adopt it (plus its "
            "engine via EngineAssociation and its <Name>Editor build target) as the proxy's "
            "launch/build defaults. Auto-runs when UE comes up; call manually after opening a "
            "different project in the editor. The adopted project persists across proxy restarts.",
            {},
            {"title": "Adopt running UE project", "openWorldHint": True},
        ),
        _tool(
            "proxy__ue_enable_toolset",
            "Enable a toolset plugin (e.g. LiveCodingToolset, EditorToolset, NiagaraToolsets) in "
            "the active project's .uproject so its tools become available over MCP. Works on any "
            "adopted project: locates the plugin in the engine, backs up the .uproject, writes the "
            "Enabled entry. Needs UE closed + proxy__ue_build to compile the plugin module (build "
            "auto-relaunches the editor). LiveCodingToolset ships EnabledByDefault=false, so "
            "harnesses that want in-editor compiles call this once per project.",
            {"properties": {"plugin": {"type": "string",
                                       "description": "plugin name, e.g. LiveCodingToolset"}}},
            {"title": "Enable UE toolset plugin", "openWorldHint": True},
        ),
        _tool(
            "proxy__ue_sync_toolsets",
            "Reconcile the active project's toolset plugin list with the engine. AllToolsets is "
            "the enable-all switch (its manifest is engine-maintained); the project keeps ONLY "
            "gap plugins — toolsets AllToolsets doesn't cover (e.g. LiveCodingToolset). Adds "
            "missing gap plugins, prunes entries that became redundant (AllToolsets now covers "
            "them) or are missing from this engine version (renamed/removed upstream — prevents "
            "'missing plugin' friction after engine updates). Explicit Enabled:false entries are "
            "user opt-outs and are respected. Run with dry_run=true to preview. After real "
            "sync: proxy__ue_build (UE closed) compiles new plugins and auto-relaunches.",
            {"properties": {"dry_run": {"type": "boolean",
                                        "description": "report planned changes without writing (default false)"}}},
            {"title": "Sync UE toolsets with engine", "openWorldHint": True},
        ),
        _tool(
            "proxy__ue_engines",
            "List every installed Unreal Engine the proxy can find (Launcher manifest store, "
            "registry, standard roots, scan_extra_roots) with version, full build version, "
            "install path, discovery source, and which engine is currently active. Use it to "
            "see available installs; choose one with proxy__ue_configure {engine_root, "
            "editor_binary}. When several installs share a version the proxy picks the newest "
            "build deterministically; the running editor's own engine always wins over scans.",
            {},
            {"title": "List installed UE engines", "openWorldHint": False},
        ),
    ]


def cap(session: UpstreamSession | None, name: str) -> bool:
    """MCP capability support is signalled by the KEY being present; its value is
    usually an empty dict (e.g. {"resources": {}}), so truthiness is the wrong test."""
    return bool(session and session.capabilities and name in session.capabilities)


class ProxyState:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.root = Path(cfg["_root"])
        self.cache = Cache(self.root / "data" / "tools_cache.json")
        self.ue = UEBridge(cfg, cfg["upstream_url"])
        self.upstream: Optional[UpstreamSession] = None
        self.upstream_lock = asyncio.Lock()
        self.last_probe: dict = {"alive": False, "status": None, "note": "not probed", "ts": 0}
        self.sessions: dict[str, dict] = {}
        self.last_refresh = 0.0
        self.refresh_inflight = False
        self.started_at = time.time()
        self.adopted_from: str | None = None
        self.load_adopted()

    # ---------------- upstream ----------------
    async def ensure_upstream(self) -> UpstreamSession:
        async with self.upstream_lock:
            if self.upstream is not None:
                return self.upstream
            s = UpstreamSession(self.cfg["upstream_url"], self.cfg.get("forward_timeout_s", 900))
            try:
                await s.initialize()
            except Exception:
                await s.close()
                raise
            self.upstream = s
            return s

    async def drop_upstream(self) -> None:
        async with self.upstream_lock:
            if self.upstream is not None:
                await self.upstream.close()
                self.upstream = None

    async def forward(self, method: str, params: Any) -> Any:
        """Forward one JSON-RPC request upstream, with one transparent session-retry."""
        s = await self.ensure_upstream()
        try:
            return await s.request(method, params)
        except UpstreamError as e:
            if e.kind in ("expired", "unreachable", "stream"):
                log.info("upstream session stale (%s), reconnecting once", e.kind)
                await self.drop_upstream()
                self.last_probe = {"alive": False, "status": None, "note": str(e), "ts": time.time()}
                s = await self.ensure_upstream()
                return await s.request(method, params)
            raise

    # ---------------- catalog ----------------
    async def refresh_catalog(self) -> dict:
        """Pull tools/resources/prompts from UE and store them on disk."""
        if self.refresh_inflight:
            return {"ok": True, "status": "refresh_in_progress"}
        self.refresh_inflight = True
        try:
            probe = await self.probe(tries=4, settle_s=3.0)
            if not probe["alive"]:
                return {"ok": False, "status": "ue_offline",
                        "message": f"UE MCP not reachable ({probe['note']}); cached catalog kept",
                        "cached_at": self.cache.captured_at}
            try:
                tools = (await self.forward("tools/list", {})).get("tools", [])
            except RpcError as e:
                return {"ok": False, "status": "upstream_error", "message": f"tools/list failed upstream: {e}"}
            resources, templates, prompts = [], [], []
            if cap(self.upstream, "resources"):
                try:
                    r = await self.forward("resources/list", {})
                    resources = r.get("resources", [])
                    templates = (await self.forward("resources/templates/list", {})).get("resourceTemplates", [])
                except (RpcError, UpstreamError) as e:
                    log.debug("resources snapshot skipped: %s", e)
            if cap(self.upstream, "prompts"):
                try:
                    prompts = (await self.forward("prompts/list", {})).get("prompts", [])
                except (RpcError, UpstreamError) as e:
                    log.debug("prompts snapshot skipped: %s", e)
            self.cache.update(
                {"tools": tools, "resources": resources, "prompts": prompts, "resourceTemplates": templates},
                self.cfg["upstream_url"], (self.upstream.server_info if self.upstream else {}) or {},
            )
            self.last_refresh = time.time()
            log.info("catalog cached: %d tools, %d resources, %d prompts",
                     len(tools), len(resources), len(prompts))
            return {"ok": True, "status": "refreshed", "tools": len(tools),
                    "resources": len(resources), "prompts": len(prompts)}
        finally:
            self.refresh_inflight = False

    # ---------------- probes ----------------
    async def probe(self, max_age: float = 5.0, tries: int = 1, settle_s: float = 2.0) -> dict:
        if time.time() - self.last_probe.get("ts", 0) < max_age:
            return self.last_probe
        p = await self.ue.upstream_probe()
        # A freshly launched editor accepts TCP on :8000 before its game thread
        # is idle (shader compilation etc.) -> ReadTimeout. Retry before giving up.
        for _ in range(max(0, tries - 1)):
            if p["alive"]:
                break
            await asyncio.sleep(settle_s)
            p = await self.ue.upstream_probe()
        p["ts"] = time.time()
        self.last_probe = p
        if not p["alive"] and self.upstream is not None:
            await self.drop_upstream()
        return p

    # ---------------- project adoption ----------------
    def load_adopted(self) -> None:
        """Apply last adopted project (survives proxy restarts; config.json untouched)."""
        p = self.root / "data" / "adopted.json"
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for key in ("project_path", "engine_root", "editor_binary", "build_target"):
            if data.get(key):
                self.cfg[key] = data[key]
        self.ue.cfg = self.cfg
        self.adopted_from = data.get("adopted_from")

    async def adopt_running_project(self) -> dict:
        """Detect the running editor's .uproject and adopt it (project+engine+target).

        This is what makes the proxy follow any project opened in the harness:
        MCP routing is already project-agnostic (UE owns port 8000), and adoption
        makes launch/build defaults follow the same project UE is showing now.
        """
        if sys.platform == "win32":
            info = await asyncio.to_thread(self.ue.running_project)
        else:
            info = self.ue.running_project()
        if not info:
            return {"ok": False, "status": "no_running_project",
                    "message": "no running editor with a .uproject on its command line was detected"}
        return await self.adopt_project(info["uproject"], exe=info.get("exe"),
                                        source="running_editor")

    def discover_targets_for(self, uproject: str) -> list[dict]:
        from .config import discover_targets
        return discover_targets(uproject)

    def pick_target_for(self, uproject: str) -> tuple[str, str]:
        from .config import pick_target
        return pick_target(self.cfg, uproject)

    async def adopt_project(self, uproject: str, exe: str | None = None,
                            source: str = "manual", require_engine: bool = False) -> dict:
        """Adopt one .uproject: resolve its engine, set launch/build defaults, persist."""
        from .config import (resolve_engine_for_project, derive_target,
                             engine_from_editor_exe, scan_engines)

        new = {"project_path": uproject}
        eng = None
        if exe:
            # best signal, zero guessing: the engine of the editor that is
            # literally open right now (ExecutablePath -> root)
            eng = engine_from_editor_exe(exe)
        if eng is None:
            eng = resolve_engine_for_project(uproject,
                                             scan_engines(self.cfg.get("scan_extra_roots")))
        if eng:
            new["engine_root"] = eng["engine_root"]
            new["editor_binary"] = eng["editor_binary"]
        elif require_engine:
            # a project whose engine we cannot resolve must not inherit some
            # other project's editor_binary — refuse and tell the agent why
            return {"ok": False, "status": "engine_unresolved",
                    "message": f"Engine for {uproject} was not found via registry/launcher-manifest "
                               "scans (source-build GUID association, or engine installed in a custom "
                               "folder). Launch it once with its engine (Launcher / double-click the "
                               ".uproject) — the proxy adopts from the running editor automatically — "
                               "or set engine_root/editor_binary via proxy__ue_configure."}
        elif not self.cfg.get("editor_binary"):
            return {"ok": False, "status": "engine_unresolved",
                    "message": f"Engine for {uproject} was not found via registry/launcher-manifest "
                               "scans (source-build GUID association, or engine installed in a custom "
                               "folder). Do this once: launch the project with its engine (Epic Launcher, "
                               "double-click the .uproject, or VS) — the proxy adopts the engine from the "
                               "running editor automatically. Alternatively set editor_binary explicitly "
                               "via proxy__ue_configure."}
        name = Path(uproject).stem
        from .config import target_for_project
        new["build_target"] = target_for_project({**self.cfg, "project_path": uproject}, uproject) \
            or f"{name}Editor"
        for k, v in new.items():
            self.cfg[k] = v
        self.ue.cfg = self.cfg
        self.adopted_from = uproject
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "data").mkdir(exist_ok=True)
        payload = {**new, "adopted_from": uproject, "adopted_source": source, "adopted_at": time.time()}
        (self.root / "data" / "adopted.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        log.info("adopted project (%s): %s (engine=%s target=%s)", source, uproject,
                 self.cfg.get("engine_root"), self.cfg.get("build_target"))
        return {"ok": True, "status": "adopted", "adopted_source": source, **new}

    async def resolve_workspace(self, sess: dict | None) -> dict | None:
        """The harness workspace in priority order — generic, per session.

        1. roots     — harness advertised the roots capability: fetch roots/list
                       from that client (MCP spec reverse request).
        2. declared  — harness pinned it: `x-harness-workspace` header on its
                       POSTs, or proxy__ue_declare_workspace for this session.
        3. cwd       — OS probe of the session's TCP peer process (and its
                       ancestors): the first one whose cwd contains a .uproject.
        4. env       — BUNNY_WORKSPACE_PROJECT operator pin.

        Returns {project_dir, uproject, source, ...} or None. Never raises.
        """
        if not self.cfg.get("workspace_adopt", True):
            return None
        if sess is not None:
            if sess.get("roots_capable"):
                ws = await self.fetch_roots(sess)
                if ws:
                    return ws
            declared = sess.get("declared")
            if declared:
                ws = workspace_from_path(declared)
                if ws:
                    ws["source"] = "declared"
                    return ws
            peer = sess.get("peer")
            if peer:
                ws = await asyncio.to_thread(detect_peer_workspace, peer,
                                             int(self.cfg.get("port") or 0))
                if ws:
                    log.debug("workspace resolved via peer probe: %s (pid=%s cwd=%s)",
                              ws.get("project_dir"), ws.get("pid"), ws.get("cwd"))
                    return ws
        return env_workspace(self.cfg)

    async def fetch_roots(self, sess: dict) -> dict | None:
        """Reverse MCP request: ask the harness for its roots (spec 2025-06-18).

        Only for sessions that advertised the roots capability. Times out fast
        and caches the answer; a harness that never replies is remembered so we
        don't stall its calls again.
        """
        now = time.time()
        ws = sess.get("roots")
        if ws and sess.get("roots_ttl", 0) > now:
            return ws  # fresh cached answer
        if sess.get("roots_unsupported"):
            return None
        q = sess.setdefault("roots_q", asyncio.Queue(maxsize=8))
        sess["roots_id"] = sess.get("roots_id", 0) + 1
        rid = sess["roots_id"]
        sess["pending_roots"] = rid
        try:
            await self.deliver(sess, {"jsonrpc": "2.0", "id": rid, "method": "roots/list"})
        except Exception as e:  # pragma: no cover
            log.debug("roots/list delivery failed: %s", e)
            sess["roots_unsupported"] = True
            return None
        try:
            resp = await asyncio.wait_for(q.get(), timeout=6.0)
        except asyncio.TimeoutError:
            sess["roots_unsupported"] = True  # this harness ignores reverse requests
            log.debug("roots/list unanswered by %s; won't ask again",
                      (sess.get("client_info") or {}).get("name", "client"))
            return None
        sess["pending_roots"] = None
        if resp.get("error"):
            sess["roots_unsupported"] = True
            return None
        ws = roots_workspace(resp.get("result"))
        if ws:
            ws["source"] = "roots"
            sess["roots"] = ws
            sess["roots_ttl"] = now + 300.0
        return ws

    async def deliver(self, sess: dict, payload: dict) -> None:
        """Push a server-initiated JSON-RPC message to a client session.

        Streamable-HTTP clients (no GET stream) receive it as a JSON-RPC
        notification on their next POST (application/json); SSE-stream sessions
        receive it on their open stream.
        """
        body = json.dumps(payload, ensure_ascii=False)
        sess["outbox"].append(body)
        q = sess.get("sse_queue")
        if q is not None:
            try:
                q.put_nowait(body)
            except Exception:
                pass

    async def ensure_launch_target(self, sess: dict | None = None,
                                   prefer_workspace: bool = False) -> dict | None:
        """Point launch/build at the project the agent is working on NOW.

        Priority: (1) a running editor wins — its project is adopted (UE owns
        the MCP port, everything routes to it); (2) no editor running: the live
        harness workspace's .uproject is adopted, so a fresh agent in a
        switched workspace launches ITS project, not the last-adopted one;
        (3) neither signal: keep current defaults.

        prefer_workspace (build path): the agent asked to compile ITS project.
        A running editor of a DIFFERENT project must not hijack that — Build.bat
        for the workspace project is safe while an unrelated editor runs (it is
        the same-project editor that locks binaries).
        """
        if not self.cfg.get("auto_adopt_project") or not self.cfg.get("workspace_adopt", True):
            return None
        ws = await self.resolve_workspace(sess)
        if sys.platform == "win32":
            info = await asyncio.to_thread(self.ue.running_project)
        else:
            info = self.ue.running_project()
        if info:
            cur = os.path.normpath(str(self.cfg.get("project_path") or ""))
            run = os.path.normpath(info["uproject"])
            same = run.lower() == cur.lower()
            if prefer_workspace and ws:
                want = os.path.normpath(ws["uproject"])
                if want.lower() != run.lower():
                    log.info("build follows harness workspace (%s), not running editor (%s)",
                             want, run)
                    return await self.adopt_project(want, exe=None,
                                                    source=str(ws.get("source", "workspace")),
                                                    require_engine=True)
            if same:
                return None  # already following the running editor
            return await self.adopt_running_project()
        if not ws:
            return None
        cur = os.path.normpath(str(self.cfg.get("project_path") or "")).lower()
        up = os.path.normpath(ws["uproject"]).lower()
        if cur == up:
            return None  # current adoption already matches the workspace
        log.info("harness workspace detected (%s): %s -> %s",
                 ws.get("source"), ws.get("project_dir"), ws["uproject"])
        res = await self.adopt_project(ws["uproject"], exe=None,
                                       source=str(ws.get("source", "workspace")),
                                       require_engine=True)
        if not res.get("ok"):
            log.warning("workspace adoption refused (%s): %s", ws["uproject"],
                        str(res.get("message"))[:160])
        return res

    # ---------------- views ----------------
    def cached_tools_view(self) -> list[dict]:
        """UE tool defs from cache, ue_-prefixed, with a routing note appended."""
        out = []
        for t in self.cache.tools:
            t = dict(t)
            t["name"] = mangle(t.get("name", ""))
            d = t.get("description") or ""
            t["description"] = (
                d
                + "\n\n[proxied Unreal Engine tool — routes to the live editor when it is running; "
                  "returns an actionable error (with a proxy__ue_launch hint) when UE is closed]"
            )
            out.append(t)
        return out

    def tools_view(self) -> list[dict]:
        return proxy_tool_defs(self.cfg) + self.cached_tools_view()


# --------------------------------------------------------------------------
# JSON-RPC plumbing
# --------------------------------------------------------------------------

def rpc_error(req_id, code: int, message: str, data: Any = None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def rpc_result(req_id, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def text_response(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def json_text(obj: Any, is_error: bool = False) -> dict:
    return text_response(json.dumps(obj, indent=2, ensure_ascii=False), is_error)


class BunnyServer:
    def __init__(self, cfg: dict):
        self.state = ProxyState(cfg)
        self.poller_task: asyncio.Task | None = None

        @asynccontextmanager
        async def _lifespan(_app: FastAPI):
            st = self.state
            self.poller_task = asyncio.create_task(poller(st))
            probe = await st.probe(max_age=0)
            if probe["alive"]:
                if st.cfg.get("auto_adopt_project"):
                    await st.adopt_running_project()
                await st.refresh_catalog()
            elif not st.cache.tools:
                log.warning(
                    "UE not reachable and no cached catalog yet. Start the UE editor once "
                    "(ModelContextProtocol.StartServer) and run `python -m bunny.server --refresh`, "
                    "or call proxy__ue_refresh_cache once UE is up — tools cache automatically after that."
                )
            yield
            if self.poller_task is not None:
                self.poller_task.cancel()
            await st.drop_upstream()

        self.app = FastAPI(title="bunny", version=__version__, lifespan=_lifespan)
        self._wire_routes()

    # ---------------- routes ----------------
    def _wire_routes(self) -> None:
        app = self.app

        @app.get("/mcp")
        async def get_stream(request: Request):
            sid = request.headers.get(SESSION_HEADER)
            sess = self.state.sessions.get(sid) if sid else None
            if sess is not None and sess.get("roots_capable"):
                # This client advertised server-initiated messaging (roots):
                # open the spec-compliant per-session SSE stream and flush any
                # queued server requests (roots/list). Clients that did NOT
                # advertise it keep getting 405 — the Kotlin SDK treats 405 as
                # "stream disabled, JSON-only" but dies on 404.
                q = sess.setdefault("sse_queue", asyncio.Queue(maxsize=64))
                for item in list(sess.get("outbox") or []):
                    try:
                        q.put_nowait(item)
                    except Exception:
                        break
                sess["outbox"] = []

                async def gen():
                    while True:
                        if await request.is_disconnected():
                            break
                        try:
                            item = await asyncio.wait_for(q.get(), timeout=15.0)
                            yield f"data: {item}\n\n"
                        except asyncio.TimeoutError:
                            yield ": keepalive\n\n"

                log.info("GET /mcp sid=%s -> 200 SSE stream (client advertised roots)", sid)
                return StreamingResponse(gen(), media_type="text/event-stream",
                                         headers={"mcp-session-id": sid,
                                                  "Cache-Control": "no-cache"})
            log.info("GET /mcp sid=%s -> 405 (server-initiated stream not offered)", sid or "-")
            return Response(status_code=405, headers={"Allow": "GET, POST, DELETE"},
                            content="server-initiated SSE stream not offered; POST works")

        @app.delete("/mcp")
        async def terminate(request: Request):
            sid = request.headers.get(SESSION_HEADER)
            if not sid or sid not in self.state.sessions:
                log.info("DELETE /mcp sid=%s -> 404 (no such session)", sid or "-")
                return Response(status_code=404, content="no such session")
            self.state.sessions.pop(sid, None)
            log.info("DELETE /mcp sid=%s -> 200 (session terminated)", sid)
            return Response(status_code=200)

        @app.post("/mcp")
        async def post(request: Request):
            sid = request.headers.get(SESSION_HEADER)
            if sid is not None and sid not in self.state.sessions:
                # spec: after a session ends, re-init is required -> 404 is correct,
                # but make it visible in the log (strict SDKs die right here)
                log.info("POST sid=%s -> 404 unknown session (client must re-initialize)", sid)
                return JSONResponse(rpc_error(None, JSONRPC_ERROR_CODES["invalid_request"],
                                              "unknown or terminated session id"), status_code=404)
            body = await request.body()
            if not body.strip():
                return Response(status_code=202)
            try:
                parsed = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return JSONResponse(rpc_error(None, JSONRPC_ERROR_CODES["parse_error"],
                                              "body must be UTF-8 JSON"), status_code=400)
            messages = parsed if isinstance(parsed, list) else [parsed]

            # workspace signals ride on the session: TCP peer (for the OS probe)
            # and an explicit header declaration from harnesses that support it
            if sid and sid in self.state.sessions:
                sess = self.state.sessions[sid]
                if request.client is not None:
                    sess["peer"] = (request.client.host, request.client.port)
                hdr = request.headers.get(WORKSPACE_HEADER)
                if hdr:
                    sess["declared"] = hdr

            # trace: what clients actually send (used to debug strict-SDK handshakes)
            for m in messages:
                if isinstance(m, dict):
                    log.debug("POST sid=%s method=%s id=%s accept=%s",
                              sid or "-", m.get("method", f"<response id={m.get('id')}>"),
                              m.get("id", "-"),
                              request.headers.get("accept", "-"))

            responses = []
            for msg in messages:
                if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
                    responses.append(rpc_error(msg.get("id") if isinstance(msg, dict) else None,
                                               JSONRPC_ERROR_CODES["invalid_request"],
                                               "invalid JSON-RPC message"))
                    continue
                if "method" in msg:
                    if "id" not in msg:  # notification
                        await self._handle(msg, sid, respond=False)
                    else:
                        r = await self._handle(msg, sid, respond=True)
                        if r is not None:
                            responses.append(r)
                elif "id" in msg:
                    # a client's answer to a server-initiated request (roots/list)
                    sess = self.state.sessions.get(sid) if sid else None
                    pend = (sess or {}).get("pending_roots")
                    if sess is not None and pend is not None and msg.get("id") == pend:
                        sess["pending_roots"] = None
                        try:
                            sess.setdefault("roots_q", asyncio.Queue(maxsize=8)).put_nowait(msg)
                        except Exception:
                            pass
                    else:
                        log.debug("ignoring client response id=%s", msg.get("id"))
                else:
                    responses.append(rpc_error(None, JSONRPC_ERROR_CODES["invalid_request"],
                                               "JSON-RPC message needs method or id"))
            if not responses:
                return Response(status_code=202)
            # JSON-only roots-capable sessions get server-initiated requests
            # piggybacked on the response array (spec-permitted batch form);
            # SSE-stream sessions already got them on their GET stream.
            sess0 = self.state.sessions.get(sid) if sid else None
            if sess0 is not None and sess0.get("outbox"):
                extra = []
                for b in sess0["outbox"]:
                    try:
                        extra.append(json.loads(b))
                    except json.JSONDecodeError:
                        pass
                sess0["outbox"] = []
                responses = extra + responses
            payload = responses[0] if len(responses) == 1 else responses
            resp = JSONResponse(payload)
            # initialize minted a session id: advertise it as the spec requires
            # (mcp-session-id header) and strip the internal field from the result
            minted = None
            items = payload if isinstance(payload, list) else [payload]
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("result"), dict) \
                        and "_minted_session_id" in item["result"]:
                    minted = item["result"].pop("_minted_session_id")
            if minted:
                resp.headers[SESSION_HEADER] = minted
            # Streamable-HTTP: the server MAY mint a session id; when it does,
            # the client MUST echo it on every following request. Advertise it
            # on every response that carries one so strict SDK clients latch on.
            if sid and sid in self.state.sessions:
                resp.headers[SESSION_HEADER] = sid
            return resp

        @app.get("/health")
        async def health():
            probe = await self.state.probe()
            return {
                "proxy": "bunny",
                "version": __version__,
                "upstream_url": self.state.cfg["upstream_url"],
                "ue_mcp_alive": probe["alive"],
                "ue_note": probe["note"],
                "cached_tools": len(self.state.cache.tools),
                "cached_at": self.state.cache.captured_at,
                "sessions": len(self.state.sessions),
                "uptime_s": round(time.time() - self.state.started_at),
            }

    # ---------------- dispatch ----------------
    async def _handle(self, msg: dict, sid: str | None, respond: bool):
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}
        sess = self.state.sessions.get(sid) if sid else None

        try:
            result = await self._dispatch(method, params, sess)
        except RpcError as e:
            result = ("error", e.code, e.message, e.data)
        except UpstreamError as e:
            result = ("error", INTERNAL_ERROR, str(e), {"kind": e.kind})
        except Exception as e:  # pragma: no cover - safety net
            log.exception("unhandled error on %s", method)
            result = ("error", INTERNAL_ERROR, f"bunny internal error: {e}", None)

        if isinstance(result, tuple) and result and result[0] == "error":
            _, code, message, data = result
            return rpc_error(req_id, code, message, data)
        if respond:
            return rpc_result(req_id, result)
        return None

    async def _dispatch(self, method: str, params: dict, sess: dict | None):
        st = self.state

        if method == "initialize":
            requested = str(params.get("protocolVersion") or "")
            version = requested if requested in ACCEPTED_CLIENT_VERSIONS else "2025-06-18"
            if requested and requested not in ACCEPTED_CLIENT_VERSIONS:
                log.info("client asked for unknown protocol %r; negotiating 2025-06-18", requested)
            sid = str(uuid.uuid4())
            caps = params.get("capabilities") or {}
            st.sessions[sid] = {"created": time.time(), "protocol": version,
                                "client_info": params.get("clientInfo") or {},
                                "roots_capable": bool(caps.get("roots")),
                                "outbox": [], "peer": None, "declared": None}
            log.info("initialize client=%s caps=%s params_keys=%s",
                     json.dumps(params.get("clientInfo") or {}, ensure_ascii=False),
                     json.dumps(params.get("capabilities") or {}, ensure_ascii=False)[:300],
                     sorted(params.keys()))
            return {
                "protocolVersion": version,
                "_minted_session_id": sid,  # route layer pops this, sets mcp-session-id header
                "capabilities": {
                    "tools": {"listChanged": True},
                    "resources": {"listChanged": False, "subscribe": False},
                    "prompts": {"listChanged": False},
                    "logging": {},
                },
                "serverInfo": {"name": "bunny-ue-mcp-proxy", "version": __version__,
                               "title": "bunny — always-on proxy for Unreal Engine MCP"},
                "instructions": (
                    "UE tools are cached and always listed (names prefixed ue_). "
                    "UE offline -> ue_* calls return an actionable error; call proxy__ue_status, "
                    "then proxy__ue_launch, then retry. proxy__ue_build compiles the project with UE closed."
                ),
            }

        if method in ("notifications/initialized", "notifications/cancelled",
                      "notifications/roots/list_changed"):
            return None
        if method == "ping":
            return {}

        if method == "tools/list":
            return {"tools": st.tools_view()}

        if method == "resources/list":
            return {"resources": st.cache.data.get("resources", [])}
        if method == "resources/templates/list":
            return {"resourceTemplates": st.cache.data.get("resource_templates", [])}
        if method == "prompts/list":
            return {"prompts": st.cache.data.get("prompts", [])}

        if method == "tools/call":
            name = params.get("name", "")
            arguments = params.get("arguments") or {}
            if name.startswith("proxy__"):
                return await self._proxy_tool(name, arguments, sess)
            if name.startswith("ue_"):
                return await self._call_ue(name, arguments)
            # unprefixed name: harness may have stripped the prefix
            if name in st.cache.tool_names():
                return await self._call_ue(mangle(name), arguments)
            return ("error", METHOD_NOT_FOUND, f"unknown tool {name!r}",
                    "call tools/list; UE tools are prefixed ue_")

        # resources/read, prompts/get, completion/complete, logging, etc.
        return await self._forward_method(method, params, offline_hint=method)

    async def _forward_method(self, method: str, params: Any, offline_hint: str = ""):
        probe = await self.state.probe()
        if not probe["alive"]:
            return json_text({
                "ue_online": False,
                "message": f"'{offline_hint}' needs the running Unreal Editor MCP server. "
                           "Call proxy__ue_launch (starts the editor and waits for MCP), "
                           "or proxy__ue_wait_ready if you are launching it yourself.",
                "upstream_url": self.state.cfg["upstream_url"],
                "probe_note": probe["note"],
            }, is_error=True)
        try:
            return await self.state.forward(method, params)
        except RpcError as e:
            return ("error", e.code, f"upstream rpc error: {e.message}", e.data)
        except UpstreamError as e:
            await self.state.probe(max_age=0)
            return json_text({"ue_online": False, "message": str(e), "kind": e.kind}, is_error=True)

    async def _call_ue(self, name: str, arguments: dict) -> dict:
        st = self.state
        probe = await st.probe()
        if not probe["alive"]:
            return json_text({
                "ue_online": False,
                "tool": demangle(name),
                "message": "Unreal Engine's MCP server is not reachable — the editor is closed or its MCP "
                           "server is not started. The tool is cached (this proxy listed it) but live "
                           "execution needs UE. Call proxy__ue_launch to start the editor, then retry "
                           "this call; use proxy__ue_build first if you just edited C++.",
                "upstream_url": st.cfg["upstream_url"],
                "probe_note": probe["note"],
            }, is_error=True)
        try:
            res = await st.forward("tools/call", {"name": demangle(name), "arguments": arguments})
            await self._maybe_refresh_catalog()
            return res
        except RpcError as e:
            return json_text({"tool": demangle(name), "ue_error": e.message, "data": e.data}, is_error=True)
        except UpstreamError as e:
            await st.probe(max_age=0)
            return json_text({
                "ue_online": False,
                "tool": demangle(name),
                "message": f"UE's MCP server dropped the call ({e.kind}): {e}. "
                           "The editor may have been closed mid-call; proxy__ue_status, then proxy__ue_launch.",
            }, is_error=True)

    async def _maybe_refresh_catalog(self) -> None:
        st = self.state
        if time.time() - st.last_refresh < 60:
            return
        probe = await st.probe()
        if probe["alive"]:
            await st.refresh_catalog()

    # ---------------- proxy__ tools ----------------
    async def _proxy_tool(self, name: str, args: dict, sess: dict | None = None):
        st = self.state
        if name == "proxy__ue_declare_workspace":
            ws_arg = str(args.get("workspace") or "").strip()
            if not ws_arg:
                return json_text({"ok": False, "status": "missing_workspace",
                                  "message": "workspace: absolute folder (containing a .uproject) "
                                             "or .uproject path required"}, is_error=True)
            ws = workspace_from_path(ws_arg)
            if not ws:
                return json_text({"ok": False, "status": "no_uproject",
                                  "message": f"no .uproject found in {ws_arg}; pass the workspace "
                                             "folder that contains the project"}, is_error=True)
            if sess is not None:
                sess["declared"] = ws_arg
                sess["roots_unsupported"] = True  # explicit wins; stop polling roots
            res = {"ok": True, "status": "declared", "workspace": ws["project_dir"],
                   "uproject": ws["uproject"], "source": "declared"}
            if args.get("sticky"):
                res["adoption"] = await st.ensure_launch_target(sess)
            return json_text(res)
        if name == "proxy__ue_status":
            probe = await st.probe(max_age=0)
            ue = await st.ue.status()
            return json_text({
                "ue_mcp_alive": probe["alive"],
                "upstream_url": st.cfg["upstream_url"],
                "probe_note": probe["note"],
                "editor_process_running": ue["editor_process_running"],
                "editor_pids": ue["editor_pids"],
                "launched_by_proxy": ue["launched_by_proxy"],
                "can_launch": ue["can_launch"],
                "can_build": ue["can_build"],
                "window": ue.get("window"),
                "active_project": st.cfg["project_path"],
                "active_engine": st.cfg.get("engine_root"),
                "active_build_target": st.cfg.get("build_target"),
                "build_target_source": (st.cfg.get("build_target") and "persisted")
                                        or (st.cfg.get("project_path")
                                            and st.pick_target_for(st.cfg["project_path"])[1]) or "",
                "available_targets": st.discover_targets_for(st.cfg.get("project_path") or ""),
                "adopted_from": st.adopted_from,
                "harness_workspace": await st.resolve_workspace(sess),
                "build_state": st.ue.build_state(),
                "cached_tools": len(st.cache.tools),
                "cached_at": st.cache.captured_at,
                "proxy_uptime_s": round(time.time() - st.started_at),
            })
        if name == "proxy__ue_launch":
            ws = await st.ensure_launch_target(sess)  # follow harness workspace / running editor
            if st.cfg.get("auto_adopt_project"):
                await st.adopt_running_project()  # editor running with MCP off? follow it
            wait = bool(args.get("wait_ready", True))
            timeout = args.get("timeout_s")
            res = await st.ue.launch(wait_ready=wait, timeout_s=timeout)
            if ws:
                res["workspace_adoption"] = ws
            if res.get("ok") and res.get("status") in ("ready", "already_running"):
                if st.cfg.get("auto_adopt_project"):
                    res["adopted"] = await st.adopt_running_project()
                res["catalog"] = await st.refresh_catalog()
            return json_text(res, is_error=not res.get("ok"))
        if name == "proxy__ue_wait_ready":
            res = await st.ue.wait_ready(args.get("timeout_s"))
            if res.get("ok"):
                res["catalog"] = await st.refresh_catalog()
            return json_text(res, is_error=not res.get("ok"))
        if name == "proxy__ue_build":
            ws = await st.ensure_launch_target(sess, prefer_workspace=True)  # compile the agent's project
            blocking = bool(args.get("blocking", True))
            auto = args.get("auto_launch_after_build")
            if auto is None:
                auto = bool(st.cfg.get("auto_launch_after_build", True))
            res = await st.ue.build(blocking=blocking)
            if ws:
                res["workspace_adoption"] = ws
            if (res.get("ok") and res.get("status") == "build_ok" and auto):
                # full Build.bat compile: UE was closed for it — bring it back
                # so ue_* tools route again without a human in the loop.
                launch = await st.ue.launch(wait_ready=True,
                                            timeout_s=st.cfg.get("launch_ready_timeout_s", 240))
                res["auto_launch"] = launch
                if launch.get("ok"):
                    if st.cfg.get("auto_adopt_project"):
                        launch["adopted"] = await st.adopt_running_project()
                    res["catalog"] = await st.refresh_catalog()
            return json_text(res, is_error=not res.get("ok"))
        if name == "proxy__ue_kill":
            res = await st.ue.kill(confirm=bool(args.get("confirmed")))
            return json_text(res, is_error=not res.get("ok"))
        if name == "proxy__ue_refresh_cache":
            res = await st.refresh_catalog()
            return json_text(res, is_error=not res.get("ok"))
        if name == "proxy__ue_adopt_project":
            res = await st.adopt_running_project()
            return json_text(res, is_error=not res.get("ok"))
        if name == "proxy__ue_enable_toolset":
            plugin = str(args.get("plugin") or "").strip()
            if not plugin:
                return json_text({"ok": False, "status": "need_plugin_name",
                                  "message": "pass plugin, e.g. 'LiveCodingToolset'"}, is_error=True)
            res = st.ue.enable_plugin(plugin)
            return json_text(res, is_error=not res.get("ok"))
        if name == "proxy__ue_sync_toolsets":
            res = st.ue.sync_toolsets(dry_run=bool(args.get("dry_run", False)))
            return json_text(res, is_error=not res.get("ok"))
        if name == "proxy__cache_stats":
            return json_text({
                "proxy_version": __version__,
                "cache_path": str(st.cache.path),
                "cached_at": st.cache.captured_at,
                "tools": len(st.cache.tools),
                "resources": len(st.cache.data.get("resources", [])),
                "prompts": len(st.cache.data.get("prompts", [])),
                "upstream_server_info": st.cache.data.get("upstream_server_info"),
                "config": {
                    "port": st.cfg["port"],
                    "upstream_url": st.cfg["upstream_url"],
                    "project_path": st.cfg["project_path"],
                    "editor_binary": st.cfg["editor_binary"],
                    "engine_root": st.cfg.get("engine_root"),
                    "build_target": st.cfg.get("build_target"),
                    "poll_interval_s": st.cfg["poll_interval_s"],
                },
            })
        if name == "proxy__ue_engines":
            engines = scan_engines(st.cfg.get("scan_extra_roots"))
            active = os.path.normpath(str(st.cfg.get("engine_root") or "")).lower()
            by_version: dict[str, list[dict]] = {}
            for e in engines:
                v = e.get("version") or "?"
                by_version.setdefault(v, []).append(e)
            for v, group in by_version.items():
                if len(group) > 1:
                    group.sort(key=lambda e: _version_key(
                        e.get("build_version_full") or e.get("version")), reverse=True)
                e = group[0]
                e["selected_for_version"] = os.path.normpath(
                    e["engine_root"]).lower() == active or len(group) == 1
            return json_text({
                "engines": engines,
                "active_engine": st.cfg.get("engine_root"),
                "active_editor_binary": st.cfg.get("editor_binary"),
                "note": ("several installs share a version -> selected_for_version marks the "
                         "newest build the proxy resolves to; the running editor's own engine "
                         "always wins over scans. Choose another with proxy__ue_configure "
                         "{engine_root, editor_binary}."),
            })
        if name == "proxy__ue_configure":
            action = args.get("action", "set")
            if action == "scan":
                return json_text({"engines": scan_engines(st.cfg.get("scan_extra_roots")),
                                  "note": "pick engine_root/editor_binary; set project_path to your .uproject"})
            cfg_path = Path(st.cfg["_config_path"])
            existing = {}
            if cfg_path.exists():
                try:
                    existing = json.loads(cfg_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    existing = {}
            for key in ("project_path", "editor_binary", "engine_root", "build_target"):
                if args.get(key):
                    existing[key] = str(args[key])
            cfg_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
            for key in ("project_path", "editor_binary", "engine_root", "build_target"):
                if args.get(key):
                    st.cfg[key] = str(args[key])
            st.ue.cfg = st.cfg
            return json_text({"ok": True, "written": str(cfg_path), "effective": {
                k: st.cfg[k] for k in ("project_path", "editor_binary", "engine_root", "build_target")}})
        return ("error", METHOD_NOT_FOUND, f"unknown proxy tool {name!r}", None)


# --------------------------------------------------------------------------
# background tasks + entrypoint
# --------------------------------------------------------------------------

async def poller(state: ProxyState) -> None:
    """Watch UE liveness; refresh catalog when UE comes up; drop stale upstream session."""
    was_alive = None
    interval = max(3.0, float(state.cfg.get("poll_interval_s", 12)))
    while True:
        await asyncio.sleep(interval)
        try:
            probe = await state.probe(max_age=0)
            alive = probe["alive"]
            if was_alive is None:
                was_alive = len(state.cache.tools) > 0
            if alive and not was_alive:
                log.info("UE MCP came up; adopting its project and refreshing catalog")
                if state.cfg.get("auto_adopt_project"):
                    await state.adopt_running_project()
                await state.refresh_catalog()
            if not alive and state.upstream is not None:
                await state.drop_upstream()
            was_alive = alive
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("poller tick failed")


def build_app(cfg: dict | None = None) -> tuple[FastAPI, ProxyState]:
    cfg = cfg or load_config()
    server = BunnyServer(cfg)
    return server.app, server.state


def main(argv=None) -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="bunny", description="always-on MCP proxy for Unreal Engine 5.8")
    ap.add_argument("--port", type=int, default=None, help="override listen port")
    ap.add_argument("--host", type=str, default=None, help="override listen host")
    ap.add_argument("--config", type=str, default=None, help="path to config.json")
    ap.add_argument("--refresh", action="store_true",
                    help="cache UE tools now (requires UE running), then exit")
    ap.add_argument("--print-config", action="store_true", help="print the effective config and exit")
    ap.add_argument("--emit-client-config", nargs="?", const="", default=None, metavar="PATH",
                    help="no PATH: print the harness's MCP config for this proxy to stdout; "
                         "PATH: write/merge it into that file. Target harness via --agent.")
    ap.add_argument("--agent", choices=sorted(AGENTS), default="claude",
                    help="target harness for --emit-client-config "
                         "(claude, codex, opencode, hermes, copilot, junie; default claude)")
    ap.add_argument("--workspace", type=str, default=None,
                    help="with --emit-client-config: pin this project folder into the "
                         "written entry as an x-harness-workspace header (project-scope "
                         "config files only — write to <project>/.junie/mcp/mcp.json etc.)")
    ns = ap.parse_args(argv)

    cfg = load_config(ns.config)
    if ns.host:
        cfg["host"] = ns.host
    if ns.port:
        cfg["port"] = int(ns.port)

    if ns.print_config:
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
        return
    if ns.emit_client_config is not None:
        text, default_path = emit_client_config(cfg, ns.agent, ns.workspace)
        if ns.emit_client_config:
            written = write_client_config(cfg, ns.emit_client_config, ns.agent, ns.workspace)
            print(f"wrote {ns.agent} config to {written} (entry {SERVER_NAME!r} merged; "
                  f"restart the harness to pick it up)")
        else:
            print(text)
            print(f"# agent: {ns.agent}\n# default global location: {default_path}\n"
                  f"# {AGENTS[ns.agent]['note']}", file=sys.stderr)
        return
    if ns.refresh:
        import asyncio

        async def _r():
            _app, state = build_app(cfg)
            probe = await state.probe(max_age=0)
            if not probe["alive"]:
                print(f"UE MCP not reachable at {cfg['upstream_url']}: {probe['note']}")
                return 1
            res = await state.refresh_catalog()
            print(json.dumps(res, indent=2))
            await state.drop_upstream()
            return 0 if res.get("ok") else 1

        raise SystemExit(asyncio.run(_r()))

    log.info("bunny %s listening on http://%s:%d/mcp -> upstream %s",
             __version__, cfg["host"], cfg["port"], cfg["upstream_url"])
    uvicorn.run(build_app(cfg)[0], host=cfg["host"], port=int(cfg["port"]), log_level="warning")


if __name__ == "__main__":
    main()
