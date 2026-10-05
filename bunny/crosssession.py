"""Cross-session launch support for bunny running as a Windows service.

A service lives in session 0, which has no interactive desktop: a GUI editor
spawned there cannot show a window, and an RHI that needs a desktop (swap
chain) usually fails too (observed: exit code 3, D3D12Util.cpp fatal). When the
service runs AS THE USER, it can borrow the active console session's token
(WTSQueryUserToken) and spawn the process INTO the user's session
(CreateProcessWithTokenW) — the programmatic equivalent of
`runas /user:<you> /interactive`, so the editor window appears on the user's
desktop even though bunny itself stays in session 0.

Indirection functions exist so tests can monkeypatch them without touching the
real OS. wintypes.HANDLE is 32-bit on x64, so every handle uses c_void_p — a
64-bit handle would otherwise be truncated.
"""
from __future__ import annotations

import os
import subprocess
import time
import ctypes
import ctypes.wintypes as wt

CREATE_NEW_CONSOLE = 0x00000010
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
STARTF_USESHOWWINDOW = 0x00000001
STARTF_USESTDHANDLES = 0x00020000
STILL_ACTIVE = 259
SW_SHOWNORMAL, SW_HIDE, SW_SHOWMINNOACTIVE = 1, 0, 7
TOKEN_QUERY, TOKEN_DUPLICATE, TOKEN_PRIVILEGES = 0x0008, 0x0002, 3
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SECURITY_ID, TOKEN_PRIMARY = 2, 1
# same-user processes we can borrow a session token from when WTS is unavailable
_SESSION_PROBES = ("explorer.exe", "runtimebroker.exe", "shell.exe")


def kernel32():
    return ctypes.windll.kernel32


def last_error():
    """Error of the failed call: stored last error, with a direct fallback.

    ctypes records the last error only when UseLastError is set on the windll
    instance; otherwise get_last_error() can report 0 for a genuine failure
    (observed as 'CreateProcessWithTokenW failed (err 0)'). Fall back to the
    thread's own GetLastError so the message the agent sees is the real cause.
    """
    code = ctypes.get_last_error()
    if code:
        return int(code)
    return int(kernel32().GetLastError())


