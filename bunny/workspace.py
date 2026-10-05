"""Harness workspace detection — generic, no per-harness adapters.

"The project the agent is in right now" is resolved from spec-native and
OS-generic signals only, in this priority (server side decides the order):

  roots    — MCP spec channel: the harness advertised the `roots` capability
             in initialize; bunny fetches roots/list from that client.
  declared — explicit per-session declaration: HTTP header
             `x-harness-workspace` on any POST, or proxy__ue_declare_workspace.
  cwd      — OS probe: the TCP peer of the harness's HTTP connection ->
             owning process -> ancestor chain -> first process whose current
             directory contains a .uproject. Works for ANY harness launched
             from its project folder (CLI, IDE plugin, bridge).
  env      — BUNNY_WORKSPACE_PROJECT pin (operator config).

Windows APIs via ctypes (no psutil dependency); POSIX falls back to /proc.
All probes are read-only, TTL-cached, never raise, and are bypassed entirely
by cfg["workspace_adopt"]=False.
"""
from __future__ import annotations

import os
import socket
import struct
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from .log import get_logger

log = get_logger("workspace")

_WIN = sys.platform == "win32"

# Tuning knobs (module-level so tests can shrink them)
_PEER_PID_TTL = 3.0      # peer (ip,port) -> owning pid
_PID_CWD_TTL = 30.0      # pid -> current directory
_ANCESTRY_DEPTH = 8      # how far up the process tree to walk
_UPWARD_HOPS = 4         # harness cwd may be a subdir of the project root


def find_uproject_in(directory: str) -> str | None:
    """Return the .uproject inside a workspace dir, preferring one named for
    the directory (UE's usual convention) over an arbitrary file."""
    d = Path(directory)
    try:
        if not d.is_dir():
            return None
        cands = sorted(d.glob("*.uproject"))
    except (OSError, ValueError):
        return None
    if not cands:
        return None
    for c in cands:
        if c.stem.lower() == d.name.lower():
            return str(c)
    return str(cands[0])


def workspace_from_path(p: str, upward: int = 4) -> dict | None:
    """Accept a workspace dir OR a .uproject file path; resolve to a workspace.

    Walks up to `upward` parents: harness headers often carry a subfolder of
    the project (e.g. JetBrains ${project_files} = <project>/.idea).
    """
    p = (p or "").strip().strip('"')
    if not p:
        return None
    try:
        pp = Path(p)
        if pp.suffix.lower() == ".uproject" and pp.is_file():
            return {"project_dir": str(pp.parent), "uproject": str(pp)}
    except (OSError, ValueError):
        return None
    d = pp
    for _ in range(upward + 1):
        up = find_uproject_in(str(d))
        if up:
            return {"project_dir": str(d), "uproject": up}
        if d.parent == d:
            break
        d = d.parent
    return None


def roots_workspace(result: dict | None) -> dict | None:
    """Parse a roots/list result; first root containing a .uproject wins."""
    roots = (result or {}).get("roots") or []
    for r in roots:
        uri = str((r or {}).get("uri") or "")
        if not uri.startswith("file:"):
            continue
        try:
            path = urllib.parse.urlparse(uri).path
            if len(path) >= 3 and path[0] == "/" and path[2] == ":":
                path = path[1:]  # /D:/x -> D:/x (Windows file URI)
            path = urllib.parse.unquote(path)
        except ValueError:
            continue
        ws = workspace_from_path(path)
        if ws:
            return ws
    return None


def env_workspace(cfg: dict | None = None) -> dict | None:
    """Operator pin: BUNNY_WORKSPACE_PROJECT (dir or .uproject) = workspace."""
    p = os.environ.get("BUNNY_WORKSPACE_PROJECT") or (cfg or {}).get("workspace_project")
    ws = workspace_from_path(str(p or ""))
    if ws:
        ws["source"] = "env"
    return ws


# ---------------------------------------------------------------- OS probes

_lock = threading.Lock()
_peer_pid_cache: dict = {}   # (ip, port) -> (expires, pid|None)
_pid_cwd_cache: dict = {}    # pid -> (expires, cwd|None)

