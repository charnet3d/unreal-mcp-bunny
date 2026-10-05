"""Real lifecycle E2E: proxy kills UE -> proxy LAUNCHES UE -> tools route -> UE closed -> offline mode.

Run: .venv\\Scripts\\python.exe tests/e2e_lifecycle.py [--keep-ue]
Requires: proxy running on 127.0.0.1:8765 with project configured.
"""
from __future__ import annotations

import asyncio
import json
import sys

import httpx

PROXY = "http://127.0.0.1:8765"
sys.path.insert(0, "tests")
from e2e_real import C  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""),
          flush=True)


async def main():
    keep_ue = "--keep-ue" in sys.argv
    async with httpx.AsyncClient(timeout=httpx.Timeout(340, connect=10)) as client:
        j = C()
        init = await j.call(client, "initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "lifecycle", "version": "1"}})
        check("initialize echoes 2025-03-26", init["result"]["protocolVersion"] == "2025-03-26")
        await j.notify(client, "notifications/initialized")

        async def status():
            r = await j.call(client, "tools/call", {"name": "proxy__ue_status", "arguments": {}})
            return json.loads(r["result"]["content"][0]["text"])

        # 1. ensure UE closed: kill (idempotent)
        r = await j.call(client, "tools/call", {"name": "proxy__ue_kill", "arguments": {"confirmed": True}})
        print("kill:", r["result"]["content"][0]["text"][:150], flush=True)
        await asyncio.sleep(6)
        st = await status()
        check("UE closed after kill", st["ue_mcp_alive"] is False, json.dumps(st)[:200])

        # 2. offline mode: tools listed, calls actionable, initialize ok
        tl = await j.call(client, "tools/list", {})
        ue_names = [t["name"] for t in tl["result"]["tools"] if t["name"].startswith("ue_")]
        check("cached ue_ tools listed offline", len(ue_names) >= 3, str(ue_names))
        r = await j.call(client, "tools/call", {"name": "ue_list_toolsets", "arguments": {}})
        res = r["result"]
        check("offline ue_ call -> actionable isError with launch hint",
              res.get("isError") is True and "proxy__ue_launch" in res["content"][0]["text"])

        # 3. proxy launches REAL UE
        print("proxy__ue_launch (real editor; takes minutes)...", flush=True)
        r = await j.call(client, "tools/call",
                         {"name": "proxy__ue_launch", "arguments": {"timeout_s": 300}})
        launch = json.loads(r["result"]["content"][0]["text"])
        print("launch:", json.dumps(launch)[:400], flush=True)
        check("proxy__ue_launch ok (WinError 87 fixed, editor spawned + ready)",
              launch.get("ok") is True and launch.get("status") in ("ready", "already_running"),
              json.dumps(launch)[:250])
        st = await status()
        check("status: launched_by_proxy true", st["launched_by_proxy"] is True, json.dumps(st)[:250])

        # 4. tools route for real
        tl = await j.call(client, "tools/list", {})
        ue_names = [t["name"] for t in tl["result"]["tools"] if t["name"].startswith("ue_")]
        check("ue_ tools listed live", len(ue_names) >= 3)
        r = await j.call(client, "tools/call", {"name": "ue_list_toolsets", "arguments": {}})
        res = r["result"]
        txt = res["content"][0]["text"]
        check("ue_list_toolsets live -> real toolset list",
              res.get("isError") is not True and "Toolset" in txt, txt[:120])

        # 5. cache stats + resources surface
        r = await j.call(client, "tools/call", {"name": "proxy__cache_stats", "arguments": {}})
        body = json.loads(r["result"]["content"][0]["text"])
        check("cache stats: >=3 real tools", body["tools"] >= 3)

        # 6. close UE -> proxy survives, same session keeps working
        if not keep_ue:
            r = await j.call(client, "tools/call",
                             {"name": "proxy__ue_kill", "arguments": {"confirmed": True}})
            await asyncio.sleep(6)
            st = await status()
            check("after kill: proxy session alive, UE down", st["ue_mcp_alive"] is False)
            tl2 = await j.call(client, "tools/list", {})
            check("same session lists cached tools after engine died",
                  any(t["name"].startswith("ue_") for t in tl2["result"]["tools"]))

    ok = sum(1 for _, c in results if c)
    print(f"\n{ok}/{len(results)} lifecycle checks passed")
    sys.exit(0 if ok == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
