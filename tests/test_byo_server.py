"""The bring-your-own-server path: registry metadata and enforcement, without ProductServer.

A team that already runs an MCP server should be able to take the registry's metadata, its
audience resolution and its argument preparation, and keep their own transport. These tests
pin that those pieces work standalone — because if they only work inside ProductServer, then
"use the registry" means "rewrite your server", which is the adoption cliff this SDK exists
to avoid.
"""
import os

import pytest

from yourco_mcp import (AuthUser, InvalidArguments, ProductServer, ToolCatalog,
                        scope_satisfied)
from tests.test_sdk_unit import MANIFEST


def catalog() -> ToolCatalog:
    return ToolCatalog(MANIFEST)


# ----------------------------------------------------------------- metadata, standalone


def test_a_catalog_needs_no_server() -> None:
    """The whole point: construct it from a manifest dict and nothing else."""
    assert catalog().seq == MANIFEST["seq"]


def test_tools_for_an_audience_are_in_mcp_shape() -> None:
    """Handed straight to another server's add_tool; no translation step to get wrong."""
    tools = catalog().tools_for("external")
    assert [t["name"] for t in tools] == ["get_invoice"]
    assert set(tools[0]) == {"name", "title", "description", "inputSchema", "annotations"}
    assert tools[0]["description"] == "Fetch an invoice."


def test_each_audience_sees_its_own_resolved_view() -> None:
    """The reason audiences exist. An internal caller gets the internal description."""
    assert catalog().tools_for("internal")[0]["description"] == "Internal view."


def test_an_unknown_audience_offers_nothing_rather_than_everything() -> None:
    """A misspelled audience must not be a way to see tools you are not entitled to."""
    assert catalog().tools_for("typo") == []


# ----------------------------------------------------------------- enforcement, standalone


def test_unknown_arguments_are_stripped() -> None:
    """A parameter hidden from an audience must not be settable by naming it."""
    args = catalog().prepare("get_invoice", "external", {"invoice_id": "i1", "evil": 1})
    assert "evil" not in args


def test_pins_beat_the_caller() -> None:
    """A pinned value is an administrator's decision; a caller overriding it would make the
    pin advisory, which is not what a pin is."""
    args = catalog().prepare("get_invoice", "external", {"invoice_id": "i1", "max_results": 999})
    assert args["max_results"] == 25


def test_schema_defaults_are_applied() -> None:
    args = catalog().prepare("get_invoice", "internal", {"invoice_id": "i1"})
    assert args["max_results"] == 100


def test_invalid_arguments_raise_something_catchable() -> None:
    """Public exception, so a caller can map it onto their own transport's error shape."""
    with pytest.raises(InvalidArguments, match="Invalid arguments"):
        catalog().prepare("get_invoice", "external", {})


def test_an_unknown_tool_is_refused_before_any_handler_runs() -> None:
    with pytest.raises(InvalidArguments, match="Unknown or disabled"):
        catalog().prepare("nope", "external", {})


# ----------------------------------------------------------------- scopes, standalone


def test_required_scopes_are_the_union_of_both_sides() -> None:
    """Either side may tighten, neither can loosen: an admin cannot open a tool the code
    says is privileged, and code cannot ignore a scope an admin added."""
    assert catalog().required_scopes("get_invoice", "external", ["billing:read"]) == ["billing:read"]


def test_scope_satisfied_is_importable_on_its_own() -> None:
    """It was implemented but left out of __all__, so the one function a bring-your-own
    server needs to enforce registry scopes was unreachable from the public API."""
    user = AuthUser(id="u1", scopes=["billing:read"])
    assert scope_satisfied(user, ["billing:read"])
    assert not scope_satisfied(user, ["billing:write"])


# ----------------------------------------------------------------- audience entitlement


def test_the_header_requests_and_auth_grants() -> None:
    entitled = AuthUser(id="u1", scopes=["audience:internal"])
    assert catalog().audience_for("internal", entitled) == "internal"


def test_an_unentitled_request_downgrades_rather_than_failing() -> None:
    """A mistyped or over-reaching header should degrade to the public view, not leak an
    internal one and not fail the call."""
    assert catalog().audience_for("internal", AuthUser(id="u1")) == "external"
    assert catalog().audience_for("internal", None) == "external"
    assert catalog().audience_for("", None) == "external"


# ----------------------------------------------------------------- configuration


def test_a_server_reads_its_deployment_facts_from_the_environment(monkeypatch) -> None:
    """Addresses and credentials are deployment facts, so the same source serves dev and
    prod without an edit and a product key is never typed into a repository."""
    monkeypatch.setenv("REGISTRY_URL", "http://registry.test")
    monkeypatch.setenv("REGISTRY_PRODUCT_KEY", "billing")
    monkeypatch.setenv("REGISTRY_API_KEY", "k")
    server = ProductServer()
    assert server.product_key == "billing"


def test_partial_configuration_names_what_is_missing(monkeypatch) -> None:
    """A server that starts and then fails on its first manifest fetch reads as an outage
    rather than as the missing setting it is."""
    for var in ("REGISTRY_URL", "REGISTRY_PRODUCT_KEY", "REGISTRY_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("REGISTRY_URL", "http://registry.test")
    with pytest.raises(ValueError) as caught:
        ProductServer()
    assert "REGISTRY_PRODUCT_KEY" in str(caught.value)
    assert "REGISTRY_API_KEY" in str(caught.value)
    assert "REGISTRY_URL" not in str(caught.value)      # the one that IS set is not blamed


def test_an_explicit_argument_still_wins_over_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("REGISTRY_PRODUCT_KEY", "from-env")
    server = ProductServer("http://registry.test", "explicit", "k")
    assert server.product_key == "explicit"
