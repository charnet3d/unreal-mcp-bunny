"""End-to-end check with the OFFICIAL MCP Python SDK client (mcp.client.streamable_http).

This is the same SDK family real harnesses use, so it validates header
discipline, session handling, and version negotiation the way a strict client does.
Run: python tests/test_sdk_client.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from mcp import ClientSession  # noqa: E402

from test_proxy import FakeUE, ServerThread, ue_app, make_cfg  # noqa: E402


async def main():
    tmp = ROOT / "data" / "testrun_sdk"
    tmp.mkdir(parents=True, exist_ok=True)

    ue = FakeUE()
    ue_t = ServerThread(ue_app(ue), 8131)
    ue_t.start()

    cfg = make_cfg(8131, tmp)
    from bunny.server import build_app

    app, state = build_app(cfg)
    proxy_t = ServerThread(app, 8132)
    proxy_t.start()
    await asyncio.sleep(1.5)

    url = "http://127.0.0.1:8132/mcp"
    results = []

    # The SDK client defaults to the newest protocol it speaks; that is exactly the
    # situation where a strict client used to die against UE.
    async with streamable_http_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            results.append(("SDK initialize succeeds", True))
            proto = getattr(init, "protocolVersion", None) or getattr(init, "protocol_version", None)
            sinfo = getattr(init, "serverInfo", None) or getattr(init, "server_info", None)
            print(f"SDK negotiated protocol: {proto}")
            print(f"SDK serverInfo: {sinfo.name} v{sinfo.version}")
            results.append(("SDK negotiated a version the SDK supports",
                            proto in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28")))

            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print(f"tools: {names}")
            results.append(("SDK lists proxy tools", any(n.startswith("proxy__") for n in names)))
            results.append(("SDK lists cached UE tools", any(n.startswith("ue_") for n in names)))

            r = await session.call_tool("proxy__ue_status", {})
            body = json.loads(r.content[0].text)
            results.append(("proxy__ue_status live via SDK", body["ue_mcp_alive"] is True))
            print("ue_mcp_alive:", body["ue_mcp_alive"], "cached_tools:", body["cached_tools"])

            r = await session.call_tool("ue_call_tool", {"toolset": "EditorToolset", "tool": "ping"})
            txt = r.content[0].text
            results.append(("ue_ call routes via SDK", "ok" in txt))

            res = await session.list_resources()
            results.append(("SDK lists cached resources", len(res.resources) == 1))
            prompts = await session.list_prompts()
            results.append(("SDK lists cached prompts", len(prompts.prompts) == 1))

    # now kill UE and reconnect a NEW SDK session: must succeed, tools visible
    ue_t.stop()
    await asyncio.sleep(2.0)
    async with streamable_http_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            results.append(("SDK reconnect with UE closed", True))
            results.append(("SDK sees cached ue_ tools offline",
                            any(n.startswith("ue_") for n in names)))
            r = await session.call_tool("ue_call_tool", {})
            results.append(("offline ue_ call returns isError (not a crash)",
                            bool(getattr(r, "isError", None) or getattr(r, "is_error", False))))
            print("offline call text:", r.content[0].text[:120].replace("\n", " "))

    proxy_t.stop()
    await asyncio.sleep(0.5)

    ok = sum(1 for _, c in results if c)
    for name, c in results:
        print(f"{'PASS' if c else 'FAIL'}  {name}")
    print(f"\n{ok}/{len(results)} SDK-client checks passed")
    sys.exit(0 if ok == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