def wtsapi32():
    w = ctypes.windll.wtsapi32
    w.WTSQueryUserToken.argtypes = [wt.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    return w


def advapi32():
    a = ctypes.windll.advapi32
    a.CreateProcessWithTokenW.restype = wt.BOOL
    a.CreateProcessWithTokenW.argtypes = [ctypes.c_void_p, wt.DWORD, ctypes.c_wchar_p,
                                          ctypes.c_wchar_p, wt.DWORD, ctypes.c_void_p,
                                          ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p]
    # CreateProcessAsUserW is the CROSS-SESSION spawn function: it places the
    # child in the SESSION OF THE TOKEN. CreateProcessWithTokenW places the
    # child in the CALLER's session and only borrows the token's identity, so a
    # session-0 service using it lands the editor in session 0 anyway: no
    # window, and the swap chain dies with DXGI_ERROR_NOT_CURRENTLY_AVAILABLE
    # (observed: D3D12Util.cpp:1062 via WindowsD3D12Viewport.cpp:162, exit 3).
    a.CreateProcessAsUserW.restype = wt.BOOL
    a.CreateProcessAsUserW.argtypes = [ctypes.c_void_p,          # hToken
                                       ctypes.c_wchar_p,          # lpApplicationName
                                       ctypes.c_wchar_p,          # lpCommandLine
                                       ctypes.c_wchar_p,          # lpProcessAttributes
                                       ctypes.c_wchar_p,          # lpThreadAttributes
                                       wt.BOOL,                   # bInheritHandles
                                       wt.DWORD,                  # dwCreationFlags
                                       ctypes.c_void_p,           # lpEnvironment
                                       ctypes.c_wchar_p,          # lpCurrentDirectory
                                       ctypes.c_void_p,           # lpStartupInfo
                                       ctypes.c_void_p]           # lpProcessInformation
    a.OpenProcessToken.argtypes = [ctypes.c_void_p, wt.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    a.DuplicateTokenEx.argtypes = [ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, ctypes.c_int,
                                   ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
    a.GetTokenInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
                                      ctypes.POINTER(wt.DWORD)]
    return a


def active_console_session() -> int | None:
    """Session id of the console the user is logged into (kernel32 owns this call)."""
    if os.name != "nt":
        return None
    s = int(kernel32().WTSGetActiveConsoleSessionId())
    return s if s not in (0, 0xFFFFFFFF) else None


def user_token(session: int):
    """Primary token of the user logged into `session` (None if unreachable).

    WTSQueryUserToken only works for LocalSystem + SeTCBPrivilege, so a service
    running as the user normally cannot use it (observed: ERROR_PRIVILEGE_NOT_HELD
    1314). That is why `session_token` below also tries the duplicate route.
    """
    if os.name != "nt":
        return None
    tok = ctypes.c_void_p(0)
    if not wtsapi32().WTSQueryUserToken(wt.DWORD(session), ctypes.byref(tok)):
        return None
    return tok


def toolhelp():
    k = kernel32()
    k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k.Process32First.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k.Process32Next.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    k.ProcessIdToSessionId.argtypes = [wt.DWORD, ctypes.POINTER(wt.DWORD)]
    return k


def token_from_session_process(session: int):
    """Token duplicated from a same-user process already running in `session`.

    Allowed without special privileges when we run as that same user
    (OpenProcessToken on a same-user process is permitted), and the duplicate
    carries the source session id — so CreateProcessWithTokenW lands the child
    there. Used as the fallback when WTSQueryUserToken is not available to us.
    """
    if os.name != "nt":
        return None
    TH32CS_SNAPPROCESS = 2
    class PROCENT(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD),
                    ("th32CntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                    ("th32PriClassBase", wt.LONG), ("dwFlags", wt.DWORD),
                    ("szExeFile", ctypes.c_char * 260)]
    k = toolhelp()
    a = advapi32()
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap:
        return None
    try:
        e = PROCENT()
        e.dwSize = ctypes.sizeof(PROCENT)
        if not k.Process32First(snap, ctypes.byref(e)):
            return None
        while True:
            sid = wt.DWORD(0)
            if k.ProcessIdToSessionId(wt.DWORD(e.th32ProcessID), ctypes.byref(sid)) and int(sid.value) == session:
                name = e.szExeFile.decode("utf-8", "ignore").strip("\x00").lower()
                if name in _SESSION_PROBES:
                    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, wt.DWORD(e.th32ProcessID))
                    if h:
                        tok = ctypes.c_void_p(0)
                        if a.OpenProcessToken(h, TOKEN_QUERY | TOKEN_DUPLICATE, ctypes.byref(tok)):
                            dup = ctypes.c_void_p(0)
                            if a.DuplicateTokenEx(tok, 0, None, SECURITY_ID, TOKEN_PRIMARY,
                                                  ctypes.byref(dup)):
                                return dup
                    continue
            if not k.Process32Next(snap, ctypes.byref(e)):
                break
        return None
    finally:
        k.CloseHandle(snap)


def token_session_value(tok) -> int | None:
    """Session id carried by a token — what CreateProcessWithTokenW will use."""
    if os.name != "nt" or not tok:
        return None
    sid = wt.DWORD(0)
    a = advapi32()
    handle = getattr(tok, "value", tok)
    if not a.GetTokenInformation(ctypes.c_void_p(int(handle)), 2,
                                 ctypes.cast(ctypes.byref(sid), ctypes.c_void_p), 4,
                                 ctypes.byref(wt.DWORD(0))):
        return None
    return int(sid.value)


def session_token(session: int):
    """Token we can actually use for `session`: WTS route, then duplicate route.

    Returns (token, source) or (None, reason). `source` is what the logs show,
    so an operator can tell which route the service used. Cached briefly: status
    calls this per request and the toolhelp scan should not run that often.
    """
    hit = _TOKEN_CACHE.get(session)
    if hit and time.time() - hit[0] < _TOKEN_TTL:
        return hit[1]
    result = _resolve_token(session)
    _TOKEN_CACHE[session] = (time.time(), result)
    return result


_TOKEN_CACHE: dict[int, tuple[float, tuple]] = {}
_TOKEN_TTL = 30.0


def token_cache_clear():
    _TOKEN_CACHE.clear()


def _resolve_token(session: int):
    tok = user_token(session)
    if tok:
        return tok, "WTSQueryUserToken"
    werr = ctypes.get_last_error()
    tok = token_from_session_process(session)
    if tok:
        return tok, "duplicate-token"
    return None, (f"no usable token for session {session} "
                  f"(WTSQueryUserToken err {werr} - needs LocalSystem/SeTCB - "
                  f"and no same-user process in that session to duplicate from)")


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]


