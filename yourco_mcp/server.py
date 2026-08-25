"""ProductServer: a stateless MCP server (Streamable HTTP, JSON-RPC 2.0) whose tool
metadata is owned by the Tool Registry and hot-swapped at runtime.

Request path never touches the registry: everything serves from an atomically
swapped in-memory manifest. Arguments are validated against the caller's
audience-resolved schema, unknown args stripped, pinned values injected —
before the handler runs."""
import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

import jsonschema

from .auth import (AuthPolicy, AuthProvider, AuthUser, CallableProvider, DefaultPolicy,
                   scope_satisfied)
from .client import RegistryClient

log = logging.getLogger("yourco_mcp")

PROTOCOL_VERSION = "2025-06-18"
JSONRPC_METHOD_NOT_FOUND, JSONRPC_INVALID_PARAMS, JSONRPC_INTERNAL = -32601, -32602, -32603
MCP_UNAUTHORIZED, MCP_FORBIDDEN = -32001, -32003
AUDIENCE_HEADER = "x-tool-audience"


@dataclass
class ToolContext:
    user: AuthUser
    audience: str
    tool: str
    meta: dict = field(default_factory=dict)


class _CompiledManifest:
    """Immutable, pre-compiled view of a manifest: validators built once per swap,
    O(1) lookups on the request path."""

    def __init__(self, manifest: dict):
        self.raw = manifest
        self.seq: int = manifest.get("seq", 0)
        self.default_audience: str = manifest.get("default_audience", "external")
        self.audiences: set = set(manifest.get("audiences", ["external"]))
        self.tools: Dict[str, dict] = {}          # (audience -> name -> view)
        self.validators: Dict[tuple, Any] = {}
        for entity in manifest.get("entities", []):
            if entity.get("type") != "tool":
                continue
            for audience, view in (entity.get("views") or {}).items():
                if not view or not view.get("enabled"):
                    continue
                self.tools.setdefault(audience, {})[entity["name"]] = view
                schema = view["spec"].get("input_schema") or {"type": "object"}
                try:
                    self.validators[(audience, entity["name"])] = \
                        jsonschema.Draft202012Validator(schema)
                except jsonschema.SchemaError:
                    log.error("invalid schema for %s/%s; tool excluded", audience, entity["name"])
                    self.tools[audience].pop(entity["name"], None)

    def apply(self, event: dict) -> "_CompiledManifest":
        """Pure delta application -> NEW compiled manifest (never mutates self)."""
        raw = json.loads(json.dumps(self.raw))
        raw["seq"] = event["seq"]
        entities = {(e["type"], e["name"]): e for e in raw.get("entities", [])}
        etype = event.get("type", "")
        body = event.get("entity") or {}
        if etype in ("entity.created", "entity.updated") and body:
            entities[(body.get("type", "tool"), body["name"])] = {
                "id": body.get("id"), "type": body.get("type", "tool"),
                "name": body["name"], "version": body.get("version", 0),
                "views": body.get("views", {})}
        elif etype == "entity.deleted" and body:
            entities.pop((body.get("type", "tool"), body["name"]), None)
        raw["entities"] = list(entities.values())
        return _CompiledManifest(raw)


