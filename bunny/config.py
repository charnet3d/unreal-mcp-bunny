"""Configuration handling for the bunny proxy.

config.json lives in the root dir (BUNNY_ROOT_DIR env, else the folder
containing the bunny package). Environment variables of the form BUNNY_<KEY>
override any config key (e.g. BUNNY_UPSTREAM_URL), which is used by tests and
lets you run several instances.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
ROOT = (Path(os.environ["BUNNY_ROOT_DIR"]).resolve()
        if os.environ.get("BUNNY_ROOT_DIR")
        else PKG_DIR.parent)
ENV_PREFIX = "BUNNY_"

_TARGETS_CACHE: dict[str, tuple[float, list]] = {}  # Source dir mtime -> target list
_TARGET_TYPE_RE = re.compile(r"Type\s*=\s*TargetType\.(\w+)")


def default_config() -> dict:
    return {
        "host": "127.0.0.1",
        "port": 8765,
        "upstream_url": "http://127.0.0.1:8000/mcp",
        "poll_interval_s": 12,
        "forward_timeout_s": 900,
        "auto_refresh_on_connect": True,
        "project_path": "",
        "engine_root": "",
        "editor_binary": "",
        "build_target": "",
        "platform": "Win64",
        "configuration": "Development",
        "launch_template": '"{editor}" "{project}" {extra_args}',
        "launch_args": '-ExecCmds="ModelContextProtocol.StartServer"',
        "build_template": '"{engine_root}/Engine/Build/BatchFiles/Build.bat" {target} {platform} {configuration} -project="{project}" -NoHotReload',
        "build_timeout_s": 1800,
        "build_window": "minimized",
        "launch_window": "normal",
        "launch_ready_timeout_s": 240,
        "auto_adopt_project": True,
        "workspace_adopt": True,
        "workspace_project": "",
        "auto_launch_after_build": True,
        "scan_extra_roots": [],
    }


def load_config(path: str | os.PathLike | None = None) -> dict:
    cfg = default_config()
    p = Path(path) if path else ROOT / "config.json"
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            user = json.load(f)
        cfg.update({k: v for k, v in user.items() if not k.startswith("_")})
    cfg["_config_path"] = str(p)
    cfg["_root"] = str(ROOT)
    # env overrides: BUNNY_PORT=8766, BUNNY_UPSTREAM_URL=..., BUNNY_PROJECT_PATH=...
    for key in list(cfg.keys()):
        if key.startswith("_"):
            continue
        env = os.environ.get(ENV_PREFIX + key.upper())
        if env is None:
            continue
        default = cfg[key]
        if isinstance(default, bool):
            cfg[key] = env.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(default, list):
            cfg[key] = [s.strip() for s in env.split(";") if s.strip()]
        elif isinstance(default, (int, float)) and not isinstance(default, bool):
            cfg[key] = type(default)(float(env))
        else:
            cfg[key] = env
    return cfg


def project_name(cfg: dict) -> str:
    pp = cfg.get("project_path") or ""
    return Path(pp).stem if pp else ""


def discover_targets(uproject: str) -> list[dict]:
    """Every UBT target declared by a project: [{name, type}] from Source/*.Target.cs.

    This is the engine's own source of truth (what UBT scans), so a project
    named FooGame with only Lyra*.Target.cs yields Lyra targets, never
    a phantom 'FooGameEditor' that Build.bat cannot resolve.
    Cached briefly — safe to call from can_build/status on every request.
    """
    if not uproject:
        return []
    src = Path(uproject).parent / "Source"
    key = str(src)
    try:
        stamp = src.stat().st_mtime
    except OSError:
        return []
    hit = _TARGETS_CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    found: list[dict] = []
    if src.is_dir():
        for f in sorted(src.glob("*.Target.cs")):
            try:
                txt = f.read_text(encoding="utf-8-sig", errors="ignore")
            except OSError:
                continue
            m = _TARGET_TYPE_RE.search(txt)
            # LyraEditor.Target.cs declares target 'LyraEditor' (UBT drops the
            # '.Target' suffix; Build.bat rejects 'LyraEditor.Target')
            name = re.sub(r"\.Target$", "", f.stem)
            found.append({"name": name, "type": m.group(1) if m else "Unknown"})
    _TARGETS_CACHE[key] = (stamp, found)
    return found


def pick_target(cfg: dict, uproject: str) -> tuple[str, str]:
    """(target, source) to build for `uproject`.

    source: persisted | convention | scan | fallback. The persisted target is
    honoured only when that project declares it — an adoption must never
    compile the previous project's target.
    """
    stem = re.sub(r"[^A-Za-z0-9_]", "", Path(uproject).stem)
    persisted = cfg.get("build_target") or ""
    targets = discover_targets(uproject)
    by_name = {t["name"]: t for t in targets}
    if persisted and (not targets or persisted in by_name):
        return persisted, "persisted"
    if not targets:
        return (f"{stem}Editor" if stem else persisted), "fallback"
    if f"{stem}Editor" in by_name:  # Epic's naming convention
        return f"{stem}Editor", "convention"
    editors = [t["name"] for t in targets if t["type"].lower() == "editor"]
    if stem:
        named = [n for n in editors if stem.lower() in n.lower()]
        if named:
            return named[0], "scan"
        named = [n for n in by_name if stem.lower() in n.lower()]
        if named:
            return named[0], "scan"
    # an Editor target is what a project compile needs (editor binaries +
    # hot reload); Lyra-style projects name theirs after the template.
    if editors:
        return editors[0], "scan"
    return next(iter(by_name), f"{stem}Editor"), "scan"


def derive_target(cfg: dict) -> str:
    target = cfg.get("build_target") or ""
    pp = cfg.get("project_path") or ""
    if not pp:
        return target
    return pick_target(cfg, pp)[0]


def target_for_project(cfg: dict, uproject: str) -> str:
    """Build target to use when adopting `uproject` (see pick_target)."""
    return pick_target(cfg, uproject)[0]


def derive_engine_root(cfg: dict) -> str:
    root = cfg.get("engine_root") or ""
    if root:
        return root
    ed = cfg.get("editor_binary") or ""
    if ed:
        p = Path(ed)  # .../Engine/Binaries/Win64/UnrealEditor(.Cmd).exe
        for _ in range(3):
            p = p.parent
        if p.name.lower() == "engine":
            return str(p)
    return ""


def render_template(template: str, cfg: dict) -> list[str]:
    """Render a launch/build template into an argv list.

    Placeholders: {editor} {project} {project_dir} {project_file} {pname}
    {target} {platform} {configuration} {engine_root} {extra_args}
    Quotes around a placeholder are preserved as shell quotes when it expands
    to a path containing spaces (cmd-style templates rely on them).
    """
    name = project_name(cfg)
    mapping = {
        "editor": cfg.get("editor_binary") or "",
        "project": cfg.get("project_path") or "",
        "project_dir": str(Path(cfg["project_path"]).parent) if cfg.get("project_path") else "",
        "project_file": Path(cfg["project_path"]).name if cfg.get("project_path") else "",
        "pname": name,
        "target": derive_target(cfg),
        "platform": cfg.get("platform") or "Win64",
        "configuration": cfg.get("configuration") or "Development",
        "engine_root": derive_engine_root(cfg),
        "extra_args": cfg.get("launch_args") or "",
    }
    rendered = template
    for key, value in mapping.items():
        rendered = rendered.replace("{" + key + "}", value)
    # tokenize windows-style: keeps quoted segments
    import shlex

    try:
        argv = shlex.split(rendered, comments=False, posix=False)
    except ValueError:
        argv = rendered.split()

    # CreateProcess list-argv needs NO shell quotes: strip every quote char.
    # posix=False kept them, and mid-token quotes (e.g. -project="D:\x.uproject")
    # reach UBT as literal '"' characters -> invalid path -> exit 6.
    # Windows paths cannot contain '"' so this loses nothing.
    return [a.replace('"', "") for a in argv]


def _engine_version(engine_root: Path) -> str:
    """Authoritative engine version from Engine/Build/Build.version (json)."""
    try:
        v = json.loads((engine_root / "Engine" / "Build" / "Build.version").read_text(encoding="utf-8"))
        return f"{v.get('MajorVersion')}.{v.get('MinorVersion')}"
    except (OSError, json.JSONDecodeError, ValueError):
        return ""


def _mk_engine(root: str) -> dict | None:
    rp = Path(root)
    editor = rp / "Engine" / "Binaries" / "Win64" / "UnrealEditor.exe"
    if not editor.is_file():
        return None
    full = ""
    try:
        v = json.loads((rp / "Engine" / "Build" / "Build.version").read_text(encoding="utf-8"))
        full = f"{v.get('MajorVersion')}.{v.get('MinorVersion')}.{v.get('PatchVersion')}"
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return {"version": _engine_version(rp) or rp.name.replace("UE_", ""),
            "build_version_full": full,
            "engine_root": str(rp), "editor_binary": str(editor), "source": ""}


def _version_key(v: str) -> tuple:
    parts = re.findall(r"\d+", v or "")
    return tuple(int(p) for p in parts) or (0,)


def _scan_registry() -> list[dict]:
    """Authoritative source #2: Epic's engine registration registry.

    HKLM/HKCU 'SOFTWARE\\Epic Games\\Unreal Engine\\Builds' stores each installed
    engine as a registry VALUE: name = version string ('5.8') for Launcher
    installs, or the source-build GUID; data = engine root path. This is what
    UnrealVersionSelector and UBT read. A project's GUID EngineAssociation
    matches the value NAME directly.
    """
    out = []
    try:
        import winreg
    except ImportError:
        return out
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(hive, r"SOFTWARE\Epic Games\Unreal Engine\Builds")
        except OSError:
            continue
        try:
            for i in range(winreg.QueryInfoKey(key)[1]):  # value count
                name, data, _typ = winreg.EnumValue(key, i)
                if not data:
                    continue
                eng = _mk_engine(str(data))
                if not eng:
                    continue
                eng["source"] = "registry"
                eng["registry_name"] = str(name)
                if re.fullmatch(r"\d+\.\d+", str(name)):
                    eng["version"] = str(name)
                out.append(eng)
        except OSError:
            pass
        winreg.CloseKey(key)
    return out


def _scan_launcher_manifests() -> list[dict]:
    """Authoritative source #1: Epic Games Launcher installed-app store.

    %ProgramData%\\Epic\\EpicGamesLauncher\\Data\\Manifests\\*.item — JSON content
    with .item extension. Engine entries carry InstallLocation (the engine root),
    AppName ('UE_5.8'), AppVersionString ('5.8.3-…'), TechnicalType
    ('engines/ue5,engines'). Filter to engines; games share the folder.
    """
    out = []
    pd = Path(os.environ.get("ProgramData", Path.home() / "ProgramData"))
    mdir = pd / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    if not mdir.is_dir():
        return out
    for mf in list(mdir.glob("*.item")) + list(mdir.glob("*.json")):
        try:
            data = json.loads(mf.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        technical = str(data.get("TechnicalType") or "").lower()
        app = str(data.get("AppName") or "")
        if "engines" not in technical and not re.match(r"(?i)^ue_", app):
            continue
        eng = _mk_engine(str(data.get("InstallLocation")
                            or data.get("InstallDirectory") or ""))
        if not eng:
            continue
        eng["source"] = "launcher_manifest"
        m = re.match(r"(\d+\.\d+)", str(data.get("AppVersionString") or ""))
        if m and not eng["version"]:
            eng["version"] = m.group(1)
        eng["app_name"] = app
        out.append(eng)
    return out


def scan_engines(extra_roots: list[str] | None = None) -> list[dict]:
    """Discover installed engines: registry + launcher manifests first, standard
    Launcher manifest store is first (what the Launcher itself installed,
    newest first); registry second (source builds, older/partial entries);
    standard roots as last resort. Custom folders are NOT hardcoded — pass them
    via config 'scan_extra_roots' / env BUNNY_SCAN_EXTRA_ROOTS.
    """
    found: list[dict] = []
    seen: set[str] = set()

    def add(items: list[dict]) -> None:
        for e in items:
            key = os.path.normpath(e["engine_root"]).lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(e)

    add(_scan_launcher_manifests())
    add(_scan_registry())
    # last resort: standard roots (drive-letter Program Files Epic Games only)
    roots = []
    for drive in list("CDEFGH"):
        roots.append(f"{drive}:\\Program Files\\Epic Games")
    roots += list(extra_roots or [])
    for root in roots:
        rp = Path(root)
        if not rp.is_dir():
            continue
        for child in sorted(rp.iterdir()):
            if child.is_dir():
                eng = _mk_engine(str(child))
                if eng:
                    eng["source"] = eng["source"] or "dir_scan"
                    add([eng])
    return found


def engine_from_editor_exe(exe_path: str) -> dict | None:
    """Derive the engine from a RUNNING editor's executable path:
    <root>/Engine/Binaries/Win64/UnrealEditor[.exe|-Cmd.exe] -> <root>.
    Zero guessing: the engine you have open IS the engine to adopt."""
    try:
        p = Path(exe_path)
        # <root>\Engine\Binaries\Win64\UnrealEditor.exe -> root = parents[3]
        if p.parents[2].name.lower() != "engine":
            return None
        root = p.parents[3]
    except (OSError, ValueError, IndexError):
        return None
    editor = root / "Engine" / "Binaries" / "Win64" / "UnrealEditor.exe"
    if not editor.is_file():
        return None
    return {"version": _engine_version(root), "engine_root": str(root),
            "editor_binary": str(editor), "source": "running_editor"}


def resolve_engine_for_project(uproject: str, engines: list[dict] | None = None) -> dict | None:
    """Map a .uproject's EngineAssociation to an installed engine (from scan_engines).

    Returns {'engine_root','editor_binary','version'} with editor_binary pointing at
    the GUI UnrealEditor.exe — long-lived editor sessions must not use
    UnrealEditor-Cmd.exe (it is for command/test runs that close the engine after).
    Returns None when the association is a source-build GUID or the version is not installed.
    """
    try:
        data = json.loads(Path(uproject).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    assoc = str(data.get("EngineAssociation") or "")
    engines = engines if engines is not None else scan_engines()
    if not assoc:
        return None
    # same engine found through several layers -> dedupe by normalized root
    uniq: dict[str, dict] = {}
    for e in engines:
        uniq.setdefault(os.path.normpath(e["engine_root"]).lower(), e)
    # GUID association (source build): matches a registry value NAME directly
    matches = [e for e in uniq.values()
               if e.get("version") == assoc or e.get("registry_name") == assoc]
    if not matches:
        return None
    # multiple installs of the same version (e.g. C: and D: copies): newest
    # build first, deterministic; caller reports the choice
    matches.sort(key=lambda e: _version_key(e.get("build_version_full") or e.get("version")),
                 reverse=True)
    return dict(matches[0])


SERVER_NAME = "unreal-via-bunny"


def _proxy_url(cfg: dict) -> str:
    return f"http://{cfg['host']}:{cfg['port']}/mcp"


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


# Per-harness MCP config syntax, verified against each vendor's docs (2026-01).
# kind: json  -> merge snippet[key][SERVER_NAME] into the file
# kind: toml  -> append/replace the [mcp_servers.<name>] section in a TOML file
# kind: yaml  -> append/merge the mcp_servers.<name> fragment in a YAML file
AGENTS: dict[str, dict] = {
    "claude": {
        "kind": "json", "key": "mcpServers",
        "default": _appdata() / "Claude" / "claude_desktop_config.json",
        "build": lambda url: {"command": "npx",
                              "args": ["-y", "mcp-remote", url]},
        "note": "Claude Desktop has no native url transport; npx mcp-remote bridges "
                "the stdio entry to the proxy. Needs Node.js. Recent Desktop builds "
                "also accept \"url\" directly — swap the entry if yours does.",
    },
    "codex": {
        "kind": "toml", "key": "mcp_servers",
        "default": Path.home() / ".codex" / "config.toml",
        "build": lambda url: {"url": url},
        "note": "Codex speaks Streamable HTTP natively. On older codex-cli add "
                "experimental_use_rmcp_client = true at the top of config.toml.",
    },
    "opencode": {
        "kind": "json", "key": "mcp",
        "default": Path.home() / ".config" / "opencode" / "opencode.json",
        "build": lambda url: {"type": "remote", "url": url, "enabled": True},
        "note": "Global config; per-project opencode.json in the repo root works "
                "the same way. Restart opencode to pick it up.",
    },
    "hermes": {
        "kind": "yaml", "key": "mcp_servers",
        "default": _hermes_home() / "config.yaml",
        "build": lambda url: {"url": url},
        "note": "Hermes reads mcp_servers from config.yaml; tools register as "
                "mcp_unreal-via-bunny_*. Restart Hermes to pick it up.",
    },
    "copilot": {
        "kind": "json", "key": "servers",
        "default": _appdata() / "Code" / "User" / "mcp.json",
        "build": lambda url: {"type": "http", "url": url},
        "note": "VS Code Copilot user scope. Copilot CLI is a different file "
                "(~/.copilot/mcp-config.json, top key mcpServers) — write it via "
                "--path. Workspace scope: .vscode/mcp.json.",
    },
    "junie": {
        "kind": "json", "key": "mcpServers",
        "workspace_header": "x-harness-workspace",
        "default": Path.home() / ".junie" / "mcp" / "mcp.json",
        "build": lambda url, ws=None: ({"type": "http", "url": url}
                                        if not ws else
                                        {"type": "http", "url": url,
                                         "headers": {"x-harness-workspace": str(ws)}}),
        "note": "User scope; project scope is .junie/mcp/mcp.json in the repo "
                "root. Restart the IDE/Junie CLI to pick it up. To make the "
                "proxy follow THIS project, write the project-scope file with "
                "--workspace <project folder>: it bakes an x-harness-workspace "
                "header with the literal path (Junie has no config "
                "placeholders). User-scope entries pin no project by design.",
    },
}


def _build_entry(spec: dict, url: str, workspace: str | None) -> dict:
    """Harness entry builder; specs with a workspace_header accept a pin."""
    fn = spec["build"]
    if workspace and spec.get("workspace_header"):
        return fn(url, workspace)
    return fn(url)


def emit_client_config(cfg: dict, agent: str = "claude",
                       workspace: str | None = None) -> tuple[str, str]:
    """Return (config_text, default_global_path) for the given harness."""
    spec = AGENTS[agent]
    url = _proxy_url(cfg)
    entry = _build_entry(spec, url, workspace)
    if spec["kind"] == "json":
        doc = {spec["key"]: {SERVER_NAME: entry}}
        if agent == "opencode":
            doc = {"$schema": "https://opencode.ai/config.json", **doc}
        return json.dumps(doc, indent=2), str(spec["default"])
    if spec["kind"] == "toml":
        lines = [f"[mcp_servers.{SERVER_NAME}]"]
        lines += [f'{k} = "{v}"' for k, v in entry.items()]
        return "\n".join(lines), str(spec["default"])
    # yaml fragment
    return (f"{spec['key']}:\n  {SERVER_NAME}:\n"
            + "\n".join(f"    {k}: {v}" for k, v in entry.items())), str(spec["default"])


_TOML_SECTION_RE = re.compile(rf"\[mcp_servers\.{SERVER_NAME}\][^\[]*", re.S)


def write_client_config(cfg: dict, out_path: str | os.PathLike,
                        agent: str = "claude", workspace: str | None = None) -> str:
    """Write/merge the harness entry into an existing config file.

    JSON files are merged key-by-key (other servers survive). TOML/YAML files
    get the bunny section appended, replacing only a previous bunny section.
    """
    spec = AGENTS[agent]
    text, _ = emit_client_config(cfg, agent)
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = p.read_text(encoding="utf-8") if p.exists() else ""
    url = _proxy_url(cfg)

    if spec["kind"] == "json":
        if existing.strip():
            try:
                doc = json.loads(existing)
            except json.JSONDecodeError as e:
                raise SystemExit(f"refusing to write {p}: existing file is not valid JSON ({e})")
            if not isinstance(doc, dict):
                raise SystemExit(f"refusing to write {p}: top level is not an object")
        else:
            doc = {"$schema": "https://opencode.ai/config.json"} if agent == "opencode" else {}
        section = doc.setdefault(spec["key"], {})
        if not isinstance(section, dict):
            raise SystemExit(f"refusing to write {p}: {spec['key']!r} is not an object")
        section[SERVER_NAME] = _build_entry(spec, url, workspace)
        p.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return str(p)

    if spec["kind"] == "toml":
        body = _TOML_SECTION_RE.sub("", existing) if existing else ""
        p.write_text(body.rstrip("\n") + ("\n\n" if body.strip() else "") + text + "\n",
                     encoding="utf-8")
        return str(p)

    # yaml: merge into mcp_servers, replacing only our entry
    frag = f"  {SERVER_NAME}:\n" + "\n".join(
        f"    {k}: {v}" for k, v in _build_entry(spec, url, workspace).items())
    if "mcp_servers:" not in existing:
        p.write_text(existing.rstrip("\n") + ("\n\n" if existing.strip() else "")
                     + spec["key"] + ":\n" + frag + "\n", encoding="utf-8")
    elif re.search(rf"^\s*{re.escape(SERVER_NAME)}:", existing, re.M):
        pat = rf"^(\s*){re.escape(SERVER_NAME)}:\n(?:^\s{{4,}}.*\n?)*"
        p.write_text(re.sub(pat, frag.replace("\\", "\\\\") + "\n", existing, count=1,
                            flags=re.M), encoding="utf-8")
    else:
        p.write_text(re.sub(r"^(mcp_servers:[^\n]*\n)", r"\1" + frag + "\n",
                            existing, count=1, flags=re.M), encoding="utf-8")
    return str(p)