class _LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", _LUID), ("Attributes", wt.DWORD)]


class _TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("Count", wt.DWORD), ("Privileges", _LUID_AND_ATTRIBUTES * 1)]


def token_privilege_names(tok=None):
    """Names of the privileges a token holds.

    GetTokenInformation(TokenPrivileges) returns LUIDs, not names — resolving
    each through LookupPrivilegeNameW is the only way to test for a specific
    privilege. Two traps, both hit here:
      - the size query returns FALSE with the needed size (ERROR_INSUFFICIENT_BUFFER
        is normal), so a failed size query must not be treated as an error;
      - the Privileges array is variable-length, so entries are read by pointer
        offset, not by indexing a fixed-size field.
    """
    if os.name != "nt":
        return None
    a = advapi32()
    k = toolhelp()
    own = tok
    if own is None:
        own = ctypes.c_void_p(0)
        if not a.OpenProcessToken(ctypes.c_void_p(k.GetCurrentProcess()), TOKEN_QUERY,
                                  ctypes.byref(own)):
            return None
    need = wt.DWORD(0)
    a.GetTokenInformation(own, TOKEN_PRIVILEGES, None, 0, ctypes.byref(need))
    if not need.value:
        return None
    buf = ctypes.create_string_buffer(need.value)
    if not a.GetTokenInformation(own, TOKEN_PRIVILEGES, ctypes.cast(buf, ctypes.c_void_p), need,
                                 ctypes.byref(wt.DWORD(0))):
        return None
    count = int(ctypes.cast(buf, ctypes.POINTER(ctypes.c_ulong)).contents.value)
    entries = ctypes.cast(ctypes.byref(buf, 4), ctypes.POINTER(_LUID_AND_ATTRIBUTES))
    a.LookupPrivilegeNameW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(_LUID),
                                       ctypes.c_wchar_p, ctypes.POINTER(wt.DWORD)]
    names, name_buf, size = [], ctypes.create_unicode_buffer(64), wt.DWORD(64)
    for i in range(count):
        entry = entries[i]
        size.value = 64
        if a.LookupPrivilegeNameW(None, ctypes.byref(entry.Luid), name_buf, ctypes.byref(size)):
            names.append(name_buf.value)
    return names


def has_privilege(name, tok=None):
    """Whether this process (or `tok`) holds a named privilege."""
    return name in (token_privilege_names(tok) or [])


def has_impersonate():
    """SeImpersonatePrivilege: CreateProcessWithTokenW cannot run without it."""
    return os.name == "nt" and has_privilege("SeImpersonatePrivilege")


class STARTUPINFOW(ctypes.Structure):
    """Full STARTUPINFOW, including std handles (STARTF_USESTDHANDLES)."""
    _fields_ = [("cb", wt.DWORD), ("lpReserved", ctypes.c_wchar_p),
                ("lpDesktop", ctypes.c_wchar_p), ("lpTitle", ctypes.c_wchar_p),
                ("dwX", wt.DWORD), ("dwY", wt.DWORD), ("dwXSize", wt.DWORD),
                ("dwYSize", wt.DWORD), ("dwXCountChars", wt.DWORD),
                ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD),
                ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD),
                ("cbReserved2", wt.WORD), ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", ctypes.c_void_p), ("hStdOutput", ctypes.c_void_p),
                ("hStdError", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", ctypes.c_void_p), ("hThread", ctypes.c_void_p),
                ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]


