"""Demo product MCP server built on the yourco_mcp SDK.

Run:  REGISTRY_API_KEY=trk_... python examples/billing_server.py
Tool metadata (descriptions, audiences, pins) comes from the Tool Registry and
hot-reloads live; only the handlers live here.
"""
import os

from yourco_mcp import ProductServer, StaticTokenProvider

server = ProductServer(
    registry_url=os.environ.get("REGISTRY_URL", "http://localhost:8000"),
    product_key=os.environ.get("PRODUCT_KEY", "billing"),
    api_key=os.environ["REGISTRY_API_KEY"],
    auth=StaticTokenProvider({
        "internal-agent-token": {"id": "internal-agent",
                                 "scopes": ["audience:internal", "payments:write"]},
    }),
)


@server.tool("get_invoice")
async def get_invoice(ctx, invoice_id: str, max_results: int = 100, include_ledger: bool = False):
    return {"invoice_id": invoice_id, "max_results": max_results,
            "include_ledger": include_ledger, "served_to_audience": ctx.audience}


if __name__ == "__main__":
    server.run(port=int(os.environ.get("PORT", "8080")))
