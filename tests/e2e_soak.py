"""Soak test: proxy launches UE and the editor SURVIVES (stdin-EOF regression).

Historic failure: UnrealEditor-Cmd reads stdin as console commands; stdin=DEVNULL
means EOF means "quit", killing the editor ~1 min after a successful launch.

Run: .venv\\Scripts\\python.exe tests/e2e_soak.py [--minutes N]
Requires: proxy on 8765 with project configured.
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
    minutes = 2
    for i, a in enumerate(sys.argv):
        if a == "--minutes" and i + 1 < len(sys.argv):
            minutes = int(sys.argv[i + 1])
    async with httpx.AsyncClient(timeout=httpx.Timeout(340, connect=10)) as client:
        j = C()
        init = await j.call(client, "initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "soak", "version": "1"}})
        await j.notify(client, "notifications/initialized")

        async def status():
            r = await j.call(client, "tools/call", {"name": "proxy__ue_status", "arguments": {}})
            return json.loads(r["result"]["content"][0]["text"])

        st = await status()
        if st["ue_mcp_alive"]:
            r = await j.call(client, "tools/call", {"name": "proxy__ue_kill", "arguments": {"confirmed": True}})
            await asyncio.sleep(6)

        r = await j.call(client, "tools/call",
                         {"name": "proxy__ue_launch", "arguments": {"timeout_s": 300}})
        launch = json.loads(r["result"]["content"][0]["text"])
        check("launch ready", launch.get("ok") is True, json.dumps(launch)[:200])
        pid = launch.get("pid")

        # adoption must have detected this project from the RUNNING editor
        st = await status()
        adopted = str(st.get("adopted_from") or "")
        check("adoption ran against running editor (adopted_from = .uproject)",
              adopted.lower().endswith(".uproject"),
              json.dumps({k: st.get(k) for k in ("active_project", "adopted_from")}))

        # soak: editor must stay alive for `minutes`
        end = asyncio.get_event_loop().time() + minutes * 60
        samples = 0
        alive_all = True
        while asyncio.get_event_loop().time() < end:
            await asyncio.sleep(15)
            st = await status()
            samples += 1
            ok = st["ue_mcp_alive"] and (st["launched_by_proxy"] or st["editor_process_running"])
            print(f"  sample {samples}: mcp_alive={st['ue_mcp_alive']} "
                  f"launched_by_proxy={st['launched_by_proxy']} pids={st['editor_pids']}", flush=True)
            if not ok:
                alive_all = False
                break
        check(f"editor survives {minutes} min after proxy launch (pid {pid})", alive_all)

        # tools still route at the end of soak
        r = await j.call(client, "tools/call", {"name": "ue_list_toolsets", "arguments": {}})
        res = r["result"]
        txt = res["content"][0]["text"]
        check("tools route after soak", res.get("isError") is not True and "Toolset" in txt, txt[:100])

    ok = sum(1 for _, c in results if c)
    print(f"\n{ok}/{len(results)} soak checks passed")
    sys.exit(0 if ok == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