class ForeignProc:
    """poll()/wait()/terminate() surface for a cross-session spawned process."""

    def __init__(self, pid: int, handle, token_session: int | None = None,
                 token_source: str | None = None):
        self.pid = int(pid)
        self._handle = handle
        self.token_session = token_session
        self.token_source = token_source
        self._ret = None

    def poll(self):
        if self._ret is not None:
            return self._ret
        code = wt.DWORD(0)
        if kernel32().GetExitCodeProcess(ctypes.c_void_p(self._handle), ctypes.byref(code)):
            if code.value != STILL_ACTIVE:
                self._ret = int(code.value)
        return self._ret

    def wait(self, timeout=None):
        deadline = (time.time() + timeout) if timeout else None
        while True:
            r = self.poll()
            if r is not None:
                return r
            if deadline and time.time() >= deadline:
                return None
            time.sleep(0.5)

    def terminate(self):
        if kernel32().TerminateProcess(ctypes.c_void_p(self._handle), 1):
            self.wait(5)
            return True
        return False

    @property
    def returncode(self):
        return self._ret


def cross_session_flags(want_console: bool, show_window: str) -> int:
    """CreateProcess flags for a cross-session child.

    A GUI app must NOT be given a console and must NOT inherit the service's
    console: UnrealEditor.exe registers a console control handler, so Ctrl+C
    sent to the service console (nssm does this on every service stop) exits
    every console-inheriting child with 0xC000013A (STATUS_CONTROL_C_EXIT) —
    observed as the editor dying seconds after a cross-session launch.
    DETACHED_PROCESS leaves it console-less. Build.bat is a console app and
    needs one — minimized when asked, none when show_window='hidden'.
    """
    flags = CREATE_NEW_PROCESS_GROUP | CREATE_UNICODE_ENVIRONMENT
    if show_window == "hidden":
        flags |= 0x08000000  # CREATE_NO_WINDOW
    elif want_console:
        flags |= CREATE_NEW_CONSOLE
    else:
        flags |= 0x00000008  # DETACHED_PROCESS: never inherit the service console
    return flags


def env_block() -> ctypes.c_void_p:
    """Current environment as the double-null UTF-16 block CreateProcess wants."""
    pairs = b"".join(f"{k}={v}\x00".encode("utf-16-le") for k, v in os.environ.items())
    buf = ctypes.create_string_buffer(pairs + b"\x00\x00", len(pairs) + 2)
    return ctypes.cast(ctypes.byref(buf), ctypes.c_void_p)


def user_env_block(tok):
    """Environment block of the TOKEN's user (None if unavailable).

    A service running as LocalSystem has SYSTEM's environment: spawning the
    editor with it puts %USERPROFILE%/%LOCALAPPDATA% under systemprofile, so
    editor preferences and per-user caches land in the wrong profile.
    CreateEnvironmentBlock builds the token user's real environment instead.
    Caller destroys it after CreateProcess returns (DestroyEnvironmentBlock).
    """
    if os.name != "nt" or not tok:
        return None
    u = ctypes.windll.userenv
    u.CreateEnvironmentBlock.restype = wt.BOOL
    u.CreateEnvironmentBlock.argtypes = [ctypes.POINTER(ctypes.c_void_p),
                                         ctypes.c_void_p, wt.BOOL]
    u.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]
    block = ctypes.c_void_p(0)
    handle = int(getattr(tok, "value", tok))
    if not u.CreateEnvironmentBlock(ctypes.byref(block), ctypes.c_void_p(handle), False):
        return None
    if not block.value:
        return None
    return block


def destroy_env_block(block) -> None:
    if os.name == "nt" and block:
        try:
            ctypes.windll.userenv.DestroyEnvironmentBlock(ctypes.c_void_p(int(block.value)))
        except Exception:
            pass