class ProductServer:
    def __init__(self, registry_url: str, product_key: str, api_key: str,
                 auth: Optional[AuthProvider] = None,
                 policy: Optional[AuthPolicy] = None,
                 snapshot_path: str = "",
                 client: Optional[RegistryClient] = None):
        self.client = client or RegistryClient(registry_url, product_key, api_key,
                                               snapshot_path=snapshot_path)
        self.product_key = product_key
        self._handlers: Dict[str, Callable] = {}
        self._authorize_hook: Optional[Callable] = None
        if callable(auth) and not isinstance(auth, AuthProvider):
            auth = CallableProvider(auth)
        self.auth: Optional[AuthProvider] = auth
        self.policy: AuthPolicy = policy or DefaultPolicy()
        self._compiled: Optional[_CompiledManifest] = None
        self._listen_task: Optional[asyncio.Task] = None

    # ---- registration API ----

    def tool(self, name: str):
        def register(fn):
            self._handlers[name] = fn
            return fn
        return register

    def authorize(self, fn):
        """Optional hook: async (user, tool_name, args) -> bool, runs after scope checks."""
        self._authorize_hook = fn
        return fn

    # ---- lifecycle ----

    async def start(self) -> None:
        if self._compiled is not None:
            return                                    # idempotent: already started
        manifest = await self.client.fetch_or_snapshot()
        self._swap(_CompiledManifest(manifest))
        self._warn_unbound()
        self._listen_task = asyncio.create_task(self._listen())

    async def stop(self) -> None:
        if self._listen_task:
            self._listen_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listen_task

    def _swap(self, compiled: "_CompiledManifest") -> None:
        self._compiled = compiled                     # atomic reference swap

    def _warn_unbound(self) -> None:
        listed = {n for tools in self._compiled.tools.values() for n in tools}
        for name in listed - set(self._handlers):
            log.warning("tool '%s' is in the registry but has no local handler; "
                        "it will not be served", name)

    async def _listen(self) -> None:
        while True:
            try:
                async for event in self.client.subscribe(self._compiled.raw):
                    await self.handle_event(event)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning("subscription dropped (%s); retrying in 2s", exc)
                await asyncio.sleep(2)
                with contextlib.suppress(Exception):
                    await self._refetch()

    async def handle_event(self, event: dict) -> None:
        current = self._compiled
        seq = event.get("seq", 0)
        if seq <= current.seq:
            return                                    # stale/duplicate: idempotent
        if event.get("type") == "manifest.reload":
            await self._refetch()                     # structural change: full re-sync
            return
        if seq != current.seq + 1:
            log.info("sequence gap (%s -> %s); full re-sync", current.seq, seq)
            await self._refetch()
            return
        try:
            self._swap(current.apply(event))
        except Exception as exc:                      # a bad update never kills a good manifest
            log.error("could not apply event (%s); keeping seq=%s and re-syncing",
                      exc, current.seq)
            await self._refetch()

    async def _refetch(self) -> None:
        with contextlib.suppress(Exception):
            self._swap(_CompiledManifest(await self.client.fetch_manifest()))

    # ---- audience resolution: header REQUESTS, auth GRANTS ----

    def resolve_audience(self, headers: dict, user: Optional[AuthUser]) -> str:
        requested = headers.get(AUDIENCE_HEADER, "") or self._compiled.default_audience
        if requested == self._compiled.default_audience:
            return requested
        if requested in self._compiled.audiences and user and \
                scope_satisfied(user, [f"audience:{requested}"]):
            return requested
        return self._compiled.default_audience        # unentitled -> safe downgrade

    # ---- MCP JSON-RPC (stateless) ----

    async def handle_request(self, body: dict, headers: dict) -> Optional[dict]:
        headers = {k.lower(): v for k, v in headers.items()}
        rid, method = body.get("id"), body.get("method", "")
        params = body.get("params") or {}
        if method.startswith("notifications/"):
            return None
        user: Optional[AuthUser] = None
        if self.auth is not None:
            user = await self.auth.authenticate(headers)
        if self.policy.requires_auth(method) and user is None:
            return _error(rid, MCP_UNAUTHORIZED, "Unauthorized")
        audience = self.resolve_audience(headers, user)
        try:
            if method == "initialize":
                result = {"protocolVersion": PROTOCOL_VERSION,
                          "capabilities": {"tools": {"listChanged": True}},
                          "serverInfo": {"name": self.product_key, "version": "registry-live"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self._list_tools(audience)}
            elif method == "tools/call":
                result = await self._call_tool(params, user, audience)
            else:
                return _error(rid, JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}")
        except _McpFailure as exc:
            return _error(rid, exc.code, exc.message)
        except Exception:
            log.exception("handler crash")
            return _error(rid, JSONRPC_INTERNAL, "Internal error")
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def _list_tools(self, audience: str) -> list:
        out = []
        for name, view in sorted((self._compiled.tools.get(audience) or {}).items()):
            if name not in self._handlers:
                continue
            spec = view["spec"]
            out.append({"name": name, "title": spec.get("title", name),
                        "description": spec.get("description", ""),
                        "inputSchema": spec.get("input_schema", {"type": "object"}),
                        "annotations": spec.get("annotations", {})})
        return out

    async def _call_tool(self, params: dict, user: Optional[AuthUser], audience: str) -> dict:
        name = params.get("name", "")
        raw_args = params.get("arguments") or {}
        view = (self._compiled.tools.get(audience) or {}).get(name)
        if view is None or name not in self._handlers:
            raise _McpFailure(JSONRPC_METHOD_NOT_FOUND, f"Unknown or disabled tool: {name}")
        required_scopes = (view["spec"].get("auth") or {}).get("required_scopes", [])
        if required_scopes and (user is None or not scope_satisfied(user, required_scopes)):
            raise _McpFailure(MCP_FORBIDDEN, "Missing required scope")
        args = self._prepare_args(view, audience, name, raw_args)
        if self._authorize_hook and not await self._authorize_hook(user, name, args):
            raise _McpFailure(MCP_FORBIDDEN, "Not authorized for this call")
        ctx = ToolContext(user=user or AuthUser(id="anonymous"), audience=audience, tool=name)
        result = await self._handlers[name](ctx, **args)
        text = result if isinstance(result, str) else json.dumps(result, default=str)
        return {"content": [{"type": "text", "text": text}], "isError": False}

    def _prepare_args(self, view: dict, audience: str, name: str, raw: dict) -> dict:
        schema = view["spec"].get("input_schema") or {}
        props = schema.get("properties", {})
        args = {k: v for k, v in raw.items() if k in props}     # strip unknown/hidden
        for pname, pspec in props.items():                       # schema defaults
            if pname not in args and "default" in pspec:
                args[pname] = pspec["default"]
        validator = self._compiled.validators.get((audience, name))
        if validator:
            errors = sorted(validator.iter_errors(args), key=lambda e: list(e.absolute_path))
            if errors:
                detail = "; ".join(f"{'/'.join(map(str, e.absolute_path)) or 'arguments'}: "
                                   f"{e.message}" for e in errors[:5])
                raise _McpFailure(JSONRPC_INVALID_PARAMS, f"Invalid arguments: {detail}")
        args.update(view.get("pins") or {})                      # pins win, always
        return args

    # ---- ASGI app (Starlette, stateless Streamable HTTP) ----

    def build_asgi(self):
        import contextlib as _ctx
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse, Response
        from starlette.routing import Route

        @_ctx.asynccontextmanager
        async def lifespan(app):
            await self.start()
            yield
            await self.stop()

        async def mcp_endpoint(request):
            body = await request.json()
            headers = dict(request.headers)
            if isinstance(body, list):                            # JSON-RPC batch
                results = [r for r in [await self.handle_request(b, headers) for b in body] if r]
                return JSONResponse(results) if results else Response(status_code=202)
            result = await self.handle_request(body, headers)
            return JSONResponse(result) if result else Response(status_code=202)

        async def health(request):
            return JSONResponse({"ok": True, "seq": self._compiled.seq if self._compiled else -1})

        return Starlette(routes=[Route("/mcp", mcp_endpoint, methods=["POST"]),
                                 Route("/healthz", health)], lifespan=lifespan)

    def run(self, host: str = "0.0.0.0", port: int = 8080):
        import uvicorn
        uvicorn.run(self.build_asgi(), host=host, port=port)


class _McpFailure(Exception):
    def __init__(self, code: int, message: str):
        self.code, self.message = code, message


def _error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
