# yourco-mcp — MCP SDK for the AI Registry

Build an MCP server whose tool **metadata lives in the AI Registry** and hot-reloads
at runtime. You write handler functions; everything else — descriptions, schemas,
audiences, per-tool scopes, exposure — is managed by admins in the registry UI and
reaches your running server in milliseconds, with no redeploy.

```
Registry (control plane)          Your server (data plane, this SDK)
  admins edit metadata   ──push──▶  in-memory manifest ──▶ answers MCP calls
  UI / RBAC / versions              your handlers      ──▶ your business logic
```

Your server never blocks on the registry: all MCP traffic is served from memory,
and if the registry is down your server keeps running (see Resilience).

## Install

```bash
pip install "yourco-mcp[server,redis] @ git+https://github.com/amitmohapatra/mcp-sdk.git"
```

Pin a tag in production (`...mcp-sdk.git@v0.1.0`). Extras: `server` bundles uvicorn
for `server.run()`; `redis` enables Redis pub/sub (recommended in prod — without it
the SDK falls back to the registry's SSE stream automatically).

## Quick start — the whole integration

```python
import os
from yourco_mcp import ProductServer

server = ProductServer(
    registry_url="https://registry.yourco.com",
    product_key="billing",                    # your product's key in the registry
    api_key=os.environ["REGISTRY_API_KEY"],   # issued in the UI: Manage -> SDK API keys
)

@server.tool("get_invoice")                   # bound by NAME — metadata comes from the registry
async def get_invoice(ctx, invoice_id: str, max_results: int = 100):
    return {"invoice_id": invoice_id, "max_results": max_results}

if __name__ == "__main__":
    server.run(port=8080)                     # stateless MCP over HTTP at POST /mcp
```

Notice what is **absent**: no descriptions, no JSON schemas, no Redis config, no auth
boilerplate. The registry owns metadata; your code owns behavior. If the registry
lists a tool you have no handler for, it is excluded from `tools/list` with a warning
(fail-safe, never fail-crash).

## Authentication — you own identity, the SDK owns enforcement

**Each product handles auth for its own tools.** The SDK never sees your passwords,
keys, or token formats — you implement exactly one method: *headers in, user out.*

```python
from yourco_mcp import ProductServer, AuthProvider, AuthUser

class MyProductAuth(AuthProvider):
    async def authenticate(self, headers) -> AuthUser | None:
        token = headers.get("authorization", "").removeprefix("Bearer ")
        claims = my_jwt_verify(token)          # YOUR auth: your JWT lib, your OAuth
        if not claims:                         # introspection, your session store
            return None
        return AuthUser(id=claims["sub"], scopes=claims.get("scopes", []))

server = ProductServer(..., auth=MyProductAuth())
```

A plain `async def fn(headers) -> AuthUser | None` works too.

**Real-world example — Firebase bearer + roles from your DB:**

```python
import asyncio
import firebase_admin
from firebase_admin import auth as fb_auth
from yourco_mcp import AuthProvider, AuthUser

firebase_admin.initialize_app()                      # your service account creds

class FirebaseAuth(AuthProvider):
    async def authenticate(self, headers) -> AuthUser | None:
        token = headers.get("authorization", "").removeprefix("Bearer ").strip()
        try:                                          # verify_id_token is blocking:
            decoded = await asyncio.to_thread(fb_auth.verify_id_token, token)
        except Exception:
            return None
        roles = await my_db.fetch_roles(decoded["uid"])       # YOUR roles table
        return AuthUser(id=decoded["uid"],
                        scopes=[f"role:{r}" for r in roles],   # roles become scopes
                        claims=decoded)
```

Then require roles **per tool, right at the decorator**:

```python
@server.tool("refund_payment", scopes=["role:finance-admin"])
async def refund_payment(ctx, payment_id: str, amount: float): ...
```

Code-declared `scopes` enforce in **union** with registry-set `required_scopes`
— either side may tighten a tool, neither can loosen the other. Built-ins:
`ApiKeyAuthProvider({key: {...}})`, `StaticTokenProvider({token: {...}})`, and
`NoAuth()` — the **explicit** opt-out for genuinely open servers (nothing is ever
open by accident).

**The contract in one line:** *discovery is always public; everything about
execution is your product's pluggable choice.*

- **`tools/list` (and initialize/ping) never require auth** — an invariant, not
  a default. Gateways and catalogs (e.g. Bifrost) can enumerate every product's
  tools with zero credentials. Anonymous callers see the default audience's view.
- **Authentication is pluggable**: your `AuthProvider` — or `NoAuth()` for a
  fully open server (an explicit choice, never an accident).
- **Authorization is pluggable**: your scopes, minted by your auth system,
  checked against registry-set `required_scopes` per tool, plus your
  `@server.authorize` hook for anything scopes can't express.
- **Per-tool execution auth is your choice**:

```python
@server.tool("ping", public=True)          # executes without auth
async def ping(ctx): ...

@server.tool("refund_payment")             # gated (the default)
async def refund(ctx, payment_id: str): ...
```

Safety rule: if an admin attaches `required_scopes` to a tool in the registry,
auth is required again **even if the code marks it public** — runtime
tightening always wins; the code-side opt-out can never override it.

Once your verifier exists, the SDK enforces — you write none of this:

| Layer | Behavior | You configure it… |
|---|---|---|
| Default policy | `tools/list` open; `tools/call` requires an authenticated user (`-32001` otherwise) | never (or swap `policy=AllGatedPolicy()`) |
| Audience entitlement | `x-tool-audience: internal` honored only if the user's scopes include `audience:internal`; everyone else is silently downgraded to the default audience | by which scopes your auth mints |
| Per-tool scopes | a tool with `required_scopes: ["payments:write"]` in the registry rejects callers without that scope (`-32003`) — admins tighten this at runtime, no redeploy | in the registry UI |
| Business rules | arbitrary code check after the scope checks | `@server.authorize` hook |

```python
@server.authorize
async def gate(user, tool, args) -> bool:
    return not (tool == "refund_payment" and args["amount"] > 10_000
                and "payments:admin" not in user.scopes)
```

**Company scope conventions** (align once, org-wide):
- `audience:<key>` — grants an audience (e.g. `audience:internal` for internal agents)
- `<domain>:<action>` — per-tool requirements admins set in the registry
  (e.g. `payments:write`, `invoices:read`)

## Audiences, hidden parameters, fixed values

Admins can expose one tool differently per audience (e.g. `external` vs `internal`):
different descriptions, extra internal-only parameters, or parameters that are
**hidden** from an audience with a **fixed value sent to your handler** instead —
callers can never see or override it. Your handler just declares the parameter with
a default; the SDK validates arguments against the caller's audience schema, strips
unknown arguments, and injects fixed values before your code runs.

```python
@server.tool("charge_card")
async def charge_card(ctx, card_id: str, amount: float, currency: str = "USD"):
    # external callers can't even see `currency` — the SDK always passes the
    # admin-fixed value; internal callers control it. ctx.audience tells you which.
    ...
```

`ctx` gives you `ctx.user` (the AuthUser), `ctx.audience`, and `ctx.tool`.

## Live updates — how a registry save reaches your server

1. Admin saves in the registry → one transaction bumps the product's sequence number
   and publishes an event carrying the already-resolved views.
2. Your server (subscribed since startup — Redis if your product has it configured,
   the registry's SSE stream otherwise; the manifest tells the SDK which) receives it.
3. Sequence check: next-in-order → applied as an atomic manifest swap; stale →
   ignored; a gap → full re-fetch and reconcile. Convergence is guaranteed.
4. The next `tools/list`/`tools/call` serves the new metadata. Typical latency:
   **single-digit milliseconds** (Redis) to a few hundred ms (SSE).

## Resilience

- **Registry down** → your server keeps serving from memory, including the latest
  applied update. The registry is a control plane, never a runtime dependency.
- **Cold start while registry is down** → served from a last-known-good snapshot
  the SDK maintains automatically (cached under `~/.cache/yourco-mcp/`; override
  the location with `YOURCO_MCP_CACHE_DIR` if your runtime needs it).
- **Bad/malformed update** → logged, ignored, re-synced; a good manifest is never
  replaced by a broken one.
- **Stateless HTTP** → run N replicas behind any load balancer; no sticky sessions.

## Checklist for a new product team

1. Ask a registry admin to onboard your product and hand you an **API key**.
2. `pip install` (above), set `REGISTRY_API_KEY` in your deployment env.
3. Write handlers for the tools your product owns (names must match the registry).
4. Wire your existing auth into one `AuthProvider.authenticate` method.
5. Decide which of your tokens carry `audience:*` scopes (internal agents etc.).
6. `server.run()` — verify with `curl localhost:8080/healthz` and an MCP
   `tools/list`. Edit a description in the registry UI and watch it change live.
