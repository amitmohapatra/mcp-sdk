"""Standalone SDK unit tests — no registry required. The contract behaviors
(manifest compile, event application, seq re-sync, audience entitlement,
argument preparation, auth policies) verified against fixture data."""
import asyncio
import json

import pytest

from yourco_mcp import AuthUser, DefaultPolicy, NoAuth
from yourco_mcp.auth import scope_satisfied
from yourco_mcp.server import _CompiledManifest, ProductServer

MANIFEST = {
    "contract": "v1", "product_key": "billing", "seq": 1,
    "audiences": ["external", "internal"], "default_audience": "external",
    "channel": {"redis_url": "", "name": "registry:billing"},
    "entities": [{
        "id": "e1", "type": "tool", "name": "get_invoice", "version": 1,
        "views": {
            "external": {"enabled": True, "pins": {"max_results": 25},
                         "spec": {"name": "get_invoice", "description": "Fetch an invoice.",
                                  "input_schema": {"type": "object",
                                                   "properties": {"invoice_id": {"type": "string"}},
                                                   "required": ["invoice_id"],
                                                   "additionalProperties": False}}},
            "internal": {"enabled": True, "pins": {},
                         "spec": {"name": "get_invoice", "description": "Internal view.",
                                  "input_schema": {"type": "object",
                                                   "properties": {"invoice_id": {"type": "string"},
                                                                  "max_results": {"type": "integer",
                                                                                  "default": 100}},
                                                   "required": ["invoice_id"],
                                                   "additionalProperties": False}}},
        }}],
}


def make_server():
    s = ProductServer("http://x", "billing", "key", auth=NoAuth())
    s._swap(_CompiledManifest(json.loads(json.dumps(MANIFEST))))
    @s.tool("get_invoice")
    async def handler(ctx, invoice_id, max_results=100):
        return {"invoice_id": invoice_id, "max_results": max_results, "aud": ctx.audience}
    return s


def test_compiled_manifest_indexes_views():
    cm = _CompiledManifest(MANIFEST)
    assert cm.seq == 1 and cm.default_audience == "external"
    assert set(cm.tools["external"]) == {"get_invoice"}
    assert ("internal", "get_invoice") in cm.validators


def test_apply_event_is_pure_and_versioned():
    cm = _CompiledManifest(MANIFEST)
    ev = {"seq": 2, "type": "entity.deleted", "entity": {"type": "tool", "name": "get_invoice"}}
    cm2 = cm.apply(ev)
    assert cm2.seq == 2 and not cm2.tools.get("external")
    assert cm.seq == 1 and cm.tools["external"]          # original untouched


def test_seq_gap_and_stale_handling():
    s = make_server()
    fetches = []
    async def fake_refetch():
        fetches.append(True)
    s._refetch = fake_refetch
    asyncio.get_event_loop().run_until_complete(s.handle_event({"seq": 1, "type": "entity.updated"}))
    assert not fetches                                    # stale: ignored
    asyncio.get_event_loop().run_until_complete(s.handle_event({"seq": 5, "type": "entity.updated"}))
    assert fetches                                        # gap: full re-sync


def test_audience_entitlement_downgrade():
    s = make_server()
    anon = None
    entitled = AuthUser(id="a", scopes=["audience:internal"])
    unentitled = AuthUser(id="b", scopes=[])
    assert s.resolve_audience({}, anon) == "external"
    assert s.resolve_audience({"x-tool-audience": "internal"}, anon) == "external"
    assert s.resolve_audience({"x-tool-audience": "internal"}, unentitled) == "external"
    assert s.resolve_audience({"x-tool-audience": "internal"}, entitled) == "internal"


def test_prepare_args_strips_pins_and_validates():
    s = make_server()
    view = s._compiled.tools["external"]["get_invoice"]
    args = s._prepare_args(view, "external", "get_invoice",
                           {"invoice_id": "i1", "max_results": 999, "evil": 1})
    assert args == {"invoice_id": "i1", "max_results": 25}   # stripped + pin wins
    from yourco_mcp.server import _McpFailure
    with pytest.raises(_McpFailure):
        s._prepare_args(view, "external", "get_invoice", {})  # missing required


