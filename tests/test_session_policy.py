"""Session-0 honesty: when bunny runs as a service (session 0), window_mode must
report 'no window possible' instead of pretending STARTUPINFO was the cause.
Deterministic via monkeypatching — no host state touched."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bunny.ue as UE  # noqa: E402
from bunny.config import default_config  # noqa: E402

passed = total = 0


def check(name, cond, detail=""):
    global passed, total
    total += 1
    print(("PASS  " if cond else "FAIL  ") + name, detail if not cond else "")
    passed += bool(cond)


orig = UE._session_id
try:
    # service with NO reachable user session -> no windows possible
    UE._session_id = lambda pid: 0
    import bunny.crosssession as CS  # noqa: E402
    orig_active = CS.active_console_session
    CS.active_console_session = lambda: None
    wm = UE.window_mode(default_config())
    check("session 0 detected", wm["process_session"] == 0, wm)
    check("service mode with no reachable session reports windows impossible",
          not wm["windows_possible"], wm)
    check("service mode emits an honest note", "session 0" in wm["note"]
          and "service" in wm["note"], wm["note"])
    # service + reachable session with a usable token: windows possible, and the
    # note names the token route it would use
    CS.active_console_session = lambda: 1
    orig_token, orig_imp = CS.session_token, CS.has_impersonate
    CS.session_token = lambda s: (1, "duplicate-token")
    CS.has_impersonate = lambda: True
    wm1 = UE.window_mode(default_config())
    check("service + usable token reports windows possible via cross-session",
          wm1["windows_possible"] and wm1["cross_session_session"] == 1
          and wm1["cross_session_route"] == "duplicate-token", wm1)
    check("note for that case mentions the user desktop", "desktop" in wm1["note"], wm1["note"])
    # reachable session but WTS denied and no token to duplicate -> must NOT
    # promise a window: overpromising here is what misled the agent earlier
    CS.session_token = lambda s: (None, "no usable token (WTSQueryUserToken needs SeTCB)")
    wm1b = UE.window_mode(default_config())
    check("reachable session without a usable token still reports no windows",
          not wm1b["windows_possible"] and not wm1b["cross_session_session"], wm1b)
    check("note explains the missing privilege, not just 'session 0'",
          "SeTCB" in wm1b["note"] and "no window can be shown" in wm1b["note"], wm1b["note"])
    CS.session_token, CS.has_impersonate = orig_token, orig_imp
    CS.active_console_session = orig_active
    check("policies still reported (launch normal, build minimized)",
          wm["launch_window"] == "normal" and wm["build_window"] == "minimized", wm)

    # interactive case: session 1 -> windows possible, no note
    UE._session_id = lambda pid: 1
    wm2 = UE.window_mode(default_config())
    check("interactive session reports windows_possible=True", wm2["windows_possible"], wm2)
    check("interactive session emits no service note", wm2["note"] == "", wm2["note"])

    # startupinfo always explicit: every mode returns a STARTUPINFO (never inherit)
    for mode in ("normal", "minimized", "hidden", ""):
        si = UE._startupinfo(mode)
        ok = si is not None and (si.dwFlags & int(getattr(
            __import__("subprocess"), "STARTF_USESHOWWINDOW", 1)))
        check(f"startupinfo explicit for mode '{mode or 'default'}'", bool(ok), si)

    # BUNNY_BUILD_WINDOW env override honored through load_config
    import os
    os.environ["BUNNY_BUILD_WINDOW"] = "hidden"
    cfg = default_config()
    from bunny.config import load_config  # noqa: E402
    cfg2 = load_config("/nonexistent-config-for-test.json")
    check("env override BUNNY_BUILD_WINDOW applies", cfg2["build_window"] == "hidden", cfg2)
    check("launch_window default present", cfg2["launch_window"] == "normal", cfg2)
    del os.environ["BUNNY_BUILD_WINDOW"]
finally:
    UE._session_id = orig

print(f"\n{passed}/{total} session-policy checks passed")
sys.exit(0 if passed == total else 1)
