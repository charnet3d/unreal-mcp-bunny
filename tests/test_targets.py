"""Target-discovery tests: build targets come from Source/*.Target.cs, not from
the .uproject name (a sample-style project has only Lyra* targets). No host state."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bunny.config import discover_targets, pick_target  # noqa: E402

passed = total = 0


def check(name, cond, detail=""):
    global passed, total
    total += 1
    if cond:
        passed += 1
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name} {detail}")


tmp = Path(tempfile.mkdtemp(prefix="bunny_targets_"))


def mkproject(name, targets):
    """targets: list of (file_stem, TargetType|None)"""
    root = tmp / name
    src = root / "Source"
    src.mkdir(parents=True)
    (root / f"{name}.uproject").write_text("{}", encoding="utf-8")
    for stem, ttype in targets:
        body = "public class X : TargetRules\n{\n"
        if ttype:
            body += f"\tpublic XTarget() {{ Type = TargetType.{ttype}; }}\n"
        body += "}\n"
        (src / f"{stem}.Target.cs").write_text(body, encoding="utf-8")
    return str(root / f"{name}.uproject")


# 1) Lyra-style: name mismatch — the reported bug
lyra = mkproject("SampleGameV2", [
    ("LyraEditor", "Editor"), ("LyraGame", "Game"),
    ("LyraClient", "Client"), ("LyraServer", "Server"),
    ("LyraGameSteam", None),
])
ts = discover_targets(lyra)
check("scan finds Lyra targets", {t["name"] for t in ts} ==
      {"LyraEditor", "LyraGame", "LyraClient", "LyraServer", "LyraGameSteam"}, ts)
check("target names drop the .Target suffix",
      all("." not in t["name"] for t in ts), ts)
check("Editor type detected from Target.cs",
      next(t for t in ts if t["name"] == "LyraEditor")["type"] == "Editor")
check("pick: project name yields NO phantom <Stem>Editor",
      pick_target({}, lyra) == ("LyraEditor", "scan"))
check("pick: prefers the Editor target",
      pick_target({"build_target": ""}, lyra)[0] == "LyraEditor")
check("pick: stale persisted target from another project is ignored",
      pick_target({"build_target": "OtherGameEditor"}, lyra)[0] == "LyraEditor")
check("pick: persisted target that THIS project declares wins",
      pick_target({"build_target": "LyraGame"}, lyra) == ("LyraGame", "persisted"))

# 2) Epic convention: <Stem>Editor exists
conv = mkproject("MyGame", [("MyGameEditor", "Editor"), ("MyGame", "Game")])
check("convention target wins when present",
      pick_target({}, conv) == ("MyGameEditor", "convention"))
check("no targets declared -> <Stem>Editor fallback",
      pick_target({}, mkproject("Empty", [])) == ("EmptyEditor", "fallback"))
check("no Source dir at all -> <Stem>Editor fallback",
      pick_target({}, str(tmp / "Nope" / "Nope.uproject"))[1] == "fallback")

# 3) Editor-target preference when stem matches nothing
odd = mkproject("Zeta", [("OmegaEditor", "Editor"), ("Omega", "Game")])
check("falls back to any Editor target (build what the editor needs)",
      pick_target({}, odd) == ("OmegaEditor", "scan"))

# 4) stem partial-match beats a foreign editor target
named = mkproject("Zeta2", [("Zeta2ThingsEditor", "Editor"), ("Omega2Editor", "Editor")])
check("stem-named editor target preferred",
      pick_target({}, named)[0] == "Zeta2ThingsEditor")

# 5) derive_target follows the active project
from bunny.config import derive_target  # noqa: E402
check("derive_target scans the ACTIVE project (not persisted-blind)",
      derive_target({"project_path": lyra, "build_target": ""}) == "LyraEditor")
check("derive_target with no project keeps persisted",
      derive_target({"project_path": "", "build_target": "FooEditor"}) == "FooEditor")

print(f"\n{passed}/{total} target checks passed")
sys.exit(0 if passed == total else 1)
