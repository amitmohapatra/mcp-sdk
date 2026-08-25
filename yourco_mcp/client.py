"""RegistryClient: bootstrap fetch + live subscription + snapshot fallback.
Transport and subscriber are injectable (tests, custom infra)."""
import asyncio
import json
import logging
from pathlib import Path
from typing import AsyncIterator, Callable, Optional

import httpx

log = logging.getLogger("yourco_mcp")


class RegistryError(Exception):
    pass


class RegistryClient:
    def __init__(self, registry_url: str, product_key: str, api_key: str,
                 snapshot_path: str = "", transport: Optional[httpx.AsyncBaseTransport] = None):
        self.registry_url = registry_url.rstrip("/")
        self.product_key = product_key
        self.api_key = api_key
        self.snapshot_path = Path(snapshot_path or f".yourco_mcp_{product_key}.snapshot.json")
        self._transport = transport

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, base_url=self.registry_url,
                                 headers={"X-API-Key": self.api_key}, timeout=15)

    async def fetch_manifest(self) -> dict:
        async with self._http() as client:
            r = await client.get(f"/v1/products/{self.product_key}/manifest")
            if r.status_code != 200:
                raise RegistryError(f"manifest fetch failed: HTTP {r.status_code}")
            manifest = r.json()
        self.save_snapshot(manifest)
        return manifest

    async def fetch_or_snapshot(self) -> dict:
        """Registry down != product down: fall back to last-known-good."""
        try:
            return await self.fetch_manifest()
        except Exception as exc:
            snap = self.load_snapshot()
            if snap is None:
                raise RegistryError(f"registry unreachable and no snapshot: {exc}") from exc
            log.warning("registry unreachable (%s); serving last-known-good snapshot seq=%s",
                        exc, snap.get("seq"))
            return snap

    def save_snapshot(self, manifest: dict) -> None:
        try:
            self.snapshot_path.write_text(json.dumps(manifest))
        except OSError as exc:
            log.warning("could not persist snapshot: %s", exc)

    def load_snapshot(self) -> Optional[dict]:
        try:
            return json.loads(self.snapshot_path.read_text())
        except (OSError, ValueError):
            return None

    # ---- subscription strategies ----

    async def subscribe(self, manifest: dict) -> AsyncIterator[dict]:
        """Pick the push channel the manifest advertises: product Redis if configured,
        else the registry's SSE stream."""
        redis_url = (manifest.get("channel") or {}).get("redis_url", "")
        channel = (manifest.get("channel") or {}).get("name", f"registry:{self.product_key}")
        if redis_url:
            async for m in self._subscribe_redis(redis_url, channel):
                yield m
        else:
            async for m in self._subscribe_sse():
                yield m

    async def _subscribe_redis(self, url: str, channel: str) -> AsyncIterator[dict]:
        import redis.asyncio as aioredis
        r = aioredis.from_url(url, decode_responses=True)
        async with r.pubsub() as ps:
            await ps.subscribe(channel)
            async for msg in ps.listen():
                if msg["type"] == "message":
                    yield json.loads(msg["data"])

    async def _subscribe_sse(self) -> AsyncIterator[dict]:
        async with self._http() as client:
            async with client.stream("GET", f"/v1/products/{self.product_key}/events",
                                     timeout=None) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        payload = json.loads(line[6:])
                        if "type" in payload:
                            yield payload
