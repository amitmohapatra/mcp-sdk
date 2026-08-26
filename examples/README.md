# Examples

**payments_server.py** — the canonical product server. Shows: custom AuthProvider,
a public tool, default-gated tools, a registry-scoped tool, audience-hidden params
with fixed values, the @authorize hook, and snapshot resilience.

Registry-side setup this example expects (once, via UI or API):
product `payments` with audience `internal`, an SDK API key, and these tools:

| tool | notes |
|---|---|
| `ping` | no params |
| `get_payment_status` | param `payment_id:string*` |
| `charge_card` | params `card_id*, amount*, currency` — external audience: hide `currency`, value `USD` |
| `refund_payment` | params `payment_id*, amount*` — required scopes: `payments:write` |

Try it:

```bash
# anyone may list; the public tool works anonymously
curl -s localhost:8080/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
curl -s localhost:8080/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"ping","arguments":{}}}'

# gated tool: 401 anonymous, works with a token
curl -s localhost:8080/mcp -H 'Content-Type: application/json' -H 'Authorization: Bearer readonly-tok' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_payment_status","arguments":{"payment_id":"p1"}}}'

# scoped tool: readonly-tok -> -32003 Forbidden; finance-app-token -> works
curl -s localhost:8080/mcp -H 'Content-Type: application/json' -H 'Authorization: Bearer finance-app-token' \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"refund_payment","arguments":{"payment_id":"p1","amount":50}}}'
```
