"""yourco-mcp: build an MCP server whose tool metadata lives in the Tool Registry.

    from yourco_mcp import ProductServer
    server = ProductServer(registry_url=..., product_key=..., api_key=...)

    @server.tool("get_invoice")
    async def get_invoice(ctx, invoice_id: str): ...

Metadata (descriptions, schemas, audiences, auth scopes) is controlled in the
registry UI at runtime; this SDK syncs it live and serves stateless MCP over HTTP.
"""
from .auth import (AllGatedPolicy, ApiKeyAuthProvider, AuthPolicy, AuthProvider, AuthUser,
                   DefaultPolicy, NoAuth, StaticTokenProvider)
from .client import RegistryClient, RegistryError
from .server import ProductServer, ToolContext

__all__ = ["ProductServer", "ToolContext", "RegistryClient", "RegistryError",
           "AuthUser", "AuthProvider", "AuthPolicy", "DefaultPolicy", "AllGatedPolicy",
           "NoAuth", "ApiKeyAuthProvider", "StaticTokenProvider"]
__version__ = "0.1.0"
