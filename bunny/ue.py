"""UE editor process management: detect, launch, build, kill, wait-for-ready."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

from .config import derive_target, project_name, render_template
from . import crosssession as CS
from .log import get_logger

log = get_logger("ue")


def _console_windows_minimize(pid: int) -> int:
    """Minimize every visible, non-iconic window owned by `pid` or its
    conhost.exe children (the console window is attributed to conhost, not to
    the console app). Returns how many windows were minimized.

    Why: CREATE_NEW_CONSOLE + SW_SHOWMINNOACTIVE opens the window minimized,
    but UnrealBuildTool/dotnet and conhost quirks can restore it mid-build,
    stealing focus from the harness window. Polling and re-minimizing keeps
    the promise 'opens minimized' for the window's whole life.
    """
    if os.name != "nt":
        return 0
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    SW_SHOWMINNOACTIVE = 7  # minimize without activating — keeps the harness focus

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wt.DWORD), ("szExeFile", wt.CHAR * 260)]

    targets = {pid}
    snap = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snap != -1:
        pe = PE()
        pe.dwSize = ctypes.sizeof(PE)
        if kernel32.Process32First(snap, ctypes.byref(pe)):
            while True:
                if pe.th32ParentProcessID == pid and pe.szExeFile.decode().lower() == "conhost.exe":
                    targets.add(pe.th32ProcessID)
                if not kernel32.Process32Next(snap, ctypes.byref(pe)):
                    break
        kernel32.CloseHandle(snap)

    done = 0

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lparam):
        nonlocal done
        owner = wt.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value in targets and user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_SHOWMINNOACTIVE)  # minimize WITHOUT activating
            done += 1
        return True

    user32.EnumWindows(cb, 0)
    return done


def _session_id(pid: int) -> int | None:
    """Windows session id of a process (0 = service/session-0 desktop)."""
    if os.name != "nt":
        return None
    import ctypes
    import ctypes.wintypes as wt

    k = ctypes.windll.kernel32
    sid = wt.DWORD(0)
    if k.ProcessIdToSessionId(wt.DWORD(pid), ctypes.byref(sid)):
        return int(sid.value)
    return None


def window_mode(cfg: dict) -> dict:
    """Window capability + configured policy, for status and error messages.

    A process in session 0 (running as a service / scheduled task) has no
    interactive desktop: whatever STARTUPINFO says, no window can appear for
    its children — so a GUI editor launched from a service stays invisible, and
    a RHI that needs a desktop (swap chain) fails there too (observed as exit
    code 3 / D3D12Util fatal). If the service runs as the user, the correct
    behaviour is a cross-session spawn into the active console session, which
    this module supports; the honest note tells the agent which case applies.
    """
    sid = _session_id(os.getpid())
    cross = None
    route = None
    note = ""
    if sid == 0:
        cross = CS.active_console_session()
        if cross:
            tok, info = CS.session_token(cross)
            if tok and CS.has_impersonate():
                route = info
                note = (f"bunny runs as a service in session 0, but your console session ({cross}) "
                        f"is reachable via the {route} token: children are spawned there, so the "
                        "editor window appears on your desktop. The minimize-guard can only watch "
                        "windows in its own session, so the build console is created minimized but "
                        "not re-guarded there.")
            else:
                why = ("no usable token for it (WTSQueryUserToken needs LocalSystem + SeTCB, and "
                       "no same-user process in that session to duplicate from)"
                       if not tok else "SeImpersonatePrivilege is not held")
                note = (f"bunny is a service in session 0; your console session ({cross}) exists "
                        f"but {why}, so children stay in session 0 and no window can be shown for "
                        "them. Run bunny as LocalSystem, or start it in an interactive session.")
        else:
            note = ("bunny is running as a service in session 0 and no user console session is "
                    "reachable, so no window can be shown for processes it spawns. Run bunny as "
                    "LocalSystem, or start it in an interactive session.")
    return {
        "process_session": sid,
        "cross_session_session": cross if route else None,
        "cross_session_route": route,
        "windows_possible": (bool(sid) and sid != 0) or bool(route),
        "launch_window": str(cfg.get("launch_window") or "normal"),
        "build_window": str(cfg.get("build_window") or "minimized"),
        "note": note,
    }


def _startupinfo(show_window: str):
    """STARTUPINFO forcing a console window state for the child.

    wShowWindow in STARTUPINFO is honoured for the console window Windows
    creates for a child console process. IMPORTANT: a child that supplies no
    startupinfo INHERITS the parent's — so if bunny was started by a shell that
    set STARTF_USESHOWWINDOW (bash/powershell background launches do), every
    child, including the GUI editor, was launched hidden. Always pass an explicit
    startupinfo so window behaviour never depends on how bunny itself was started.

    'normal' -> SW_SHOWNORMAL (window shown and activated), 'minimized' ->
    SW_SHOWMINNOACTIVE (shown minimized, focus stays with the harness),
    'hidden' -> SW_HIDE.
    """
    if os.name != "nt":
        return None
    SW_SHOWNORMAL, SW_HIDE, SW_SHOWMINNOACTIVE = 1, 0, 7
    code = {"normal": SW_SHOWNORMAL, "hidden": SW_HIDE,
            "minimized": SW_SHOWMINNOACTIVE}.get(show_window or "normal", SW_SHOWNORMAL)
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = code
    return si


def _popen(cmd: list[str], cwd: Path, log_path: Path, creation_flags: int = 0,
           keep_stdin_pipe: bool = False, show_window: str = "") -> subprocess.Popen:
    """Spawn detached from this process group; stdout/err -> file.

    creation_flags lets the caller pick console behaviour: DETACHED_PROCESS and
    CREATE_NEW_CONSOLE are mutually exclusive on Windows (using both fails with
    ERROR_INVALID_PARAMETER 87), so launch passes CREATE_NEW_CONSOLE alone.

    show_window: 'hidden' -> CREATE_NO_WINDOW (console exists, never shown, all
    grandchildren inherit it) + SW_HIDE; 'minimized' -> a minimized,
    non-activating console. Without this, DETACHED_PROCESS leaves a console app
    (UnrealBuildTool) with no console, and Windows allocates a VISIBLE one that
    steals focus from the harness window.

    keep_stdin_pipe: UnrealEditor-Cmd.exe reads stdin as console commands and
    EXITS when stdin is EOF — stdin=DEVNULL kills it within a minute. Give it a
    pipe whose write end the parent holds open for the child's lifetime.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logfile = open(log_path, "ab", buffering=0)
    stdin = subprocess.DEVNULL
    keep_open = None
    if keep_stdin_pipe:
        r, w = os.pipe()  # CRT pipe fds are inheritable; child receives r as stdin fd 0
        stdin = r
        keep_open = w  # parent keeps the write end open for the child's lifetime (no EOF)
    kwargs = dict(cwd=str(cwd), stdout=logfile, stderr=subprocess.STDOUT, stdin=stdin)
    if os.name == "nt":
        if show_window == "hidden":
            # console allocated but invisible: UBT + any grandchild cmd.exe
            # inherit it, so nothing flashes and nothing takes focus
            base = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | subprocess.CREATE_NEW_PROCESS_GROUP
        elif show_window == "minimized":
            base = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        elif creation_flags & getattr(subprocess, "CREATE_NEW_CONSOLE", 0):
            base = creation_flags  # caller chose the console mode explicitly
        else:
            base = creation_flags | subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        kwargs["creationflags"] = base
        kwargs["close_fds"] = True
        si = _startupinfo(show_window or "normal")  # always explicit: never inherit
        kwargs["startupinfo"] = si
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    logfile.close()  # child inherited its own handle
    if keep_stdin_pipe:
        os.close(r)  # parent keeps only the write end open
        proc._bunny_stdin_write = keep_open
    return proc


