"""Real end-to-end: proxy -> launch real UE -> cache -> call real tools.

Run with the proxy already listening (see bunny.bat / README), then:
    .venv\\Scripts\\python.exe tests/e2e_real.py [--kill-when-done]
"""
from __future__ import annotations

import asyncio
import json
import sys

import httpx

PROXY = "http://127.0.0.1:8765"


class C:
    def __init__(self, proto="2025-03-26"):
        self.proto = proto
        self.sid = None
        self.id = 0

    async def call(self, client, method, params=None):
        self.id += 1
        payload = {"jsonrpc": "2.0", "id": self.id, "method": method}
        if params is not None:
            payload["params"] = params
        headers = {"Accept": "application/json, text/event-stream",
                   "mcp-protocol-version": self.proto}
        if self.sid:
            headers["mcp-session-id"] = self.sid
        r = await client.post(f"{PROXY}/mcp", json=payload, headers=headers)
        if self.sid is None and r.headers.get("mcp-session-id"):
            self.sid = r.headers.get("mcp-session-id")
        if r.status_code == 202:
            return None
        return r.json()

    async def notify(self, client, method):
        headers = {"Accept": "application/json, text/event-stream",
                   "mcp-protocol-version": self.proto}
        if self.sid:
            headers["mcp-session-id"] = self.sid
        await client.post(f"{PROXY}/mcp", json={"jsonrpc": "2.0", "method": method}, headers=headers)


async def main():
    kill_at_end = "--kill-when-done" in sys.argv
    results = []

    def check(name, cond, detail=""):
        results.append((name, cond))
        print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))

    async with httpx.AsyncClient(timeout=httpx.Timeout(330, connect=10)) as client:
        junie = C()
        init = await junie.call(client, "initialize", {
            "protocolVersion": junie.proto, "capabilities": {},
            "clientInfo": {"name": "e2e-junie-sim", "version": "1"}})
        check("initialize (2025-03-26) negotiated echo",
              init["result"]["protocolVersion"] == "2025-03-26", json.dumps(init)[:200])
        await junie.notify(client, "notifications/initialized")

        st = await junie.call(client, "tools/call", {"name": "proxy__ue_status", "arguments": {}})
        status = json.loads(st["result"]["content"][0]["text"])
        print("status:", json.dumps(status, indent=1)[:600])
        check("proxy__ue_status callable", "ue_mcp_alive" in status)

        if not status["ue_mcp_alive"]:
            print("launching real UE 5.8 via proxy__ue_launch (this can take minutes)...")
            r = await junie.call(client, "tools/call",
                                 {"name": "proxy__ue_launch", "arguments": {"timeout_s": 300}})
            launch = json.loads(r["result"]["content"][0]["text"])
            print("launch:", json.dumps(launch, indent=1)[:800])
            check("proxy__ue_launch reached ready", launch.get("ok") is True
                  and launch.get("status") in ("ready", "already_running"),
                  json.dumps(launch)[:300])

        tl = await junie.call(client, "tools/list", {})
        names = [t["name"] for t in tl["result"]["tools"]]
        ue_names = [n for n in names if n.startswith("ue_")]
        print(f"tools listed: {len(names)} total, {len(ue_names)} UE: {ue_names[:8]}")
        check("real UE tools cached & listed", len(ue_names) >= 3)

        # real, read-only UE calls
        r = await junie.call(client, "tools/call", {"name": "ue_list_toolsets", "arguments": {}})
        res = r["result"]
        txt = res["content"][0]["text"]
        print("list_toolsets ->", txt[:400].replace("\n", " "))
        check("ue_list_toolsets executed for real",
              res.get("isError") is not True and "ue_online" not in txt and len(txt) > 20)

        r = await junie.call(client, "tools/call",
                             {"name": "ue_describe_toolset", "arguments": {"toolset_name": "EditorToolset"}})
        txt2 = r["result"]["content"][0]["text"]
        print("describe_toolset ->", txt2[:300].replace("\n", " "))
        check("ue_describe_toolset executed for real", len(txt2) > 50)

        stats = await junie.call(client, "tools/call", {"name": "proxy__cache_stats", "arguments": {}})
        body = json.loads(stats["result"]["content"][0]["text"])
        print("cache stats:", json.dumps(body["config"], indent=1))
        check("cache stats reflect real catalog", body["tools"] >= 3)

        if kill_at_end:
            r = await junie.call(client, "tools/call",
                                 {"name": "proxy__ue_kill", "arguments": {"confirmed": True}})
            print("kill:", r["result"]["content"][0]["text"][:200])

    ok = sum(1 for _, c in results if c)
    print(f"\n{ok}/{len(results)} real E2E checks passed")
    sys.exit(0 if ok == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
