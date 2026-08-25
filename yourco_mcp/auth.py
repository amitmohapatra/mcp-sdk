"""Auth layers: identity (AuthProvider) and protocol policy (AuthPolicy).
Default posture: tools/list open, execution gated. Loosening requires the
explicit NoAuth provider — it cannot happen by omission."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AuthUser:
    id: str
    scopes: list = field(default_factory=list)
    claims: dict = field(default_factory=dict)


class AuthProvider(ABC):
    @abstractmethod
    async def authenticate(self, headers: dict) -> Optional[AuthUser]: ...


class NoAuth(AuthProvider):
    """Explicit opt-out: every caller is an anonymous-but-authorized user."""
    async def authenticate(self, headers):
        return AuthUser(id="anonymous", scopes=["*"])


class ApiKeyAuthProvider(AuthProvider):
    """keys: {api_key: {"id": ..., "scopes": [...]}}"""
    def __init__(self, keys: dict):
        self._keys = keys

    async def authenticate(self, headers):
        info = self._keys.get(headers.get("x-api-key", ""))
        return AuthUser(id=info["id"], scopes=info.get("scopes", [])) if info else None


class StaticTokenProvider(AuthProvider):
    """Bearer-token map — handy for tests and simple service-to-service setups."""
    def __init__(self, tokens: dict):
        self._tokens = tokens

    async def authenticate(self, headers):
        token = headers.get("authorization", "").removeprefix("Bearer ").strip()
        info = self._tokens.get(token)
        return AuthUser(id=info["id"], scopes=info.get("scopes", [])) if info else None


class CallableProvider(AuthProvider):
    def __init__(self, fn):
        self._fn = fn

    async def authenticate(self, headers):
        return await self._fn(headers)


class AuthPolicy:
    """Which MCP methods require an authenticated caller."""
    def requires_auth(self, method: str) -> bool:
        raise NotImplementedError


class DefaultPolicy(AuthPolicy):
    OPEN = {"initialize", "ping", "tools/list", "notifications/initialized"}

    def requires_auth(self, method: str) -> bool:
        return method not in self.OPEN


class AllGatedPolicy(AuthPolicy):
    OPEN = {"initialize", "ping", "notifications/initialized"}

    def requires_auth(self, method: str) -> bool:
        return method not in self.OPEN


def scope_satisfied(user: AuthUser, required: list) -> bool:
    return "*" in user.scopes or all(s in user.scopes for s in required)