if _WIN:
    import ctypes
    import ctypes.wintypes as wt

    kernel32 = ctypes.windll.kernel32
    ntdll = ctypes.windll.ntdll

    class _ProcEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
            ("szExeFile", wt.WCHAR * 260),
        ]

    class _ProcBasicInfo(ctypes.Structure):
        # canonical PROCESS_BASIC_INFORMATION: sizeof MUST match what
        # NtQueryInformationProcess expects (48 on x64) or it returns
        # STATUS_INFO_LENGTH_MISMATCH (0xC0000004)
        _fields_ = [
            ("ExitStatus", ctypes.c_long),                      # +0x00
            ("PebBaseAddress", ctypes.c_void_p),                # +0x08
            ("AffinityMask", ctypes.c_void_p),                  # +0x10
            ("BasePriority", ctypes.c_long),                    # +0x18
            ("UniqueProcessId", ctypes.c_void_p),               # +0x20
            ("InheritedFromUniqueProcessId", ctypes.c_void_p),  # +0x28
        ]

    class _UnicodeString(ctypes.Structure):
        _fields_ = [("Length", ctypes.c_uint16),
                    ("MaxLength", ctypes.c_uint16),
                    ("Buffer", ctypes.c_void_p)]

    _X64 = sys.maxsize > 2**32
    # Vista+ x64 layouts, empirically calibrated on Win11 x64 (peb_scan.py):
    # PEB.ProcessParameters +0x20; RTL_USER_PROCESS_PARAMETERS.CurrentDirectory +0x38
    _PEB_PARAMS_OFF = 0x20 if _X64 else 0x10
    _RTL_CWD_OFF = 0x38 if _X64 else 0x24

    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.ReadProcessMemory.restype = wt.BOOL
    kernel32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    ntdll.NtQueryInformationProcess.argtypes = [wt.HANDLE, ctypes.c_ulong, ctypes.c_void_p,
                                                ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]

    def _tcp_owner_pid(peer_ip: str, peer_port: int, server_port: int) -> int | None:
        """PID owning the TCP connection (peer_ip:peer_port) -> our server port.

        GetExtendedTcpTable is exported by iphlpapi.dll (NOT kernel32).
        """
        AF_INET = 2
        TCP_TABLE_OWNER_PID_ALL = 5
        iphlpapi = ctypes.windll.iphlpapi
        iphlpapi.GetExtendedTcpTable.restype = wt.DWORD
        size = wt.DWORD(64 * 1024)
        buf = ctypes.create_string_buffer(size.value)
        r = iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, AF_INET,
                                         TCP_TABLE_OWNER_PID_ALL, 0)
        if r == 122:  # ERROR_INSUFFICIENT_BUFFER
            buf = ctypes.create_string_buffer(size.value)
            r = iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, AF_INET,
                                             TCP_TABLE_OWNER_PID_ALL, 0)
        if r != 0:
            return None
        n = int.from_bytes(buf.raw[0:4], "little")
        for i in range(n):
            row = buf.raw[4 + i * 24: 28 + i * 24]
            if len(row) < 24:
                break
            la = int.from_bytes(row[4:8], "little")
            lp = int.from_bytes(row[8:12], "little")
            rp = int.from_bytes(row[16:20], "little")
            pid = int.from_bytes(row[20:24], "little")
            lport = ((lp & 0xFF) << 8) | ((lp >> 8) & 0xFF)
            rport = ((rp & 0xFF) << 8) | ((rp >> 8) & 0xFF)
            lip = socket.inet_ntop(AF_INET, struct.pack("=I", la))
            if lport == peer_port and rport == server_port and lip == peer_ip and pid:
                return int(pid)
        return None

    def _read_remote_at(pid: int, ptr: int, n: int) -> bytes | None:
        h = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)  # VM_READ | VM_OPERATION
        if not h:
            return None
        try:
            buf = ctypes.create_string_buffer(n)
            done = ctypes.c_size_t(0)
            ok = kernel32.ReadProcessMemory(h, ctypes.c_void_p(ptr), buf, n,
                                            ctypes.byref(done))
            if not ok or done.value < n:
                return None
            return buf.raw[:n]
        finally:
            kernel32.CloseHandle(h)

    def _process_cwd(pid: int) -> str | None:
        """Current directory of `pid` via its PEB (Process Explorer technique)."""
        pid = int(pid)
        h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            info = _ProcBasicInfo()
            got = ctypes.c_ulong(0)
            st = ntdll.NtQueryInformationProcess(h, 0, ctypes.byref(info),
                                                 ctypes.sizeof(info), ctypes.byref(got))
            if st != 0 or not info.PebBaseAddress:
                return None
            peb = _read_remote_at(pid, int(info.PebBaseAddress) + _PEB_PARAMS_OFF, 8)
            if not peb:
                return None
            params = int.from_bytes(peb, "little")
            # RTL_USER_PROCESS_PARAMETERS.CurrentDirectory (CURDIR UNICODE_STRING)
            cd = _read_remote_at(pid, params + _RTL_CWD_OFF, 16)
            if not cd:
                return None
            us = _UnicodeString.from_buffer_copy(cd)
            if not us.Buffer or not us.Length:
                return None
            raw = _read_remote_at(pid, int(us.Buffer), int(us.Length))
            if not raw:
                return None
            return raw.decode("utf-16-le", "ignore").rstrip("\x00").rstrip("\\")
        finally:
            kernel32.CloseHandle(h)

    def _process_names() -> dict:
        """pid -> (parent_pid, exe_name) snapshot."""
        snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if snap == -1:
            return {}
        out = {}
        e = _ProcEntry32W()
        e.dwSize = ctypes.sizeof(e)
        if kernel32.Process32FirstW(snap, ctypes.byref(e)):
            while True:
                out[int(e.th32ProcessID)] = (int(e.th32ParentProcessID), str(e.szExeFile))
                if not kernel32.Process32NextW(snap, ctypes.byref(e)):
                    break
        kernel32.CloseHandle(snap)
        return out