def test_auth_policies_and_scopes():
    d = DefaultPolicy()
    # INVARIANT: discovery is always public; execution is gated by default
    for m in ("initialize", "ping", "tools/list"):
        assert not d.requires_auth(m)
    assert d.requires_auth("tools/call")
    assert scope_satisfied(AuthUser(id="x", scopes=["*"]), ["anything"])
    assert not scope_satisfied(AuthUser(id="x", scopes=["a"]), ["a", "b"])


def test_rpc_roundtrip_from_fixture_manifest():
    s = make_server()
    async def run():
        r = await s.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, {})
        tools = r["result"]["tools"]
        assert tools[0]["name"] == "get_invoice"
        assert "max_results" not in tools[0]["inputSchema"]["properties"]
        r = await s.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                    "params": {"name": "get_invoice",
                                               "arguments": {"invoice_id": "i9"}}}, {})
        assert json.loads(r["result"]["content"][0]["text"])["max_results"] == 25
    asyncio.get_event_loop().run_until_complete(run())


def test_per_tool_public_execution():
    """Product teams choose per tool: public tools execute anonymously while
    others stay gated; registry-set scopes re-gate even a public tool."""
    from yourco_mcp.auth import StaticTokenProvider
    s = ProductServer("http://x", "billing", "key",
                      auth=StaticTokenProvider({"t": {"id": "u", "scopes": []}}))
    m = json.loads(json.dumps(MANIFEST))
    m["entities"].append({"id": "e2", "type": "tool", "name": "ping_public", "version": 1,
        "views": {"external": {"enabled": True, "pins": {},
                  "spec": {"name": "ping_public", "description": "Public ping.",
                           "input_schema": {"type": "object", "properties": {}}}}}})
    m["entities"].append({"id": "e3", "type": "tool", "name": "locked_public", "version": 1,
        "views": {"external": {"enabled": True, "pins": {},
                  "spec": {"name": "locked_public", "description": "Public but admin-scoped.",
                           "auth": {"required_scopes": ["x:y"]},
                           "input_schema": {"type": "object", "properties": {}}}}}})
    s._swap(_CompiledManifest(m))

    @s.tool("get_invoice")                       # default: auth required
    async def gi(ctx, invoice_id, max_results=100): return {"ok": 1}

    @s.tool("ping_public", public=True)          # team's choice: no auth
    async def pp(ctx): return {"pong": True, "caller": ctx.user.id}

    @s.tool("locked_public", public=True)        # public in code, scoped by admin
    async def lp(ctx): return {"secret": True}

    async def run():
        call = lambda name, hdrs=None: s.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": name, "arguments": {} if name != "get_invoice"
                        else {"invoice_id": "i"}}}, hdrs or {})
        r = await call("get_invoice")
        assert r["error"]["code"] == -32001              # gated tool: anonymous denied
        r = await call("ping_public")
        assert json.loads(r["result"]["content"][0]["text"])["pong"] is True  # public: allowed
        r = await call("locked_public")
        assert r["error"]["code"] == -32001              # admin scopes beat code-public
        r = await call("get_invoice", {"authorization": "Bearer t"})
        assert "error" not in r                          # authenticated: fine
    asyncio.get_event_loop().run_until_complete(run())


def test_unknown_contract_refused_cleanly():
    """A future registry contract must be REFUSED (fall back to snapshot),
    never mis-parsed into an empty tool list."""
    import httpx
    from yourco_mcp.client import RegistryClient, RegistryError

    class FakeTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, json={"contract": "v2", "seq": 1,
                                             "product_key": "p", "entities": []})

    c = RegistryClient("http://x", "p", "k", transport=FakeTransport())
    with pytest.raises(RegistryError, match="contract 'v2'"):
        asyncio.get_event_loop().run_until_complete(c.fetch_manifest())
