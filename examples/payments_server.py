"""Example product MCP server: "payments".

Everything a product team writes — nothing more. Descriptions, schemas,
audiences, hidden/fixed params, per-tool scopes: managed in the AI Registry
UI and hot-reloaded here at runtime.

Run:
    export REGISTRY_API_KEY=trk_...        # from registry UI: Manage -> SDK API keys
    python payments_server.py
"""
import os

from yourco_mcp import AuthProvider, AuthUser, ProductServer

# ---------------------------------------------------------------------------
# 1. YOUR auth. One method: headers in -> user out. This example uses a static
#    token map so it runs out of the box; a real team replaces the body with
#    their existing verification (their JWT lib / OAuth introspection / mTLS):
#
#        claims = jwt.decode(token, PUBLIC_KEY, algorithms=["RS256"])
#        return AuthUser(id=claims["sub"], scopes=claims.get("scopes", []))
# ---------------------------------------------------------------------------
class PaymentsAuth(AuthProvider):
    TOKENS = {
        "finance-app-token":  {"id": "finance-app",  "scopes": ["payments:write"]},
        "internal-agent-tok": {"id": "agent-billing", "scopes": ["audience:internal",
                                                                 "payments:write"]},
        "readonly-tok":       {"id": "dashboard",     "scopes": []},
    }

    async def authenticate(self, headers) -> AuthUser | None:
        token = headers.get("authorization", "").removeprefix("Bearer ").strip()
        info = self.TOKENS.get(token)
        return AuthUser(id=info["id"], scopes=info["scopes"]) if info else None


server = ProductServer(
    registry_url=os.environ.get("REGISTRY_URL", "http://localhost:8000"),
    product_key="payments",
    api_key=os.environ["REGISTRY_API_KEY"],
    auth=PaymentsAuth(),
    snapshot_path="/var/tmp/payments.snapshot.json",   # survives registry outages
)

# ---------------------------------------------------------------------------
# 2. Handlers — bound to registry tools BY NAME. Signatures mirror the base
#    schema; audience-added params just need defaults.
# ---------------------------------------------------------------------------

@server.tool("ping", public=True)                # team's choice: no auth for this one
async def ping(ctx):
    return {"pong": True}


@server.tool("get_payment_status")               # default: any authenticated caller
async def get_payment_status(ctx, payment_id: str):
    return {"payment_id": payment_id, "status": "settled",
            "served_to": ctx.user.id, "audience": ctx.audience}


@server.tool("charge_card")
async def charge_card(ctx, card_id: str, amount: float, currency: str = "USD"):
    # In the registry, `currency` is hidden from the external audience with a
    # fixed value "USD" — external callers never see it and cannot override it;
    # internal callers (scope audience:internal) control it.
    return {"charged": amount, "currency": currency, "card": card_id}


@server.tool("refund_payment")
async def refund_payment(ctx, payment_id: str, amount: float):
    # Admins attached required_scopes ["payments:write"] in the registry -> the
    # SDK rejects callers without that scope before this line ever runs.
    return {"refunded": amount, "payment_id": payment_id, "by": ctx.user.id}


@server.authorize                                # optional: rules scopes can't express
async def business_rules(user, tool, args) -> bool:
    if tool == "refund_payment" and args.get("amount", 0) > 10_000:
        return "payments:admin" in user.scopes   # step-up for big refunds
    return True


if __name__ == "__main__":
    server.run(port=int(os.environ.get("PORT", "8080")))
