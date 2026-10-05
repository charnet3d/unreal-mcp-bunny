"""Prove bunny._popen window policies on real console children:
 minimized -> window exists, placement minimized, foreground NOT stolen
 hidden    -> no visible top-level window for the child pid
"""
import ctypes
import ctypes.wintypes as wt
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bunny.ue import _popen  # noqa: E402

user32 = ctypes.windll.user32
SW_SHOWMINIMIZED = 2


class WinPlacement(ctypes.Structure):
    _fields_ = [("flags", wt.UINT), ("showCmd", wt.UINT),
                ("ptMinPos", wt.POINT), ("ptMaxPos", wt.POINT),
                ("rcNormalPosition", wt.RECT)]


def visible_windows(pid):
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, lparam):
        pid_ = wt.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_))
        if pid_.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found


def placement(hwnd):
    p = WinPlacement()
    p.flags = 1  # WPF_ASYNCWINDOWPLACEMENT
    user32.GetWindowPlacement(hwnd, ctypes.byref(p))
    return p.showCmd


tmp = Path(tempfile.mkdtemp(prefix="bunny_window_"))
results = []

# minimized
fg_before = user32.GetForegroundWindow()
p = _popen(['cmd', '/c', 'ping', '-n', '6', '127.0.0.1'],
           __import__('pathlib').Path(tmp), __import__('pathlib').Path(tmp) / 'win_min.log',
           show_window='minimized')
time.sleep(1.2)
wins = visible_windows(p.pid)
# IsIconic = the window's CURRENT minimized state (GetWindowPlacement.showCmd
# is only the command for the NEXT ShowWindow call, not the live state)
states = [bool(user32.IsIconic(h)) for h in wins]
fg_after = user32.GetForegroundWindow()
ok_min = bool(wins) and all(states)
ok_fg = fg_after not in wins if wins else False
results.append(('minimized: window exists + IsIconic', ok_min, wins, states))
results.append(('minimized: foreground not stolen', ok_fg, hex(fg_after),
                [hex(h) for h in wins]))
p.wait()

# hidden
p2 = _popen(['cmd', '/c', 'ping', '-n', '6', '127.0.0.1'],
            __import__('pathlib').Path(tmp), __import__('pathlib').Path(tmp) / 'win_hid.log',
            show_window='hidden')
time.sleep(1.2)
wins2 = visible_windows(p2.pid)
results.append(('hidden: no visible window for child pid', wins2 == [], wins2))
p2.wait()

fails = 0
for name, ok, *detail in results:
    print(('PASS  ' if ok else 'FAIL  ') + name, detail)
    fails += 0 if ok else 1
print(f'{len(results) - fails}/{len(results)} window checks passed')
sys.exit(1 if fails else 0)