def build_startupinfo(show_window: str, stdout_handle=None, desktop: str | None = None):
    """STARTUPINFOW with explicit window state (never inherited from the parent)."""
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    si.dwFlags = STARTF_USESHOWWINDOW
    codes = {"normal": SW_SHOWNORMAL, "hidden": SW_HIDE, "minimized": SW_SHOWMINNOACTIVE}
    si.wShowWindow = codes.get(str(show_window or "normal"), SW_SHOWNORMAL)
    if desktop:
        si.lpDesktop = desktop
    if stdout_handle:
        si.dwFlags |= STARTF_USESTDHANDLES
        si.hStdInput = ctypes.c_void_p(0)
        si.hStdOutput = si.hStdError = ctypes.c_void_p(int(stdout_handle))
    return si


def own_session(pid: int | None = None) -> int | None:
    """Session id of the calling process (0 = service). Indirection for tests."""
    if os.name != "nt":
        return None
    sid = wt.DWORD(0)
    target = wt.DWORD(int(pid if pid is not None else os.getpid()))
    if kernel32().ProcessIdToSessionId(target, ctypes.byref(sid)):
        return int(sid.value)
    return None


def cross_session_spawn(cmd: list[str], cwd, si: STARTUPINFOW, env=None,
                       target_session: int | None = None, creation_flags: int = 0):
    """Spawn `cmd` in the active user session. Returns (ForeignProc, None) or
    (None, reason) — the caller must then fall back to same-session Popen.

    lpDesktop in STARTUPINFO is what puts the child on the user's desktop
    ("Winsta0\\Default"); without it a session-0 caller's child keeps the
    service's window station and no window can appear. Std-handle inheritance is
    best-effort here — the engine's own log under Saved/Logs is the
    authoritative one, so nothing critical depends on it.
    """
    own = own_session()
    target = target_session if target_session is not None else active_console_session()
    if own is None or target is None or target == own:
        return None, "no foreign session to reach"
    if not has_impersonate():
        # CreateProcessWithTokenW is the only way to spawn AS the user; without
        # this privilege a user-run service cannot reach the user's session.
        return None, ("SeImpersonatePrivilege not held — a service running as the "
                      "user cannot spawn into the user's session; run bunny as "
                      "LocalSystem (which holds it) or in an interactive session")
    tok, info = session_token(target)
    if tok is None:
        return None, info

    pi = PROCESS_INFORMATION()
    cmdline = subprocess.list2cmdline(cmd)
    # Environment: prefer the TOKEN USER's block. A LocalSystem service carries
    # SYSTEM's environment (%USERPROFILE%=systemprofile), which would put the
    # editor's per-user settings/caches in the wrong profile. Only a block that
    # CreateEnvironmentBlock allocated may be handed to DestroyEnvironmentBlock
    # — destroying our own ctypes buffer that way corrupts the heap.
    env_from_userenv = False
    if env is None:
        env = user_env_block(tok)
        if env is not None:
            env_from_userenv = True
        else:
            env = env_block()
    # Default: no console (GUI-safe). Callers needing a console for a console
    # app pass cross_session_flags(want_console=True, show_window) explicitly.
    flags = creation_flags or (CREATE_NEW_PROCESS_GROUP | CREATE_UNICODE_ENVIRONMENT)
    # CreateProcessAsUserW: the child lands in the TOKEN's session (the user's
    # console). CreateProcessWithTokenW would land it in OUR session (0) and
    # only borrow the user's identity — windowless, D3D12 swap chain dead.
    tok_handle = ctypes.c_void_p(int(getattr(tok, "value", tok)))
    ok = advapi32().CreateProcessAsUserW(
        tok_handle, None, ctypes.c_wchar_p(cmdline), None, None, False,
        flags, env, str(cwd), ctypes.byref(si), ctypes.byref(pi))
    if env_from_userenv:
        destroy_env_block(env)
    if not ok:
        # last_error(), not ctypes.get_last_error(): advapi32's windll instance
        # has UseLastError off, so get_last_error() reports 0 for a real failure
        # (observed as 'failed (err 0)' hiding the true code).
        return None, f"CreateProcessWithTokenW failed (err {last_error()})"
    return ForeignProc(pi.dwProcessId, pi.hProcess, token_session=target,
                       token_source=info), None
