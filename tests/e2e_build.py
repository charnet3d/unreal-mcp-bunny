"""E2E: proxy__ue_build with UE closed -> build_ok -> proxy AUTO-relaunches UE.

Run: .venv\\Scripts\\python.exe tests/e2e_build.py
Requires: proxy on 8765, UE closed, project+build configured.
"""
from __future__ import annotations

import asyncio
import json
import sys

import httpx

sys.path.insert(0, "tests")
from e2e_real import C  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""), flush=True)


async def main():
    async with httpx.AsyncClient(timeout=httpx.Timeout(2000, connect=10)) as client:
        j = C()
        await j.call(client, "initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "build-e2e", "version": "1"}})
        await j.notify(client, "notifications/initialized")

        async def status():
            r = await j.call(client, "tools/call", {"name": "proxy__ue_status", "arguments": {}})
            return json.loads(r["result"]["content"][0]["text"])

        st = await status()
        check("precondition: UE closed, can_build true",
              not st["ue_mcp_alive"] and st["can_build"], json.dumps(st)[:300])
        if st["ue_mcp_alive"]:
            r = await j.call(client, "tools/call", {"name": "proxy__ue_kill", "arguments": {"confirmed": True}})
            await asyncio.sleep(8)

        print("proxy__ue_build (blocking; auto_launch_after_build default ON)...", flush=True)
        r = await j.call(client, "tools/call", {"name": "proxy__ue_build", "arguments": {}})
        body = json.loads(r["result"]["content"][0]["text"])
        print(json.dumps({k: body.get(k) for k in ("ok", "status", "seconds", "exit_code")}), flush=True)
        check("blocking build ok", body.get("ok") is True and body.get("status") == "build_ok",
              json.dumps(body)[:300])
        auto = body.get("auto_launch") or {}
        check("auto_launch present + ok (UE relaunched by proxy itself)",
              auto.get("ok") is True, json.dumps(body.get("auto_launch"))[:250])
        check("auto_launch adopted project", (auto.get("adopted") or {}).get("ok") is True,
              json.dumps(auto)[:250])
        check("catalog refreshed after auto-launch",
              (body.get("catalog") or {}).get("ok") is True, json.dumps(body.get("catalog"))[:200])

        # tools route live now, in the same MCP session, with zero human steps
        r = await j.call(client, "tools/call", {"name": "ue_list_toolsets", "arguments": {}})
        res = r["result"]
        txt = res["content"][0]["text"]
        check("ue_list_toolsets routes live after auto-launch (same session)",
              res.get("isError") is not True and "Toolset" in txt, txt[:120])

        # Live Coding toolset visible for in-place compiles while UE is open
        has_lc = "LiveCoding" in txt
        check("LiveCodingToolset visible in UE toolsets (UE-open compiles via ue_* tools)", has_lc, txt[:400])

    ok = sum(1 for _, c in results if c)
    print(f"\n{ok}/{len(results)} build-chain checks passed")
    sys.exit(0 if ok == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
