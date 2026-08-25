# yourco-mcp — MCP SDK for the AI Registry

Build an MCP server whose tool metadata (descriptions, schemas, audiences,
auth scopes) lives in the AI Registry and hot-reloads at runtime. You write
handlers; admins manage everything else in the registry UI.

## Install

From the monorepo (local development):

```bash
pip install ./sdk/python                    # or:  pip install -e ./sdk/python
```

Straight from git (pin a tag in production):

```bash
pip install "yourco-mcp @ git+ssh://git@github.com/yourco/ai-registry.git@v0.1.0#subdirectory=sdk/python"
```

From your private index (recommended for product teams — publish with
`python -m build` + `twine upload` to Artifactory/CodeArtifact/devpi):

```bash
pip install yourco-mcp
```

Optional extras:

```bash
pip install "yourco-mcp[redis]"     # Redis pub/sub subscription (recommended in prod)
pip install "yourco-mcp[server]"    # bundled uvicorn for server.run()
```

## Quick start

```python
import os
from yourco_mcp import ProductServer

server = ProductServer(
    registry_url="https://registry.yourco.com",
    product_key="billing",                    # which product this server belongs to
    api_key=os.environ["REGISTRY_API_KEY"],   # issued in the registry UI (Manage -> SDK API keys)
)

@server.tool("get_invoice")                   # bound by NAME; metadata comes from the registry
async def get_invoice(ctx, invoice_id: str, max_results: int = 100):
    return {"invoice_id": invoice_id, "max_results": max_results}

if __name__ == "__main__":
    server.run(port=8080)                     # stateless MCP over HTTP at /mcp
```

Auth, audiences, pins, hot reload, snapshots: see the repo root README.
