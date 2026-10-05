"""Generic workspace-detection tests: fake roots/headers/OS probes, no host state.

Run: python tests/test_workspace.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bunny.workspace as W  # noqa: E402
from bunny.workspace import (find_uproject_in, roots_workspace,  # noqa: E402
                             workspace_from_path, detect_peer_workspace)

RESULTS = []


def check(name: str, cond: bool, detail: str = ""):
    RESULTS.append((name, cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


def main():
    tmp = ROOT / "data" / "testrun_ws"
    if tmp.exists():
        import shutil
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)

    proj_live = tmp / "LiveProj"
    proj_noue = tmp / "NoUEProj"
    for p in (proj_live, proj_noue):
        p.mkdir(parents=True)
    (proj_live / "LiveProj.uproject").write_text('{"EngineAssociation": "5.8"}', encoding="utf-8")

    # ---- path resolution ----
    check("workspace_from_path: dir with .uproject",
          (workspace_from_path(str(proj_live)) or {}).get("uproject", "").endswith("LiveProj.uproject"))
    check("workspace_from_path: .uproject file directly",
          (workspace_from_path(str(proj_live / "LiveProj.uproject")) or {}).get("project_dir")
          == str(proj_live))
    check("workspace_from_path: subdir walks UP to project root",
          (workspace_from_path(str(proj_live / "sub" / "deep")) or {}).get("project_dir")
          == str(proj_live))
    (proj_live / "sub" / "deep").mkdir(parents=True)
    check("workspace_from_path: subdir (created) walks up",
          (workspace_from_path(str(proj_live / "sub" / "deep")) or {}).get("project_dir")
          == str(proj_live))
    check("workspace_from_path: non-project dir -> None",
          workspace_from_path(str(proj_noue)) is None)

    two = tmp / "Two"
    two.mkdir()
    (two / "Bar.uproject").write_text("{}", encoding="utf-8")
    (two / "Two.uproject").write_text("{}", encoding="utf-8")
    check("prefers .uproject named for the directory",
          Path(find_uproject_in(str(two))).name == "Two.uproject")

    # ---- roots/list parsing (file URIs, incl. Windows drive letters) ----
    drv = str(proj_live).replace("\\", "/")
    res = {"roots": [{"uri": f"file:///{drv}", "name": "LiveProj"}]}
    check("roots_workspace: file:/// URI (windows drive) resolves",
          (roots_workspace(res) or {}).get("uproject", "").endswith("LiveProj.uproject"),
          str(roots_workspace(res)))
    res2 = {"roots": [{"uri": "file:///nonexistent/xx", "name": "x"},
                      {"uri": f"file:///{drv}", "name": "y"}]}
    check("roots_workspace: first non-project root skipped",
          (roots_workspace(res2) or {}).get("uproject", "").endswith("LiveProj.uproject"))
    check("roots_workspace: roots:false-shaped -> None",
          roots_workspace({"roots": []}) is None)

    # ---- OS probe (peer -> pid -> cwd), fully faked ----
    W._tcp_owner_pid = lambda ip, port, sport: 4242          # fake
    W._process_names = lambda: {4242: (1, "harness.exe"), 1: (0, "svchost.exe")}
    W._cwd = lambda pid: str(proj_live) if pid == 4242 else None
    W._peer_pid_cache.clear()
    ws = detect_peer_workspace(("127.0.0.1", 51234), 8765)
    check("peer probe: harness cwd = project -> detected",
          bool(ws) and ws["source"] == "cwd" and ws["uproject"].endswith("LiveProj.uproject"),
          str(ws))

    # harness launched from a SUBDIR of the project
    W._cwd = lambda pid: str(proj_live / "sub") if pid == 4242 else None
    W._peer_pid_cache.clear()
    ws = detect_peer_workspace(("127.0.0.1", 51235), 8765)
    check("peer probe: cwd is subdir of project -> walks up",
          bool(ws) and ws["project_dir"] == str(proj_live), str(ws))

    # ancestor has the project cwd, harness itself launched elsewhere
    W._cwd = lambda pid: (str(proj_live) if pid == 1 else str(proj_noue))
    W._peer_pid_cache.clear()
    ws = detect_peer_workspace(("127.0.0.1", 51236), 8765)
    check("peer probe: ancestor process cwd wins",
          bool(ws) and ws["pid"] == 1 and ws["uproject"].endswith("LiveProj.uproject"), str(ws))

    # no .uproject anywhere
    W._cwd = lambda pid: str(proj_noue)
    W._peer_pid_cache.clear()
    check("peer probe: non-project cwd -> None",
          detect_peer_workspace(("127.0.0.1", 51237), 8765) is None)

    # probe failure (pid unresolvable) -> None
    W._tcp_owner_pid = lambda ip, port, sport: None
    W._peer_pid_cache.clear()
    check("peer probe: unresolvable peer -> None",
          detect_peer_workspace(("127.0.0.1", 51238), 8765) is None)
    check("peer probe: missing peer -> None", detect_peer_workspace(None, 8765) is None)

    ok = sum(1 for _, c in RESULTS if c)
    print(f"\n{ok}/{len(RESULTS)} workspace checks passed")
    sys.exit(0 if ok == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
