"""bunny — an always-on MCP proxy for Unreal Engine 5.8's embedded MCP server.

Fixes the protocol-version negotiation deadlock between strict MCP clients
(e.g. Junie's Kotlin SDK offering 2025-03-26) and UE's ModelContextProtocol
plugin (whitelist 2025-11-25 / 2025-06-18 / 2024-11-05) by rewriting the
initialize handshake, and keeps the MCP surface alive when UE is closed by
serving a cached tool catalog plus local proxy tools (launch/build engine).
"""

__version__ = "0.1.0"
