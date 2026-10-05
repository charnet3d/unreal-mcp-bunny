# bunny — always-on MCP proxy for Unreal Engine 5.8

A small proxy that sits between your MCP harness (Junie, Cursor, Claude Code,
VS Code, MCP Inspector, …) and the **ModelContextProtocol** plugin embedded in
Unreal Engine 5.8. Two jobs:

1. **Fix the protocol-version deadlock.** UE 5.8 whitelists
   `2025-11-25 / 2025-06-18 / 2024-11-05` and replies with `2025-11-25` to
   anything else. Kotlin-based SDKs (e.g. Junie's) offer `2025-03-26`, see
   `2025-11-25` back, throw `Server's protocol version is not supported:
   2025-11-25` and mark the server **Failed**. The proxy negotiates *with each
   side in its own language*: it echoes back whatever protocol version the
   client asked for (including `2025-03-26`) and talks `2025-06-18` upstream
   to UE.

2. **Never be offline.** The proxy is its own MCP server. UE's tool catalog is
   cached to `data/tools_cache.json` on first connect and served even when the
   editor is closed, so harnesses keep the same stable MCP surface through
   code → compile → restart-engine loops. Calls to `ue_*` tools route to UE
   when it is running and return an **actionable** error when it is not
   (never a crash/timeout), and local `proxy__*` tools let the agent run the
   engine, build the project, and refresh the catalog itself.

```
harness ──http://127.0.0.1:8765/mcp──► bunny ──http://127.0.0.1:8000/mcp──► UE 5.8
   (any protocol version)             (shim + cache + launcher)          (2025-06-18)
```

## Quick start

```bat
:: 1. install (once)
uv venv --python 3.14 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt

:: 2. point it at your project + engine
copy config.example.json config.json
notepad config.json

:: 3. run the proxy
run_bunny.bat
::   -> http://127.0.0.1:8765/health
::   -> MCP endpoint: http://127.0.0.1:8765/mcp
```

Initial tool cache: start UE once, run `ModelContextProtocol.StartServer` in
its console (or set the plugin's **Auto Start Server**), then:

```bat
.venv\Scripts\python.exe -m bunny.server --refresh
```

After that the tools stay cached. UE can come and go freely.

### Point your harness at it

CLI-only (there is deliberately no MCP tool for this — it's setup, not a
runtime capability):

```bat
:: print the entry for a harness to stdout (default --agent claude),
:: plus its default global config location on stderr
.venv\Scripts\python.exe -m bunny.server --emit-client-config

:: write/merge the entry into a file (existing servers in it are preserved)
.venv\Scripts\python.exe -m bunny.server --emit-client-config "C:\path\mcp.json" --agent junie
```

`--agent` picks the harness syntax and default location:

| `--agent` | Syntax emitted | Default global location (no `--path`) |
| --- | --- | --- |
| `claude` (default) | Claude Desktop `mcpServers` stdio entry bridging via `npx -y mcp-remote <url>` (Desktop has no native url transport; needs Node) | `%APPDATA%\Claude\claude_desktop_config.json` |
| `codex` | TOML `[mcp_servers.unreal-via-bunny]` with `url = …` (Streamable HTTP native) | `%USERPROFILE%\.codex\config.toml` |
| `opencode` | `opencode.json` `"mcp": {"unreal-via-bunny": {"type": "remote", "url": …}}` | `%USERPROFILE%\.config\opencode\opencode.json` |
| `hermes` | YAML `mcp_servers.unreal-via-bunny.url` | `%HERMES_HOME%\config.yaml` (`~/.hermes/config.yaml`) |
| `copilot` | VS Code user `mcp.json` `"servers": {"unreal-via-bunny": {"type": "http", "url": …}}` | `%APPDATA%\Code\User\mcp.json` |
| `junie` | `mcpServers` with `"type": "http", "url": …` | `%USERPROFILE%\.junie\mcp\mcp.json` |

Writes merge: JSON files keep their other servers, TOML/YAML replace only the
bunny section. Restart the harness afterwards. Scope notes: Junie project scope
`.junie/mcp/mcp.json`; Copilot CLI is `~/.copilot/mcp-config.json`
(`mcpServers` + `type`, write via explicit path); opencode also reads a
project-root `opencode.json`. Junie example of the emitted entry:

```json
{
  "mcpServers": {
    "unreal-via-bunny": { "type": "http", "url": "http://127.0.0.1:8765/mcp" }
  }
}
```

No special client settings needed: the proxy accepts 2024-11-05, 2025-03-26,
2025-06-18, 2025-11-25 and 2026-07-28 clients.

## The tool surface

Always available (proxy-local, prefixed `proxy__`):

| Tool | Purpose |
| --- | --- |
| `proxy__ue_status` | Is UE's MCP reachable? Editor process running? Launch/build possible? |
| `proxy__ue_launch` | Start the editor (`-ExecCmds=ModelContextProtocol.StartServer`) and block until MCP is reachable; re-caches tools. Always the GUI `UnrealEditor.exe` (see launch rules). |
| `proxy__ue_wait_ready` | Block until MCP reachable (use after you start UE manually / after live-coding). |
| `proxy__ue_build` | `Build.bat <Target> Win64 Development -project=…` — compiles with UE closed; auto-relaunches the editor on success (`auto_launch_after_build`). |
| `proxy__ue_kill` | Terminate the editor (`confirmed=true` required; no autosave). |
| `proxy__ue_refresh_cache` | Re-cache UE's tools/resources/prompts (do this after enabling more toolset plugins). |
| `proxy__cache_stats` | Catalog counts, capture time, effective config. |
| `proxy__ue_configure` | `scan` engine installs / write project+engine paths from inside the harness. |
| `proxy__ue_adopt_project` | Re-detect the running editor's project+engine and adopt as launch/build defaults. |
| `proxy__ue_enable_toolset` | Enable one toolset plugin (e.g. `LiveCodingToolset`) in the active project's `.uproject` (backs it up first). |
| `proxy__ue_sync_toolsets` | `{dry_run?}` — reconcile the project's toolset list with the engine: add gap plugins AllToolsets doesn't cover, prune redundant/vanished ones, respect `Enabled:false` opt-outs, idempotent. |
| `proxy__ue_engines` | List every installed engine (version, full build, path, discovery source, which is selected). |

Cached UE tools are listed as `ue_<toolname>` (e.g. `ue_list_toolsets`,
`ue_call_tool`), with their real UE schemas, at all times — UE online or not.
When UE is live they forward verbatim (name demangled) and responses pass
through untouched.

The intended agent loop:

```
(proxy__ue_status) -> ue offline
(proxy__ue_build)  -> compile the C++ you just edited
(proxy__ue_launch) -> editor up, MCP reachable, catalog refreshed
(ue_call_tool)     -> routed to the live engine
…close editor…     -> proxy stays up, tools stay listed, calls stay actionable
```

## Configuration

`config.json` (copy from `config.example.json`), `data/`, and `logs/` live in
the **root dir**: the folder containing the `bunny` package, overridable with
the `BUNNY_ROOT_DIR` env var — point it at a deployment folder to keep your
config/state separate from the source tree and to run several independent
instances. `config.json` is gitignored; `config.example.json` is the committed
reference.

| Key | Meaning |
| --- | --- |
| `host` / `port` | Proxy bind address (default `127.0.0.1:8765`). |
| `upstream_url` | UE MCP endpoint (default `http://127.0.0.1:8000/mcp`). |
| `project_path` | Absolute path to your `.uproject`. Enables launch/build. |
| `editor_binary` | **`<engine>\Engine\Binaries\Win64\UnrealEditor.exe`** (GUI editor). Never `UnrealEditor-Cmd.exe` — see launch rules. |
| `engine_root` | Derived from `editor_binary` if omitted; used by `proxy__ue_build`. |
| `build_target` | Defaults to the Editor target discovered from `Source/*.Target.cs` (falls back to `<ProjectName>Editor`). |
| `platform` / `configuration` | Build parameters (default `Win64` / `Development`). |
| `launch_args` | Appended on launch; keep `-ExecCmds=ModelContextProtocol.StartServer`. |
| `launch_template` / `build_template` | Full command templates (placeholders `{editor} {project} {target} {platform} {configuration} {engine_root} {extra_args}`). |
| `poll_interval_s` | UE liveness poll (default 12s; cache auto-refresh when UE comes up). |
| `forward_timeout_s`, `build_timeout_s`, `launch_ready_timeout_s` | Long-call budgets. |
| `auto_adopt_project`, `auto_launch_after_build` | Adoption / relaunch behaviors (see below). |
| `scan_extra_roots` | Extra folders to scan for engine installs (engines kept outside the standard roots). |

Any key can be overridden per-run with env vars: `BUNNY_PORT=8766`,
`BUNNY_UPSTREAM_URL=…`, `BUNNY_PROJECT_PATH=…` (handy for second instances).
`--print-config` shows the effective config.

## Follows any project (project adoption)

MCP tool routing is project-agnostic: UE owns `127.0.0.1:8000`, so whichever
project the editor has open is what the `ue_*` tools act on. Launch/build
defaults follow it automatically: when UE comes up, the proxy detects the
running editor's `.uproject` from its command line, resolves its engine,
discovers its build targets from `Source/*.Target.cs`, and stores the result
in `data/adopted.json` (survives proxy restarts; `config.json` stays the
static default). `proxy__ue_status` shows `active_project / active_engine /
adopted_from`; `proxy__ue_adopt_project` forces a re-scan after you open a
different project.

Engine discovery is **layered, never guessed**: running editor's own
executable → Epic Launcher manifests → Windows registry → standard install
roots → `scan_extra_roots`. When every layer misses, the proxy reports and
asks instead of guessing. Details:
[docs/implementation_details.md](docs/implementation_details.md).

`proxy__ue_sync_toolsets {"dry_run": true?}` keeps the project's toolset list
lean and engine-proof: `AllToolsets` is the enable-all switch (its manifest is
engine-maintained), the project file stores only the gap plugins it doesn't
cover. Sync adds missing gap plugins, prunes redundant/vanished ones, respects
explicit `Enabled:false` opt-outs, never touches marketplace plugins, backs up
the `.uproject`, and is idempotent. After a real sync run `proxy__ue_build`
(UE closed) to compile new modules — it auto-relaunches the editor.

`proxy__ue_enable_toolset {"plugin": "LiveCodingToolset"}` enables any engine
toolset plugin in the active project's `.uproject`; afterwards run
`proxy__ue_build` and `ue_list_toolsets` shows the new toolset.
LiveCodingToolset ships `EnabledByDefault: false`, so a harness that wants
in-editor compiles calls this once per project.

## Notes & limits

- The proxy binds to 127.0.0.1 and does not add auth; treat it like UE's own
  MCP server (local only). UE's optional MCP auth token, if you enable it, is
  not forwarded — leave UE's server unauthenticated on loopback.
- Server→client SSE streams (`GET /mcp`) answer 405: allowed by the spec;
  clients fall back to polling. Catalog freshness comes from refresh-on-
  launch / refresh-on-UE-boot / 60s-refresh-on-call.
- Tool-name collisions with harnesses are avoided by the `ue_` prefix.
  Unprefixed cached names are also accepted, in case a harness strips prefixes.
- Resources/prompts are served from cache when offline; `resources/read` /
  `prompts/get` need UE live (forwarded, actionable error otherwise).
- `proxy__ue_launch` detects "editor running but MCP server off" and tells the
  agent to run `ModelContextProtocol.StartServer` inside it.
- Launch args must **not** include `-unattended`: UE then runs the ExecCmds and
  exits seconds later. `-ExecCmds=ModelContextProtocol.StartServer -nosplash`
  keeps the editor alive.
- **The proxy only starts a persistent editor with the GUI
  `UnrealEditor.exe`.** `UnrealEditor-Cmd.exe` is for command/test runs that
  are supposed to close the engine afterwards. If `editor_binary` points at
  `-Cmd`, the proxy silently switches to the sibling `UnrealEditor.exe`; if
  that sibling doesn't exist it refuses with `cmd_editor_not_allowed` and
  tells you to fix `editor_binary`. Editor detection (`running_project`,
  `proxy__ue_status`) still *sees* `-Cmd` processes — reading is fine,
  launching is not. Why these rules exist:
  [docs/implementation_details.md](docs/implementation_details.md).

## Running as a Windows service (nssm)

`install_service.bat` (run **elevated** — `elevate_install.bat` elevates it
with one UAC click; set `NSSM=` at the top of install_service.bat to your
nssm.exe) (re)installs `ue-mcp-bunny`: app = `.venv\Scripts\python.exe -m
bunny.server`, working dir = the folder containing the script, autostart,
account **LocalSystem**. Run it from the folder you want the proxy to live in
(that folder holds its `config.json` / `data/` / `logs/`).

- **LocalSystem is mandatory** — an editor launched from a service must land
  on your desktop, and only LocalSystem has the privileges to reach the
  interactive session. Quick check: `sc qc ue-mcp-bunny` →
  `SERVICE_START_NAME : LocalSystem`, and `proxy__ue_status` →
  `window.cross_session_route: "WTSQueryUserToken"`.
- `restart` to load new code: `nssm restart ue-mcp-bunny` (elevated).
- The full story (why running the service as your user fails, session-0
  spawn routes, environment blocks):
  [docs/implementation_details.md](docs/implementation_details.md).

## Tests

```bat
:: self-contained (mocked, no real UE needed)
.venv\Scripts\python.exe tests\test_proxy.py         :: proxy vs a mock UE (incl. the strict-SDK version deadlock)
.venv\Scripts\python.exe tests\test_sdk_client.py    :: via the official MCP Python SDK client
.venv\Scripts\python.exe tests\test_targets.py       :: build-target discovery
.venv\Scripts\python.exe tests\test_workspace.py     :: harness workspace detection
.venv\Scripts\python.exe tests\test_cmd_guard.py     :: proxy never launches UnrealEditor-Cmd.exe
.venv\Scripts\python.exe tests\test_crosssession.py  :: session-0 -> interactive-session spawn logic
.venv\Scripts\python.exe tests\test_session_policy.py:: client session/policy bookkeeping
.venv\Scripts\python.exe tests\test_window.py        :: child window policies (spawns short consoles)
.venv\Scripts\python.exe tests\test_guardian.py      :: keeps build consoles minimized

:: real end-to-end (needs a running proxy + configured project/engine)
.venv\Scripts\python.exe tests\e2e_real.py           :: launch -> cache -> real tool calls
.venv\Scripts\python.exe tests\e2e_lifecycle.py --keep-ue  :: kill UE -> offline cached tools -> proxy relaunch -> live routing
.venv\Scripts\python.exe tests\e2e_soak.py --minutes 3     :: editor survives 3 min after proxy launch
.venv\Scripts\python.exe tests\e2e_adoption.py       :: running project auto-adoption recorded cleanly in data/adopted.json
.venv\Scripts\python.exe tests\e2e_build.py          :: build with UE closed -> auto-relaunch
```

Logs: `logs/bunny.log` (proxy), `logs/editor_launch.log` (UE), `logs/build.log`.

## Docs

- [docs/implementation_details.md](docs/implementation_details.md) — design
  notes: protocol shim internals, engine resolution layers, launch rules and
  the `-Cmd` trap, the service/session-0 story, toolset sync semantics.