else:  # pragma: no cover - POSIX fallbacks

    def _tcp_owner_pid(peer_ip, peer_port, server_port):
        return None  # no portable POSIX socket->pid table

    def _process_cwd(pid):
        try:
            return os.readlink(f"/proc/{int(pid)}/cwd")
        except OSError:
            return None

    def _process_names():
        # /proc/<pid>/stat: fields after ") " — index 1 is the parent pid
        out = {}
        try:
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                try:
                    fields = (Path("/proc") / entry / "stat").read_text().rsplit(") ", 1)[-1].split()
                    out[int(entry)] = (int(fields[1]), entry)
                except OSError:
                    continue
        except OSError:
            pass
        return out


def _peer_pid(peer, server_port: int) -> int | None:
    key = (str(peer[0]), int(peer[1]))
    now = time.time()
    with _lock:
        hit = _peer_pid_cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    pid = _tcp_owner_pid(key[0], key[1], server_port)
    with _lock:
        _peer_pid_cache[key] = (now + _PEER_PID_TTL, pid)
    return pid


def _cwd(pid: int):
    now = time.time()
    with _lock:
        hit = _pid_cwd_cache.get(pid)
        if hit and hit[0] > now:
            return hit[1]
    cwd = _process_cwd(pid)
    with _lock:
        _pid_cwd_cache[pid] = (now + _PID_CWD_TTL, cwd)
    return cwd


def _ancestry_workspace(peer_pid: int) -> dict | None:
    """Walk peer process + ancestors; first one whose cwd holds a .uproject wins."""
    names = _process_names()
    chain: list[int] = []
    pid = int(peer_pid)
    for _ in range(_ANCESTRY_DEPTH):
        if pid <= 0:
            break
        chain.append(pid)
        info = names.get(pid)
        pid = info[0] if info else 0
    for pid in chain:
        cwd = _cwd(pid)
        if not cwd:
            continue
        d = Path(cwd)
        for _ in range(_UPWARD_HOPS + 1):  # cwd may be a subdir of the project
            up = find_uproject_in(str(d))
            if up:
                return {"project_dir": str(d), "uproject": up, "source": "cwd",
                        "pid": pid, "proc": (names.get(pid) or (0, ""))[1],
                        "cwd": str(cwd)}
            if d.parent == d:
                break
            d = d.parent
    return None


def detect_peer_workspace(peer, server_port: int) -> dict | None:
    """OS probe: TCP peer -> owning process (and ancestors) -> project folder.

    Generic: any harness launched from its project folder is found, with no
    per-harness code. Returns None when the probe cannot resolve. Never raises.
    """
    if not peer:
        return None
    try:
        pid = _peer_pid(peer, server_port)
        if not pid:
            return None
        return _ancestry_workspace(pid)
    except Exception as e:  # pragma: no cover - probe safety net
        log.debug("peer workspace probe failed: %s", e)
        return None
