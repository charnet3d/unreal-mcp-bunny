"""Cross-session tests (no host state, no real OS calls): a service in session 0
must reach the user's console session and spawn there with the correct window
policy; the same-session Popen remains the fallback when no user session exists.

Covers the service setup: service (LocalSystem) -> token for the active console
session (WTS route, or duplicate from a same-user process in that session) ->
CreateProcessWithTokenW into the interactive desktop.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import os  # noqa: E402
import subprocess  # noqa: E402
import ctypes  # noqa: E402
import asyncio  # noqa: E402
from bunny import crosssession as CS  # noqa: E402
from bunny import ue as UE  # noqa: E402
from bunny.config import default_config  # noqa: E402

passed = total = 0


def check(name, cond, detail=""):
    global passed, total
    total += 1
    print(("PASS  " if cond else "FAIL  ") + name, detail if not cond else "")
    passed += bool(cond)


tmp = Path(tempfile.mkdtemp(prefix="bunny_xs_"))
saved = {}


def save(mod, name):
    saved[(mod, name)] = getattr(mod, name)
    return saved[(mod, name)]


def restore():
    for (mod, name), v in saved.items():
        setattr(mod, name, v)


# ---- window policy: explicit STARTUPINFOW, never inherited ----
si = CS.build_startupinfo("minimized", desktop="Winsta0\\Default")
check("startupinfo shows minimized without activating", si.wShowWindow == 7, si.wShowWindow)
check("startupinfo targets the user desktop (window station)", si.lpDesktop == "Winsta0\\Default", si.lpDesktop)
check("window flag always set", bool(si.dwFlags & CS.STARTF_USESHOWWINDOW), si.dwFlags)
check("normal = show+activate", CS.build_startupinfo("normal").wShowWindow == 1)
check("hidden = SW_HIDE", CS.build_startupinfo("hidden").wShowWindow == 0)

# ---- create flags: GUI gets no console, build console only when wanted ----
gui = CS.cross_session_flags(want_console=False, show_window="normal")
check("GUI launch gets no console (no extra window)", not gui & CS.CREATE_NEW_CONSOLE, hex(gui))
buildmin = CS.cross_session_flags(want_console=True, show_window="minimized")
check("build console is created (minimized by startupinfo)",
      buildmin & CS.CREATE_NEW_CONSOLE and buildmin & CS.CREATE_UNICODE_ENVIRONMENT, hex(buildmin))
buildhid = CS.cross_session_flags(want_console=True, show_window="hidden")
check("build hidden = no console allocated", buildhid & 0x08000000 and not buildhid & CS.CREATE_NEW_CONSOLE, hex(buildhid))

# ---- same-session case declines (fallback path) ----
o1, o2, o3 = save(CS, "own_session"), save(CS, "active_console_session"), save(CS, "user_token")
CS.own_session = lambda pid=None: 0
CS.active_console_session = lambda: 0
r, why = CS.cross_session_spawn(["cmd", "/c", "x"], tmp, CS.build_startupinfo("normal"))
check("same session declines (no foreign session)", r is None and "no foreign session" in why, why)
CS.active_console_session = lambda: None
r, why = CS.cross_session_spawn(["cmd", "/c", "x"], tmp, CS.build_startupinfo("normal"))
check("no console session declines", r is None and "no foreign session" in why, why)

# ---- service-as-user: token borrowed from the user's session ----
calls = {}
class FakeFunc:
    """Callable that still accepts restype/argtypes assignment (as ctypes does)."""
    def __init__(self, impl):
        self._impl = impl

    def __call__(self, *args):
        return self._impl(*args)


class FakeAdvapi:
    """Just enough of advapi32 for the token path; argtypes assignments must work."""

    def __init__(self):
        self.CreateProcessWithTokenW = FakeFunc(self._impl)
        self.CreateProcessAsUserW = FakeFunc(self._impl_asuser)
        self.OpenProcessToken = FakeFunc(lambda *a: True)
        self.DuplicateTokenEx = FakeFunc(lambda *a: True)
        self.GetTokenInformation = FakeFunc(lambda *a: True)

    def _norm(self, token, cmdline):
        s = cmdline.value if isinstance(cmdline, ctypes.c_wchar_p) else str(cmdline)
        t = token.value if isinstance(token, ctypes.c_void_p) else token
        return s, t

    def _impl(self, token, flags, app, cmdline, cg, env, cwd, sip, pi):
        # the fake is not a real function object: argtypes do not convert, so
        # normalise the ctypes instances the caller passed
        s, t = self._norm(token, cmdline)
        calls.update(token=t, flags=cg, cmdline=s, cwd=str(cwd), env=env, fn="WithTokenW")
        # cross_session_spawn passes byref(pi): cast back to the struct to fill it
        ctypes.cast(pi, ctypes.POINTER(CS.PROCESS_INFORMATION)).dwProcessId = 4242
        return True

    def _impl_asuser(self, token, app, cmdline, pa, ta, inherit, cg, env, cwd, sip, pi):
        s, t = self._norm(token, cmdline)
        calls.update(token=t, flags=cg, cmdline=s, cwd=str(cwd), env=env, fn="AsUserW")
        ctypes.cast(pi, ctypes.POINTER(CS.PROCESS_INFORMATION)).dwProcessId = 4242
        return True

o4 = save(ctypes.windll, "advapi32")
orig_wn = getattr(ctypes.windll, "advapi32", None)
ctypes.windll.advapi32 = FakeAdvapi()
# env block: the token user's environment must be requested and destroyed
fake_env = ctypes.c_void_p(0x1234)
o10, o11 = save(CS, "user_env_block"), save(CS, "destroy_env_block")
CS.user_env_block = lambda tok: fake_env
CS.destroy_env_block = lambda b: calls.update(env_destroyed=b)
CS.own_session = lambda pid=None: 0
CS.active_console_session = lambda: 1
CS.has_impersonate = lambda: True
CS.user_token = lambda s: 9999
p, why = CS.cross_session_spawn(["UnrealEditor.exe", "P.uproject"], tmp,
                                CS.build_startupinfo("normal", desktop="Winsta0\\Default"))
check("WTS route preferred when available", p is not None and calls.get("token") == 9999
      and p.token_source == "WTSQueryUserToken", why)
check("spawned process bound to the user session", p and p.token_session == 1, p)
check("GUI child spawned without a console", p and not calls.get("flags", 0) & CS.CREATE_NEW_CONSOLE, hex(calls.get("flags", 0)))
check("command line and cwd preserved", calls.get("cmdline", "").endswith("P.uproject") and calls.get("cwd") == str(tmp), calls)
check("cross-session spawn uses CreateProcessAsUserW (token-session placement)",
      calls.get("fn") == "AsUserW", calls.get("fn"))
check("token user's env block passed to the spawn call",
      calls.get("env") is fake_env, calls.get("env"))
check("env block destroyed after spawn", calls.get("env_destroyed") is fake_env, calls)
CS.user_env_block = lambda tok: None  # CreateEnvironmentBlock failure -> own env
del calls["env_destroyed"]
CS.token_cache_clear()
p, why = CS.cross_session_spawn(["x"], tmp, CS.build_startupinfo("normal"))
check("env fallback: own env block used, never destroyed via userenv",
      calls.get("env") is not fake_env and "env_destroyed" not in calls, calls)

# WTSQueryUserToken needs LocalSystem + SeTCB (documented), so a service running
# as the user normally cannot use it: the duplicate-token route must take over.
CS.token_cache_clear()
CS.user_token = lambda s: None
CS.token_from_session_process = lambda s: 8888
p, why = CS.cross_session_spawn(["x"], tmp, CS.build_startupinfo("normal"))
check("duplicate-token fallback when WTS is denied",
      p is not None and calls.get("token") == 8888 and p.token_source == "duplicate-token", why)
CS.token_cache_clear()
CS.token_from_session_process = lambda s: None
r, why = CS.cross_session_spawn(["x"], tmp, CS.build_startupinfo("normal"))
check("no token names both routes", r is None and "SeTCB" in why and "duplicate" in why, why)
# CreateProcessWithTokenW itself requires SeImpersonate - honest gate up front
CS.has_impersonate = lambda: False
r, why = CS.cross_session_spawn(["x"], tmp, CS.build_startupinfo("normal"))
check("missing SeImpersonate reported as the root cause",
      r is None and "SeImpersonatePrivilege" in why and "LocalSystem" in why, why)
CS.has_impersonate = lambda: True
CS.own_session, CS.active_console_session, CS.user_token = o1, o2, o3
ctypes.windll.advapi32 = orig_wn

# token failure = the real service misconfiguration the user must see
CS.own_session = lambda pid=None: 0
CS.active_console_session = lambda: 1
CS.user_token = lambda s: None
CS.token_from_session_process = lambda s: None
r, why = CS.cross_session_spawn(["x"], tmp, CS.build_startupinfo("normal"))
check("no token names both routes as the service-user problem",
      r is None and "SeTCB" in why and "duplicate" in why, why)
CS.own_session, CS.active_console_session, CS.user_token = o1, o2, o3

# ---- UEBridge._spawn prefers cross-session, falls back to Popen ----
class FakeProc:
    def __init__(self, pid, token_session=None):
        self.pid, self.token_session = pid, token_session

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        return True

o5 = save(CS, "cross_session_spawn")
o6 = save(UE, "_popen")
UE._popen = lambda cmd, cwd, log_path, creation_flags=0, keep_stdin_pipe=False, show_window="": \
    FakeProc(1111)
CS.cross_session_spawn = lambda cmd, cwd, s, env=None, target_session=None, creation_flags=0: \
    (FakeProc(4242, 1), "")
cfg = default_config()
cfg["_root"] = str(tmp)
bridge = UE.UEBridge(cfg, cfg["upstream_url"])
p, path, why = bridge._spawn(["x"], tmp, bridge.launch_log, "normal", want_console=False)
check("launch prefers the user session", path == "cross_session" and p.token_session == 1, (path, p))

CS.cross_session_spawn = lambda cmd, cwd, s, env=None, target_session=None, creation_flags=0: \
    (None, "no foreign session to reach")
p, path, why = bridge._spawn(["x"], tmp, bridge.launch_log, "normal", want_console=False)
check("falls back to same-session spawn with the reason",
      path == "same_session" and "foreign" in why, (path, why))
p.terminate()

# window_mode honesty for both service shapes
o7 = save(UE, "_session_id")
o8, o9 = save(CS, "session_token"), save(CS, "has_impersonate")
UE._session_id = lambda pid: 0
CS.active_console_session = lambda: 1
CS.session_token = lambda s: (7777, "duplicate-token")
CS.has_impersonate = lambda: True
wm = UE.window_mode(cfg)
check("service+usable token reports cross_session_session and the route",
      wm.get("cross_session_session") == 1 and wm.get("cross_session_route") == "duplicate-token", wm)
check("honest note says editor lands on the user desktop",
      "desktop" in wm["note"] and "window" in wm["note"], wm["note"])
CS.session_token = lambda s: (None, "no usable token")
wm2 = UE.window_mode(cfg)
check("service without reachable session is honest about no windows",
      not wm2["windows_possible"] and "can be shown" in wm2["note"], wm2["note"])
UE._session_id = lambda pid: 1
wm3 = UE.window_mode(cfg)
check("interactive process claims no cross-session", wm3["windows_possible"] and not wm3.get("cross_session_session"), wm3)
CS.session_token, CS.has_impersonate = o8, o9

# build path: guard only for same-session children (can't see foreign windows)
async def guard_case():
    # stubs so build() reaches the spawn decision without touching the host
    bridge.can_build = lambda: True
    fake_bat = tmp / "build.bat"
    fake_bat.write_text("rem stub\n", encoding="utf-8")
    UE.render_template = lambda template, cfg: [str(fake_bat), "StubEditor", "Win64", "Development"]
    UE._popen = lambda cmd, cwd, log_path, creation_flags=0, keep_stdin_pipe=False, show_window="": \
        FakeProc(1111)
    CS.cross_session_spawn = lambda cmd, cwd, s, env=None, target_session=None, creation_flags=0: \
        (FakeProc(4242, 1), "")
    res = await bridge.build(blocking=False)
    check("cross-session build states the guard cannot watch it",
          res.get("spawn") == "cross_session" and "guard" in res.get("window_note", ""), res)
    check("cross-session build names the user session in the note",
      "1" in res.get("window_note", ""), res.get("window_note", ""))

    CS.cross_session_spawn = lambda cmd, cwd, s, env=None, target_session=None, creation_flags=0: \
        (None, "no foreign session to reach")
    bridge._build_proc = None  # first fake build finished; a real one would have to
    res = await bridge.build(blocking=False)
    check("same-session build keeps the minimize-guard",
          res.get("spawn") == "same_session" and bridge._build_guard is not None, res)

asyncio.run(guard_case())

# ---- launch guard: a session-0 service without a user-session token route
# must refuse, not spawn a GUI editor into session 0 (D3D12 RHI fatal, exit 3)
async def session0_case():
    bridge.can_build = lambda: True
    cfg["project_path"] = str(tmp / "S0.uproject")
    cfg["editor_binary"] = str(tmp / "UnrealEditor.exe")
    (tmp / "S0.uproject").write_text("{}", encoding="utf-8")
    async def fake_status():
        return {"upstream_alive": False, "editor_process_running": False}
    bridge.status = fake_status
    save(CS, "session_token")
    save(CS, "has_impersonate")
    CS.session_token = lambda s: (None, "no usable token")
    CS.has_impersonate = lambda: False
    UE._session_id = lambda pid: 0  # service in session 0
    res = await bridge.launch(wait_ready=False)
    check("session-0 service refuses editor launch",
          res.get("status") == "service_session_no_desktop" and not res.get("ok"), res)
    check("refusal names the D3D12 cause and the LocalSystem fix",
          "D3D12" in res.get("message", "") and "LocalSystem" in res.get("message", ""),
          res.get("message", ""))
    check("no editor child was spawned on the refused path",
          bridge._launch_proc is None, bridge._launch_proc)
    # with a usable cross-session route the launch must proceed
    bridge._launch_proc = None
    bridge._last_launch_ts = 0  # clear the debounce left by the refused launch
    CS.session_token = lambda s: (7777, "WTSQueryUserToken")
    CS.has_impersonate = lambda: True
    CS.cross_session_spawn = lambda cmd, cwd, s, env=None, target_session=None, creation_flags=0: \
        (FakeProc(4242, 1), "")
    res = await bridge.launch(wait_ready=False)
    check("cross-session route lets the launch through",
          res.get("ok") and res.get("spawn") == "cross_session", res)

asyncio.run(session0_case())
restore()

print(f"\n{passed}/{total} cross-session checks passed")
sys.exit(0 if passed == total else 1)
