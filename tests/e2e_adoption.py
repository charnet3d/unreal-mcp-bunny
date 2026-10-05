"""Adoption E2E: launch via proxy -> adopted.json records the exact .uproject.

Run: .venv\\Scripts\\python.exe tests/e2e_adoption.py
Requires: proxy on 8765.
"""
from __future__ import annotations

import asyncio
import json
import sys

import httpx

sys.path.insert(0, "tests")
from e2e_real import C  # noqa: E402

ADOPTED = "data/adopted.json"
results = []


def check(name, cond, detail=""):
    results.append((name, cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""), flush=True)


async def main():
    async with httpx.AsyncClient(timeout=httpx.Timeout(340, connect=10)) as client:
        j = C()
        await j.call(client, "initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "adopt", "version": "1"}})
        await j.notify(client, "notifications/initialized")

        async def status():
            r = await j.call(client, "tools/call", {"name": "proxy__ue_status", "arguments": {}})
            return json.loads(r["result"]["content"][0]["text"])

        st = await status()
        if not st["ue_mcp_alive"]:
            r = await j.call(client, "tools/call",
                             {"name": "proxy__ue_launch", "arguments": {"timeout_s": 300}})
            launch = json.loads(r["result"]["content"][0]["text"])
            check("proxy__ue_launch ready", launch.get("ok") is True, json.dumps(launch)[:200])

        st = await status()
        up = str(st.get("active_project") or "")
        ad = str(st.get("adopted_from") or "")
        check("status.active_project is a bare .uproject (no exe prefix)",
              up.lower().endswith(".uproject") and " " not in up.strip(),
              repr(up))
        check("status.adopted_from is a bare .uproject", ad.lower().endswith(".uproject") and " " not in ad.strip(),
              repr(ad))
        check("active_engine is the UE 5.8 root", "UE_5.8" in str(st.get("active_engine")), repr(st.get("active_engine")))

        with open(ADOPTED, encoding="utf-8") as f:
            doc = json.load(f)
        check("adopted.json project_path is a bare .uproject",
              doc["project_path"].lower().endswith(".uproject") and " " not in doc["project_path"].strip(),
              repr(doc["project_path"]))
        check("adopted.json build_target derived", doc.get("build_target", "").endswith("Editor"),
              repr(doc.get("build_target")))

    ok = sum(1 for _, c in results if c)
    print(f"\n{ok}/{len(results)} adoption checks passed")
    sys.exit(0 if ok == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
