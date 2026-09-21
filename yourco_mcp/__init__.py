"""yourco-mcp: MCP tooling whose metadata is owned by the AI Registry.

Three doors. Take the one that fits what you already have — they are independent, and none
of them requires the others.

**Starting fresh** — the whole thing::

    from yourco_mcp import ProductServer            # REGISTRY_URL / _PRODUCT_KEY / _API_KEY

    server = ProductServer()
    @server.tool("get_invoice")
    async def get_invoice(ctx, invoice_id: str): ...
    server.run(port=8080)

**You already have an MCP server** (FastMCP, the official SDK, your own) — take the metadata
and the enforcement, keep your server::

    from yourco_mcp import RegistryClient, ToolCatalog

    catalog = ToolCatalog(await RegistryClient(url, product, key).fetch_or_snapshot())
    for tool in catalog.tools_for(audience):        # descriptions, schemas, audiences
        my_server.add_tool(**tool)
    args = catalog.prepare(name, audience, caller_args)   # strip, default, validate, pin

**You only want the authorization model** — scopes as a union of registry-set and
code-declared, where either side may tighten and neither can loosen::

    from yourco_mcp import AuthUser, scope_satisfied

    if not scope_satisfied(user, catalog.required_scopes(name, audience)):
        raise PermissionError

Metadata — descriptions, schemas, audiences, scopes, pinned parameters — is edited in the
registry UI and reaches a running server in seconds, with no redeploy. Your code owns
behaviour; the registry owns everything else.
"""
from .auth import (ApiKeyAuthProvider, AuthPolicy, AuthProvider, AuthUser,
                   CallableProvider, DefaultPolicy, NoAuth, StaticTokenProvider,
                   scope_satisfied)
from .client import RegistryClient, RegistryError
from .server import InvalidArguments, ProductServer, ToolCatalog, ToolContext

__all__ = ["ProductServer", "ToolContext", "ToolCatalog", "InvalidArguments",
           "RegistryClient", "RegistryError",
           "AuthUser", "AuthProvider", "AuthPolicy", "DefaultPolicy",
           "NoAuth", "ApiKeyAuthProvider", "StaticTokenProvider", "CallableProvider",
           "scope_satisfied"]
__version__ = "0.5.0"
