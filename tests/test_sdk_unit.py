"""Standalone SDK unit tests — no registry required. The contract behaviors
(manifest compile, event application, seq re-sync, audience entitlement,
argument preparation, auth policies) verified against fixture data."""
import asyncio
import json

import pytest

from yourco_mcp import AuthUser, DefaultPolicy, AllGatedPolicy, NoAuth
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
    d, g = DefaultPolicy(), AllGatedPolicy()
    assert not d.requires_auth("tools/list") and d.requires_auth("tools/call")
    assert g.requires_auth("tools/list") and not g.requires_auth("ping")
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
