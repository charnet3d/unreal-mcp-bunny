"""Guardian test: emulate what UnrealBuildTool/conhost do — a build console
created minimized gets restored to normal — and prove the guardian
re-minimizes it WITHOUT activating it."""
import asyncio
import ctypes
import ctypes.wintypes as wt
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bunny.ue import _popen, _console_windows_minimize  # noqa: E402

u = ctypes.windll.user32
tmp = Path(tempfile.mkdtemp(prefix="bunny_guard_"))
log = tmp / "guard.log"
SW_RESTORE = 9

passed = total = 0


def check(name, cond, detail=""):
    global passed, total
    total += 1
    print(("PASS  " if cond else "FAIL  ") + name, detail if not cond else "")
    passed += bool(cond)


def windows_of(pids):
    out = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(h, _l):
        p = wt.DWORD(0)
        u.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value in pids and u.IsWindowVisible(h):
            out.append((h, bool(u.IsIconic(h))))
        return True

    u.EnumWindows(cb, 0)
    return out


async def main():
    p = _popen(["cmd", "/c", "ping", "-n", "12", "127.0.0.1"], tmp, log,
               show_window="minimized")
    await asyncio.sleep(1.0)
    wins = windows_of({p.pid})
    check("minimized child has a window", bool(wins), wins)
    check("window starts iconic", bool(wins) and all(i for _, i in wins), wins)

    # emulate UBT/conhost restoring the console to normal
    for h, _ in wins:
        u.ShowWindow(h, SW_RESTORE)
    await asyncio.sleep(0.3)
    restored = windows_of({p.pid})
    check("window was restored (test precondition)",
          bool(restored) and any(not i for _, i in restored), restored)

    # the guardian pass
    n = await asyncio.to_thread(_console_windows_minimize, p.pid)
    await asyncio.sleep(0.2)
    after = windows_of({p.pid})
    check("guardian reports it acted", n >= 1, n)
    check("window re-minimized by guardian",
          bool(after) and all(i for _, i in after), after)

    p.terminate()
    p.wait()
    print(f"\n{passed}/{total} guardian checks passed")
    sys.exit(0 if passed == total else 1)


asyncio.run(main())
