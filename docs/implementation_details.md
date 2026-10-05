# Implementation details

Design notes and the reasoning behind the rules in the README — internals you
don't need to run the proxy, but that explain why it behaves the way it does.

## Protocol shim

UE's ModelContextProtocol plugin answers `initialize` with a protocol version
from its own whitelist (`GetSupportedProtocolVersions()`: 2025-11-25 /
2025-06-18 / 2024-11-05). Strict SDKs abort when the echoed version isn't one
they offered — a Kotlin-based client offering `2025-03-26` dies with
`Server's protocol version is not supported: 2025-11-25`. The proxy:

- answers every client `initialize` with **the version the client asked for**
  (accepted set: 2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25, 2026-07-28);
- speaks upstream in a UE-safe version — it offers `("2025-06-18",
  "2024-11-05", "2025-11-25")` in preference order. `2025-06-18` is the newest
  one strict clients also accept, so UE's `2025-11-25` never leaks downstream;
- tolerates UE replying with a single JSON body *or* an SSE stream
  (hand-rolled Streamable-HTTP client in `bunny/upstream.py` for full
  handshake control).

`GET /mcp` (server→client SSE) answers 405 — spec-allowed; clients fall back
to polling. A debug trace logs what clients actually send (`DEBUG` level via
`BUNNY_LOG_LEVEL`), which is how the strict-SDK handshake was diagnosed.

## Launch rules and the `-Cmd` trap

- `UnrealEditor-Cmd.exe` reads **stdin as console commands**: with
  `stdin=DEVNULL` (what any detached launcher gives), EOF means "quit" and the
  editor dies ~1 minute after a successful boot. So: the proxy only launches
  persistent editors with the GUI `UnrealEditor.exe`; a `-Cmd` `editor_binary`
  is silently switched to its GUI sibling, or refused with
  `cmd_editor_not_allowed`. Detection still sees `-Cmd` processes (reading is
  fine, launching is not). `tests/test_cmd_guard.py` guards this.
- Launch args must not include `-unattended` — UE then runs the ExecCmds and
  exits seconds later. Keep `-ExecCmds=ModelContextProtocol.StartServer
  -nosplash`.
- Windows spawn flags: `CREATE_NEW_CONSOLE` alone. `DETACHED_PROCESS |
  CREATE_NEW_CONSOLE` together is `ERROR_INVALID_PARAMETER`.
- GUI children of the service spawn `DETACHED_PROCESS` so a service stop
  (Ctrl+C to the service console) can't kill the editor with 0xC000013A.
- Build consoles get created minimized and a "guardian" re-minimizes them
  when UnrealBuildTool/conhost restore them — without activating (stealing the
  foreground). See `tests/test_guardian.py`, `tests/test_window.py`.

## Engine resolution — layered, never guessed

1. **Running editor's own executable path** —
   `…\Engine\Binaries\Win64\UnrealEditor.exe` → engine root. Authoritative:
   the engine you have open IS the engine adopted.
2. **Epic Launcher manifest store** —
   `%ProgramData%\Epic\EpicGamesLauncher\Data\Manifests\*.item` (JSON
   content, `.item` extension). Engine entries carry `InstallLocation`,
   `AppName` (`UE_5.8`), `AppVersionString` (`5.8.3-…`), `TechnicalType`
   (`engines/ue5`). Filtered to engines (games share the folder). Reflects
   what the Launcher installed, even when the registry is stale.
3. **Windows registry** —
   `HKLM/HKCU\SOFTWARE\Epic Games\Unreal Engine\Builds`, registry *values*
   (not subkeys): name = version string (`5.8`) for Launcher installs, a
   source-build **GUID** otherwise; data = engine root. A project with a GUID
   `EngineAssociation` matches the value name directly, so source-build
   projects resolve automatically.
4. Standard roots `X:\Program Files\Epic Games` (C–H).
5. `scan_extra_roots` (config list; env `BUNNY_SCAN_EXTRA_ROOTS='a;b'`) —
   per-machine for engines in custom folders; nothing custom is hardcoded.