def extract_uproject(cmdline: str) -> str | None:
    """Pull the .uproject path out of an editor command line."""
    for tok in cmdline.split('"'):
        if tok.lower().endswith(".uproject"):
            return tok
    import re
    # unquoted path: exclude whitespace/quotes so the leading exe path in
    # the same command line cannot be swallowed into the match
    m = re.search(r'([A-Za-z]:[^\s"]*\.uproject)', cmdline)
    return m.group(1) if m else None


class UEBridge:
    def __init__(self, cfg: dict, upstream_url: str):
        self.cfg = cfg
        self.upstream_url = upstream_url
        root = Path(cfg["_root"])
        self.launch_log = root / "logs" / "editor_launch.log"
        self.build_log = root / "logs" / "build.log"
        self._launch_proc = None
        self._launch_cmdline = ""
        self._last_launch_ts = 0.0
        self._build_proc = None
        self._build_started = 0.0
        self._build_done_ts = 0.0
        self._build_failures = 0
        self._build_guard = None

    # ---------- config helpers ----------
    def project_path(self) -> str:
        return self.cfg.get("project_path") or ""

    def _gui_sibling(self, exe: str) -> str:
        """UnrealEditor-Cmd.exe -> sibling UnrealEditor.exe when it exists."""
        try:
            p = Path(exe)
            name = p.name.lower()
            if "-cmd" not in name:
                return ""
            gui = p.parent / "UnrealEditor.exe"
            return str(gui) if gui.is_file() else ""
        except (OSError, ValueError):
            return ""

    def editor_binary(self) -> str:
        return self.cfg.get("editor_binary") or ""

    def can_launch(self) -> bool:
        return bool(self.project_path()) and bool(self.editor_binary())

    def can_build(self) -> bool:
        return (
            bool(self.project_path())
            and bool(derive_target(self.cfg))
            and Path(derive_bin(self.cfg)).is_file()
        )

    # ---------- probes ----------
    def running_project(self) -> dict | None:
        """Detect the .uproject of the running editor from its command line (Windows)."""
        # Fast, race-free path: an editor the proxy itself launched — its argv
        # carries the .uproject; WMI may not list the process yet at boot.
        if self._launch_proc and self._launch_proc.poll() is None and self._launch_cmdline:
            up = extract_uproject(self._launch_cmdline)
            if up:
                return {"uproject": up, "cmdline": self._launch_cmdline}
        if sys.platform != "win32":
            return None
        try:
            ps = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='UnrealEditor.exe' or Name='UnrealEditor-Cmd.exe'\" "
                 "| ForEach-Object { [pscustomobject]@{Exe=$_.ExecutablePath; Cmd=$_.CommandLine; "
                 "Pid=$_.ProcessId} } | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=25,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            procs = json.loads(ps.stdout.strip() or "[]")
            if isinstance(procs, dict):
                procs = [procs]
        except Exception as e:  # pragma: no cover
            log.debug("running_project scan failed: %s", e)
            return None
        for pr in procs:
            line = (pr.get("Cmd") or "").strip()
            exe = (pr.get("Exe") or "").strip()
            if not line and not exe:
                continue
            # tokenise roughly: find the .uproject token
            uproject = None
            for tok in line.split('"'):
                if tok.lower().endswith(".uproject"):
                    uproject = tok
                    break
            if uproject is None:
                # unquoted path: exclude whitespace/quotes so the leading exe path in
                # the same command line cannot be swallowed into the match
                import re
                m = re.search(r'([A-Za-z]:[^\s"]*\.uproject)', line)
                if m:
                    uproject = m.group(1)
            if uproject:
                return {"uproject": uproject, "cmdline": line, "exe": exe,
                        "pid": pr.get("Pid")}
        return None

    async def upstream_probe(self) -> dict:
        """Is UE's MCP server reachable? Quick GET /mcp -> 405 means alive."""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(3.0, connect=1.5)) as c:
                r = await c.get(self.upstream_url)
            alive = r.status_code < 500
            return {"alive": alive, "status": r.status_code,
                    "note": "" if alive else f"HTTP {r.status_code} from upstream"}
        except httpx.HTTPError as e:
            return {"alive": False, "status": None,
                    "note": f"{type(e).__name__}: {e}"}

    def _editor_procs(self) -> list[dict]:
        """List running processes matching the configured editor binary (Windows)."""
        ed = self.editor_binary()
        if not ed:
            return []
        exe = Path(ed).name.lower()
        out = []
        if sys.platform == "win32":
            try:
                # tasklist CSV quotes every field: '"UnrealEditor-Cmd.exe","2400","Console","1","364,200 K"'
                # (note the thousands-separator comma inside the last quoted field —
                # only fields 0/1 are trusted after unquoting)
                ps = subprocess.run(
                    ["tasklist", "/FI", f"IMAGENAME eq {Path(ed).name}", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                for line in ps.stdout.splitlines():
                    parts = [p.strip().strip('"') for p in line.split(",")]
                    if len(parts) >= 2 and parts[0].lower() == exe:
                        try:
                            out.append({"name": parts[0], "pid": int(parts[1])})
                        except ValueError:
                            continue
            except Exception as e:  # pragma: no cover
                log.warning("proc scan failed: %s", e)
        else:
            try:
                ps = subprocess.run(["pgrep", "-fa", Path(ed).name], capture_output=True, text=True, timeout=15)
                for line in ps.stdout.splitlines():
                    fields = line.strip().split(None, 1)
                    if fields:
                        out.append({"name": Path(ed).name, "pid": int(fields[0])})
            except Exception as e:  # pragma: no cover
                log.warning("proc scan failed: %s", e)
        return out

    async def status(self) -> dict:
        proc_alive = bool(self._launch_proc and self._launch_proc.poll() is None)
        procs = self._editor_procs()
        probe = await self.upstream_probe()
        return {
            "upstream_url": self.upstream_url,
            "upstream_alive": probe["alive"],
            "upstream_note": probe["note"],
            "editor_process_running": proc_alive or bool(procs),
            "editor_pids": [p["pid"] for p in procs],
            "launched_by_proxy": proc_alive,
            "can_launch": self.can_launch(),
            "can_build": self.can_build(),
            "window": window_mode(self.cfg),
        }

    def _spawn(self, cmd: list[str], cwd: Path, log_path: Path, show_window: str,
              want_console: bool, keep_stdin_pipe: bool = False):
        """Spawn a child, preferring the user's interactive session.

        Returns (proc, spawn_path, cross_reason). A service in session 0 has no
        desktop, so cross-session spawn is what makes the editor window appear
        (and what keeps the RHI from failing on a windowless window station);
        same-session Popen is the fallback when no user session is reachable.
        """
        si = CS.build_startupinfo(show_window, desktop="Winsta0\\Default")
        # env=None: cross_session_spawn prefers the token user's environment
        # block (a LocalSystem service must not hand SYSTEM's env to the editor).
        proc, why = CS.cross_session_spawn(cmd, cwd, si, env=None,
                                          creation_flags=CS.cross_session_flags(want_console, show_window))
        if proc:
            log.info("spawned in user session %s (window=%s): %s",
                     proc.token_session, show_window, " ".join(cmd))
            return proc, "cross_session", ""
        log.info("cross-session spawn unavailable (%s); same-session fallback", why)
        proc = _popen(cmd, cwd, log_path, show_window=show_window,
                      keep_stdin_pipe=keep_stdin_pipe)
        return proc, "same_session", why

    # ---------- launch ----------
    async def launch(self, wait_ready: bool = True, timeout_s: float | None = None) -> dict:
        timeout_s = timeout_s or self.cfg.get("launch_ready_timeout_s", 240)
        st = await self.status()
        if st["upstream_alive"]:
            return {"ok": True, "status": "already_running",
                    "message": f"UE MCP server already reachable at {self.upstream_url}"}
        if not self.can_launch():
            return {
                "ok": False,
                "status": "not_configured",
                "message": "project_path and editor_binary must be set in config.json (proxy__ue_configure). "
                           f"project configured: {bool(self.project_path())}, editor configured: {bool(self.editor_binary())}",
            }
        if st["editor_process_running"] and not st["upstream_alive"]:
            return {
                "ok": False,
                "status": "running_no_mcp",
                "message": "An editor process is running but its MCP server is not reachable at "
                           f"{self.upstream_url}. Run console command ModelContextProtocol.StartServer inside it "
                           "(or enable 'Auto Start Server' in Project Settings), then retry.",
            }
        # debounce duplicate launches
        if time.time() - self._last_launch_ts < 10 and self._launch_proc and self._launch_proc.poll() is None:
            return {"ok": False, "status": "launching", "message": "a launch is already in progress"}

        argv = render_template(self.cfg["launch_template"], self.cfg)
        argv = [a for a in argv if a]
        cwd = Path(self.cfg["project_path"]).parent
        # RULE: never launch UnrealEditor-Cmd.exe for a session that must stay
        # open. -Cmd is for command/test runs that close the engine afterwards.
        # Long-lived editor = GUI UnrealEditor.exe only.
        if "-cmd" in Path(self.editor_binary()).name.lower():
            gui = self._gui_sibling(self.editor_binary())
            if gui:
                log.info("refusing UnrealEditor-Cmd for launch; using GUI editor %s", gui)
                self.cfg["editor_binary"] = gui
                argv = render_template(self.cfg["launch_template"], self.cfg)
                argv = [a for a in argv if a]
            else:
                return {"ok": False, "status": "cmd_editor_not_allowed",
                        "message": "editor_binary points at UnrealEditor-Cmd.exe, which is for "
                                   "command/test runs that close the engine afterwards and must "
                                   "not be used for a persistent editor session. Set editor_binary "
                                   "to UnrealEditor.exe (proxy__ue_configure) and retry."}
        # The GUI editor ignores stdin: DEVNULL means a proxy restart (which
        # closes our pipe write-end) can never EOF-kill the editor.
        is_cmd = False
        wm = window_mode(self.cfg)
        show = wm["launch_window"]
        # A service in session 0 has no interactive desktop: the GUI editor
        # launched there dies in D3D12 RHI (observed: exit 3, D3D12Util.cpp:1062
        # fatal after ~30 min of headless init). Only a cross-session spawn into
        # the user's console session can run it, so refuse early with the fix
        # instructions instead of burning launch_ready_timeout_s on a doomed
        # headless init and crashing.
        if wm["process_session"] == 0 and not wm["cross_session_route"]:
            log.info("refusing editor launch: session 0 without a user-session token route")
            return {
                "ok": False,
                "status": "service_session_no_desktop",
                "message": (
                    "bunny is running as a Windows service in session 0, which has no "
                    "interactive desktop: UnrealEditor.exe started there dies with a D3D12 "
                    "RHI fatal (exit 3), so the proxy did not launch it. Fix: run "
                    "install_service.bat as admin (moves the service to LocalSystem, which "
                    "can spawn the editor into your desktop session), or run bunny "
                    "interactively via bunny.bat."),
                "window": wm,
            }
        log.info("launching editor: %s (cwd=%s, window=%s, session=%s)", " ".join(argv), cwd,
                 show, wm["process_session"])
        try:
            proc, path, why = self._spawn(argv, cwd, self.launch_log, show,
                                          want_console=False, keep_stdin_pipe=is_cmd)
        except OSError as e:
            return {"ok": False, "status": "launch_failed", "message": f"could not start editor: {e}"}
        self._launch_proc = proc
        self._launch_cmdline = " ".join(argv)
        self._last_launch_ts = time.time()
        out = {"pid": proc.pid, "spawn": path}
        if path == "same_session" and why:
            out["cross_session_note"] = why

        if not wait_ready:
            out.update({"ok": True, "status": "launching",
                        "message": f"editor spawned (pid {proc.pid}); poll proxy__ue_status",
                        "window": wm})
            if wm["note"]:
                out["window_note"] = wm["note"]
            return out
        ready = await self.wait_ready(timeout_s)
        ready["pid"] = proc.pid
        ready["spawn"] = path
        ready["window"] = wm
        if path == "same_session" and why:
            ready["cross_session_note"] = why
        if wm["note"]:
            ready["window_note"] = wm["note"]
        return ready

    def _project_ue_log(self) -> str | None:
        """Newest UE-written log for the active project (Saved/Logs/*.log).

        A GUI editor writes its crash/fatal lines to its own project log, not to
        the stdout the proxy captured, so the proxy's launch log is often empty
        when the editor exits early. Both paths must be reported.
        """
        proj = self.project_path()
        if not proj:
            return None
        logs = Path(proj).parent / "Saved" / "Logs"
        if not logs.is_dir():
            return None
        files = [f for f in logs.glob("*.log") if f.is_file()]
        if not files:
            return None
        return str(max(files, key=lambda f: f.stat().st_mtime))

    @staticmethod
    def _tail_errors(path: str, limit: int = 6) -> list[str]:
        try:
            txt = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return []
        hits = [l for l in txt
                if any(k in l.lower() for k in ("fatal", "error:", "crash", "assert",
                                                 "exception", "unable to lock", "exit code"))]
        return hits[-limit:]

    async def wait_ready(self, timeout_s: float | None = None) -> dict:
        timeout_s = timeout_s or self.cfg.get("launch_ready_timeout_s", 240)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            probe = await self.upstream_probe()
            if probe["alive"]:
                return {"ok": True, "status": "ready",
                        "message": f"UE MCP server reachable at {self.upstream_url}"}
            if self._launch_proc and self._launch_proc.poll() is not None:
                rc = self._launch_proc.returncode
                return {"ok": False, "status": "editor_exited",
                        "exit_code": rc,
                        "logs": {"proxy_launch_log": str(self.launch_log),
                                 "project_ue_log": self._project_ue_log()},
                        "log_errors": self._tail_errors(self._project_ue_log() or "")
                                      or self._tail_errors(str(self.launch_log)),
                        "message": (f"editor process exited (code {rc}) before MCP became reachable. "
                                    f"Check the absolute paths in 'logs' (project_ue_log is UE's own log "
                                    f"and holds the crash lines; proxy_launch_log is only captured stdout) "
                                    f"— key lines quoted in 'log_errors'.")}
            await asyncio.sleep(3)
        return {"ok": False, "status": "timeout",
                "logs": {"proxy_launch_log": str(self.launch_log),
                         "project_ue_log": self._project_ue_log()},
                "message": f"UE MCP not reachable after {timeout_s}s; check the absolute log paths in "
                           "'logs' (UE's own log under Saved/Logs holds the crash lines). If the editor "
                           "is running, run ModelContextProtocol.StartServer inside it "
                           "(or enable 'Auto Start Server' in Project Settings)."}

    # ---------- build ----------
    async def _minimize_guard(self, pid: int) -> None:
        """Keep the build console minimized for its whole life.

        Runs alongside a build: re-minimizes the console window whenever
        UnrealBuildTool / conhost restores it, so the harness window keeps focus.
        """
        try:
            while True:
                if self._build_proc is None or self._build_proc.poll() is not None:
                    return
                await asyncio.to_thread(_console_windows_minimize, pid)
                await asyncio.sleep(1.2)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never let the guard break a build
            log.debug("minimize guard stopped: %s", e)

    async def build(self, blocking: bool = False) -> dict:
        if self._build_proc and self._build_proc.poll() is None:
            return {"ok": False, "status": "building", "message": "a build is already running"}
        if not self.can_build():
            from .config import discover_targets, pick_target
            avail = discover_targets(self.project_path())
            return {"ok": False, "status": "not_configured",
                    "available_targets": avail,
                    "selected_target": pick_target(self.cfg, self.project_path())[0] if avail else "",
                    "message": "build needs project_path, a derivable build target (set build_target) and "
                               "Build.bat under engine_root — check proxy__ue_status can_build. "
                               + (f"Targets declared by this project: "
                                  f"{', '.join(t['name'] + ' [' + t['type'] + ']' for t in avail)} "
                                  f"— pass one as build_target (proxy__ue_configure)."
                                  if avail else "This project declares no Source/*.Target.cs at all.")}
        argv = [a for a in render_template(self.cfg["build_template"], self.cfg) if a]
        build_bat = Path(argv[0])
        wm = window_mode(self.cfg)
        show = wm["build_window"]
        log.info("starting build: %s (window=%s, target=%s, session=%s)", " ".join(argv), show,
                 render_template("{target}", self.cfg)[0] if argv else "?", wm["process_session"])
        try:
            proc, path, why = self._spawn(argv, build_bat.parent, self.build_log, show,
                                          want_console=True)
        except OSError as e:
            return {"ok": False, "status": "build_failed", "message": f"could not start build: {e}"}
        self._build_proc = proc
        self._build_started = time.time()
        base = {"pid": proc.pid, "log": str(self.build_log), "argv": argv, "spawn": path}
        if show == "minimized" and os.name == "nt":
            if path == "cross_session":
                # created minimized on the user's desktop; the guard cannot see
                # windows in another session, so it is skipped here
                base["window_note"] = ("build console created minimized in the user session "
                                       f"({proc.token_session}); the minimize-guard only runs for "
                                       "same-session children")
            else:
                if self._build_guard and not self._build_guard.done():
                    self._build_guard.cancel()
                self._build_guard = asyncio.create_task(self._minimize_guard(proc.pid))
        elif path == "same_session" and why:
            base["cross_session_note"] = why
        if not blocking:
            return {"ok": True, "status": "started", "message": "build started", **base}
        # bounded blocking wait
        limit = self.cfg.get("build_timeout_s", 1800)
        loop = asyncio.get_running_loop()
        try:
            rc = await asyncio.wait_for(loop.run_in_executor(None, self._build_proc.wait), timeout=limit)
        except asyncio.TimeoutError:
            return {"ok": False, "status": "build_timeout", **base,
                    "message": f"build exceeded {limit}s; still running (log: {self.build_log})"}
        dur = round(time.time() - self._build_started, 1)
        if rc == 0:
            self._build_done_ts = time.time()
            self._build_failures = 0
            return {"ok": True, "status": "build_ok", "seconds": dur, "message": f"build succeeded in {dur}s", **base}
        self._build_failures += 1
        return {"ok": False, "status": "build_failed", "seconds": dur, "exit_code": rc,
                "message": f"build FAILED (exit {rc}) after {dur}s — inspect {self.build_log}", **base}

    def build_state(self) -> dict:
        if self._build_proc is None:
            return {"running": False, "last_result": None}
        running = self._build_proc.poll() is None
        return {
            "running": running,
            "seconds_elapsed": round(time.time() - self._build_started, 1) if running else None,
            "exit_code": None if running else self._build_proc.returncode,
            "log": str(self.build_log),
        }

    # ---------- plugins ----------
    def find_engine_plugin(self, plugin_name: str) -> str | None:
        """Locate <plugin_name>.uplugin under the engine's Plugins tree (Toolsets first)."""
        from .config import derive_engine_root

        root = derive_engine_root(self.cfg)
        if not root:
            return None
        plug_root = Path(root) / "Engine" / "Plugins"
        toolsets = plug_root / "Experimental" / "Toolsets"
        hits = list(toolsets.glob(f"*/{plugin_name}.uplugin")) if toolsets.is_dir() else []
        if not hits:
            hits = list(plug_root.rglob(f"{plugin_name}.uplugin"))
        return str(hits[0]) if hits else None

    def enable_plugin(self, plugin_name: str) -> dict:
        """Enable a plugin (e.g. LiveCodingToolset) in the active project's .uproject.

        Backs up the file first. The plugin's module must be compiled afterwards:
        proxy__ue_build (UE closed) does that and auto-relaunches the editor.
        """
        proj = self.project_path()
        if not proj:
            return {"ok": False, "status": "not_configured",
                    "message": "no active project — set project_path (proxy__ue_configure) or let "
                               "adoption pick up the running editor (proxy__ue_adopt_project)"}
        p = Path(proj)
        if not p.is_file():
            return {"ok": False, "status": "project_missing", "message": f"{p} not found"}
        uplugin = self.find_engine_plugin(plugin_name)
        if not uplugin:
            return {"ok": False, "status": "plugin_not_found",
                    "message": f"{plugin_name}.uplugin not found under the engine Plugins tree — "
                               "check the exact plugin name (ue_list_toolsets shows enabled ones)"}
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as e:
            return {"ok": False, "status": "uproject_unreadable", "message": f"{p}: {e}"}
        plugins = data.setdefault("Plugins", [])
        status = "added"
        for e in plugins:
            if e.get("Name") == plugin_name:
                if e.get("Enabled") is True:
                    return {"ok": True, "status": "already_enabled", "plugin": plugin_name,
                            "uplugin": uplugin,
                            "message": f"{plugin_name} is already enabled in {p.name}; if its tools "
                                       "are missing, run proxy__ue_build so its module compiles"}
                e["Enabled"] = True
                status = "re-enabled"
                break
        else:
            plugins.append({"Name": plugin_name, "Enabled": True})
        backup = p.with_suffix(".uproject.bak")
        backup.write_text(p.read_text(encoding="utf-8-sig"), encoding="utf-8")
        p.write_text(json.dumps(data, indent="\t", ensure_ascii=False) + "\n", encoding="utf-8")
        log.info("enabled plugin %s in %s (backup: %s)", plugin_name, p, backup.name)
        return {"ok": True, "status": status, "plugin": plugin_name, "uproject": str(p),
                "backup": str(backup), "uplugin": uplugin,
                "next": "run proxy__ue_build with the editor closed — it compiles the plugin and "
                        "auto-relaunches UE; then ue_list_toolsets will include its toolset"}

    # ---------- toolset sync ----------
    TOOLSETS_REL = "Engine/Plugins/Experimental/Toolsets"

    def _toolset_index(self) -> tuple[dict, set]:
        """Engine toolset inventory: {name: {enabled_by_default}} + AllToolsets manifest names."""
        from .config import derive_engine_root

        root = derive_engine_root(self.cfg)
        if not root:
            return {}, set()
        toolsets = Path(root) / self.TOOLSETS_REL
        index: dict[str, dict] = {}
        if toolsets.is_dir():
            for up in toolsets.glob("*/*.uplugin"):
                try:
                    data = json.loads(up.read_text(encoding="utf-8-sig"))
                except (OSError, json.JSONDecodeError):
                    continue
                index[up.stem] = {"enabled_by_default": bool(data.get("EnabledByDefault", False))}
        manifest: set[str] = set()
        allts = toolsets / "AllToolsets" / "AllToolsets.uplugin"
        if allts.is_file():
            try:
                data = json.loads(allts.read_text(encoding="utf-8-sig"))
                manifest = {e.get("Name") for e in data.get("Plugins", []) if e.get("Name")}
            except (OSError, json.JSONDecodeError):
                pass
        return index, manifest

    def sync_toolsets(self, dry_run: bool = False) -> dict:
        """Keep the project's toolset plugin list lean and engine-proof.

        AllToolsets' manifest (Epic-maintained per engine version) is the enable-all
        switch, so the project file stores ONLY gap plugins — toolsets AllToolsets
        does not cover (e.g. LiveCodingToolset). This syncs gap->project:
          add     gap plugins missing/disabled in the project (new toolsets this
                  engine version ships that AllToolsets forgot)
          prune   project toolset entries that are redundant (AllToolsets covers
                  them / EnabledByDefault) or MISSING from the engine (upstream
                  renamed/removed -> 'missing plugin' friction on editor start)
        Explicit Enabled:false project entries are user opt-outs: respected, never
        flipped, never pruned. .uproject is backed up before writing.
        """
        proj = self.project_path()
        if not proj:
            return {"ok": False, "status": "not_configured",
                    "message": "no active project — configure project_path (proxy__ue_configure) "
                               "or adopt the running editor (proxy__ue_adopt_project)"}
        p = Path(proj)
        if not p.is_file():
            return {"ok": False, "status": "project_missing", "message": f"{p} not found"}
        index, manifest = self._toolset_index()
        if not index:
            return {"ok": False, "status": "engine_scan_failed",
                    "message": f"no toolset plugins found under {self.TOOLSETS_REL} of the engine "
                               "root — check engine_root (proxy__ue_configure action=scan)"}
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as e:
            return {"ok": False, "status": "uproject_unreadable", "message": f"{p}: {e}"}
        plugins = data.setdefault("Plugins", [])
        by_name = {e.get("Name"): e for e in plugins if e.get("Name")}

        gaps = sorted(n for n, info in index.items()
                      if n not in manifest and not info["enabled_by_default"] and n != "AllToolsets")
        added, respected_optouts = [], []
        for name in gaps:
            e = by_name.get(name)
            if e is None:
                plugins.append({"Name": name, "Enabled": True})
                added.append(name)
            elif e.get("Enabled") is not True:
                if e.get("Enabled") is False:
                    respected_optouts.append(name)  # user disabled it deliberately
                else:
                    e["Enabled"] = True
                    added.append(name)

        pruned = []
        universe = set(index) | manifest
        proj_plugins_dir = p.parent / "Plugins"
        for e in list(plugins):
            n = e.get("Name")
            if n == "AllToolsets" or e.get("Enabled") is False:
                continue
            if n not in universe:
                # Future-proofing: toolset-named entries that vanished from this engine
                # version (renamed/removed upstream) -> 'missing plugin' friction.
                # Guarded so marketplace/vendored project plugins are never touched.
                if "toolset" in n.lower() and not self.find_engine_plugin(n) \
                        and not list(proj_plugins_dir.rglob(f"{n}.uplugin")):
                    plugins.remove(e)
                    pruned.append(n)
                continue
            info = index.get(n)
            if (n in manifest) or (info and info["enabled_by_default"]) or info is None:
                plugins.remove(e)
                pruned.append(n)

        report = {"ok": True, "status": "dry_run" if dry_run else "synced",
                  "engine_toolsets": len(index), "alltoolsets_manifest": len(manifest),
                  "gap_plugins": gaps, "added": added, "pruned": pruned,
                  "respected_optouts": respected_optouts}
        if dry_run:
            return report
        if added or pruned:
            backup = p.with_suffix(".uproject.bak")
            backup.write_text(p.read_text(encoding="utf-8-sig"), encoding="utf-8")
            p.write_text(json.dumps(data, indent="\t", ensure_ascii=False) + "\n", encoding="utf-8")
            report["backup"] = str(backup)
            report["next"] = ("run proxy__ue_build with the editor closed — it compiles the newly "
                              "added plugin modules and auto-relaunches UE")
        report["status"] = "already_in_sync" if not added and not pruned else report["status"]
        log.info("toolset sync: +%s -%s (engine toolsets=%d)", added, pruned, len(index))
        return report

    # ---------- kill ----------
    async def kill(self, confirm: bool = False) -> dict:
        if not confirm:
            return {"ok": False, "status": "needs_confirmation",
                    "message": "pass confirmed=true; this terminates the running editor WITHOUT saving"}
        targets = []
        if self._launch_proc and self._launch_proc.poll() is None:
            targets.append(self._launch_proc.pid)
        targets += [p["pid"] for p in self._editor_procs() if p["pid"] not in targets]
        if not targets:
            return {"ok": True, "status": "not_running", "message": "no editor process found"}
        killed = []
        for pid in targets:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                   capture_output=True, text=True, timeout=30,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                else:
                    subprocess.run(["kill", str(pid)], capture_output=True, text=True, timeout=30)
                killed.append(pid)
            except Exception as e:
                log.warning("kill %s failed: %s", pid, e)
        return {"ok": True, "status": "killed", "message": f"terminated editor pid(s) {killed}", "pids": killed}


def derive_bin(cfg: dict) -> str:
    from .config import derive_engine_root

    root = derive_engine_root(cfg)
    if not root:
        return ""
    return str(Path(root) / "Engine" / "Build" / "BatchFiles" / "Build.bat")
