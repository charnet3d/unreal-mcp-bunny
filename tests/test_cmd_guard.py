"""Guard test: the proxy must never launch UnrealEditor-Cmd.exe.

-Cmd is for command/test runs that close the engine afterwards. Any launch
meant to keep the editor alive must use the GUI UnrealEditor.exe:
  - editor_binary points at -Cmd + GUI sibling exists -> silently switch to GUI
  - editor_binary points at -Cmd + no sibling          -> refuse cmd_editor_not_allowed
"""
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bunny.config import load_config
from bunny.ue import UEBridge
import bunny.ue as ue_mod

FAILS = []


def check(name: str, cond: bool, detail: str = ""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="bunny_cmd_guard_"))
    orig_procs, orig_popen = ue_mod.UEBridge._editor_procs, ue_mod._popen
    try:
        # Fake engine layout: engine/Engine/Binaries/Win64/{UnrealEditor.exe, UnrealEditor-Cmd.exe}
        bindir = tmp / "Engine" / "Binaries" / "Win64"
        bindir.mkdir(parents=True)
        gui = bindir / "UnrealEditor.exe"
        cmd = bindir / "UnrealEditor-Cmd.exe"
        gui.write_bytes(b"MZ")
        cmd.write_bytes(b"MZ")
        (tmp / "Engine" / "Build").mkdir(parents=True)
        (tmp / "Engine" / "Build" / "Build.version").write_text(json.dumps(
            {"MajorVersion": 5, "MinorVersion": 8, "PatchVersion": 3}))
        proj = tmp / "GuardProj.uproject"
        proj.write_text(json.dumps({"FileVersion": 3, "EngineAssociation": "5.8"}))

        cfg = load_config(tmp / "no-such-config.json")  # defaults only; no host config.json
        cfg.update({
            "project_path": str(proj),
            "engine_root": str(tmp),
            "editor_binary": str(cmd),
            "scan_extra_roots": [],
            "auto_adopt_project": False,
        })

        # isolate from host: pretend no editor process exists on this machine
        ue_mod.UEBridge._editor_procs = lambda self: []

        # branch A: -Cmd with GUI sibling -> switched to GUI, never -Cmd in argv
        ue = UEBridge(dict(cfg), "http://127.0.0.1:1/mcp")
        captured = {}

        def fake_popen(argv, cwd, log_path, creation_flags=0, keep_stdin_pipe=False,
                       show_window=""):
            captured["argv"] = list(argv)
            captured["keep_stdin_pipe"] = keep_stdin_pipe
            raise OSError("stop-here: guard decisions happen before spawn")

        ue_mod._popen = fake_popen
        res = await ue.launch(wait_ready=False)
        check("switch -Cmd -> GUI sibling (no refusal)",
              res.get("status") == "launch_failed" and "stop-here" in res.get("message", ""),
              json.dumps(res)[:200])
        check("switched argv uses UnrealEditor.exe, not -Cmd",
              bool(captured.get("argv")) and captured["argv"][0].lower().endswith("unrealeditor.exe")
              and "-cmd" not in captured["argv"][0].lower(),
              str(captured.get("argv")))
        check("GUI launch uses DEVNULL stdin (keep_stdin_pipe False)",
              captured.get("keep_stdin_pipe") is False)
        check("cfg editor_binary was rewritten to GUI",
              cfg is not None and ue.cfg["editor_binary"].lower().endswith("unrealeditor.exe"),
              ue.cfg["editor_binary"])

        # branch B: -Cmd with NO GUI sibling -> refuse with actionable status
        cmd2 = tmp / "Engine" / "Binaries" / "Win64" / "UnrealEditor-Cmd2.exe"
        cmd2.write_bytes(b"MZ")
        gui.unlink()  # remove sibling
        ue2 = UEBridge({**cfg, "editor_binary": str(cmd2)}, "http://127.0.0.1:1/mcp")
        res2 = await ue2.launch(wait_ready=False)
        check("-Cmd with no GUI sibling -> cmd_editor_not_allowed",
              res2.get("status") == "cmd_editor_not_allowed", json.dumps(res2)[:200])
        check("refusal message tells how to fix",
              "UnrealEditor.exe" in res2.get("message", "") and "proxy__ue_configure" in res2.get("message", ""),
              res2.get("message", "")[:200])

    finally:
        ue_mod.UEBridge._editor_procs = orig_procs
        ue_mod._popen = orig_popen
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{len(FAILS)} failures: {FAILS}" if FAILS else "\n6/6 -Cmd guard checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