Engine version comes from `Engine\Build\Build.version` (not the folder name).
The running editor's engine always wins over scans. Same-version duplicates
are deduped by normalized root and resolved to the **newest full build**
deterministically; `proxy__ue_engines` lists every install with
`selected_for_version`, and `proxy__ue_configure {engine_root,
editor_binary}` picks another. When every layer misses (e.g. source-build GUID
association with the editor closed), the proxy **reports and asks**
(`engine_unresolved`) instead of guessing.

## Build-target discovery

Targets come from `Source/*.Target.cs` (Type = TargetType.*), **not** from the
`.uproject` name — a project named `Foo` may contain only `Lyra*` targets.
Pick order: declared Editor target → Epic convention `<Stem>Editor` →
fallback `<Stem>Editor`. Stale persisted targets from a previously adopted
project are ignored unless the *current* project declares them.
`tests/test_targets.py` covers all of this.

## The service / session-0 story

A Windows service runs in **session 0**, which has no interactive desktop.

- Running the service **as your user** is not enough: it then lacks
  `SeImpersonatePrivilege`/`SeTcbPrivilege`, so the proxy cannot borrow the
  console session's token; every editor launch falls back to session 0, where
  the GUI editor dies with a D3D12 swap-chain fatal
  (`DXGI_ERROR_NOT_CURRENTLY_AVAILABLE`, exit 3, `D3D12Util.cpp:1062`).
- **LocalSystem** holds SeImpersonate + SeTcb: bunny uses
  `WTSQueryUserToken` (token of the active console session) and spawns the
  editor into your desktop.
- The right spawn call is **CreateProcessAsUserW** — the child lands in the
  token's session (= your desktop). `CreateProcessWithTokenW` is the wrong
  call: it places the child in the *service's* session 0 with the user's
  identity — invisible and RHI-dead.
- Children get the **token user's environment** via `CreateEnvironmentBlock`;
  a LocalSystem process's own environment is SYSTEM's (`%USERPROFILE%` =
  systemprofile), which would misplace UE's user settings.
- Launching from session 0 without a usable token route is refused with
  `service_session_no_desktop` + fix instructions.
- The nssm service account cannot be changed with `sc config` (access denied
  on object changes) — it must go through `nssm set ue-mcp-bunny ObjectName
  LocalSystem`. The nssm build used here rejects an `ObjectPassword`
  parameter outright (it aborts before `ObjectName` lands); LocalSystem needs
  none.

## Project adoption & harness workspaces

- UE owns `127.0.0.1:8000`, so MCP routing follows whichever editor is up.
  Adoption (project + engine + target) writes `data/adopted.json`; the
  `.uproject` is taken from the running editor's command line (bare path, no
  exe prefix — checked in `tests/e2e_adoption.py`).
- Harness workspaces: clients may advertise `roots/list`; some (Junie) can't,
  so a project-scope config entry can pin a literal `x-harness-workspace`
  header (`--emit-client-config --workspace <folder>`). Priority: running
  editor > pinned workspace header > configured project. Detection probes the
  peer's socket, headers, and process cwd (`bunny/workspace.py`).

## Toolset sync semantics

`AllToolsets` is the enable-all switch and its manifest is engine-maintained —
toolsets Epic adds arrive with engine updates without `.uproject` churn. The
project file stores ONLY gap plugins AllToolsets doesn't cover (UE 5.8 era:
LiveCodingToolset, MVVMToolset, SequencerAnimMixerToolset,
ChaosClothAssetToolset, MetaHumanGenerator). Sync adds missing gap plugins,
prunes entries that became redundant (now in AllToolsets' manifest) or
vanished from the engine (renamed/removed upstream → avoids "missing plugin"
prompts on minor-version updates), respects explicit `Enabled:false` opt-outs,
never touches marketplace plugins, backs up the `.uproject`, idempotent.
MetaHumanGenerator's toolset needs the MetaHuman plugin stack (not bundled) →
opts out as `Enabled:false`.

## Cache lifecycle

- `data/tools_cache.json` written atomically (temp + replace) on connect /
  launch / `--refresh`; served with `ue_`-prefixed names while UE is offline.
- Poller (default 12s) notices UE booting and auto-refreshes; a call older
  than 60s triggers a lazy refresh.
- "Editor running, MCP server off" is detected and reported with the exact
  remedy (`ModelContextProtocol.StartServer` inside the editor).
