"""Shared HTTP client base: TTL caching, stale-value access, typed upstream errors.

Every upstream API client (Radarr, Sonarr, Jellyseerr, plex.tv, ...) wraps its
requests through a single ``CachedHTTP`` instance built on one shared
``httpx.AsyncClient``, so caching, error typing, and the "as of N min ago"
stale-value fallback are implemented exactly once.
"""

import time
from copy import deepcopy
from typing import Any

import httpx


class UpstreamError(Exception):
    """Raised when an upstream service call fails (connect error or non-2xx).

    Attributes:
        service: Short name of the upstream service (e.g. "radarr").
        status: Upstream HTTP status code, or None for a transport/connect
            failure where no response was received. Callers use this to tell
            "that item doesn't exist" (404) apart from "the service is
            unreachable", which are very different answers to give a client.
        detail: The upstream's own one-line explanation, when it sent one as
            JSON ``{"message": ...}``. Only ever populated for 4xx: a 4xx is
            the upstream telling the *caller* something actionable
            ("Request for this media already exists"), whereas a 5xx body is
            the upstream's internals and must not be relayed to a friend.
    """

    def __init__(
        self,
        service: str,
        message: str,
        status: int | None = None,
        detail: str | None = None,
    ) -> None:
        """Initialize with the failing service name and a human-readable message."""
        super().__init__(message)
        self.service = service
        self.status = status
        self.detail = detail


def _status_of(exc: httpx.HTTPError) -> int | None:
    """The response status behind an httpx error, if the error carries one."""
    return exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None


#: Cap on a relayed upstream message. Long enough for any real Jellyseerr
#: refusal, short enough that a hostile or broken upstream cannot use our
#: error channel as a megaphone.
_MAX_DETAIL = 200


def _detail_of(exc: httpx.HTTPError) -> str | None:
    """A relayable one-line message from a 4xx response body, if there is one.

    Deliberately narrow: only 4xx, only a JSON object's string ``message``,
    only the first line, capped. Anything else answers None so the caller
    falls back to its own sanitized wording.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    if not 400 <= exc.response.status_code < 500:
        return None
    try:
        body = exc.response.json()
    except ValueError:
        return None
    message = body.get("message") if isinstance(body, dict) else None
    if not isinstance(message, str) or not message.strip():
        return None
    return message.strip().splitlines()[0][:_MAX_DETAIL]


class CachedHTTP:
    """httpx wrapper providing TTL-based GET caching and stale-value access.

    The app owns a single shared ``httpx.AsyncClient`` (timeout 15s) and passes
    it in; ``CachedHTTP`` does not manage the client's lifecycle.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        """Wrap a shared httpx.AsyncClient with caching behavior.

        Args:
            client: A shared httpx.AsyncClient instance owned by the app.
        """
        self._client = client
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[Any, float]] = {}

    @staticmethod
    def _cache_key(url: str, params: dict[str, Any] | None) -> tuple[str, tuple[tuple[str, str], ...]]:
        """Build a cache key from the URL and sorted, stringified params.

        Values are stringified so unhashable param values (e.g. lists) don't
        raise when building the key tuple.
        """
        items = tuple(sorted((str(k), str(v)) for k, v in (params or {}).items()))
        return (url, items)

    async def get_json(
        self,
        url: str,
        *,
        service: str,
        headers: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        ttl: float = 0,
    ) -> Any:
        """GET a JSON resource, optionally served from a TTL cache.

        A ``ttl`` of 0 bypasses reading from the cache, but a successful
        response is still stored so ``stale()`` can serve it later. On
        failure, any existing cache entry for this key is left untouched.
        Every returned value is a deep copy, isolated from the cached entry,
        so callers are free to mutate what they get back.

        Args:
            url: Request URL.
            service: Short name of the upstream service, used in errors.
            headers: Optional request headers.
            params: Optional query params; part of the cache key (order-independent).
            ttl: Seconds the cached value is considered fresh. 0 = always refetch.

        Returns:
            Parsed JSON response body.

        Raises:
            UpstreamError: On a connect/transport error or non-2xx response.
        """
        key = self._cache_key(url, params)

        if ttl > 0:
            cached = self._cache.get(key)
            if cached is not None:
                value, at = cached
                if time.monotonic() - at < ttl:
                    return deepcopy(value)

        try:
            response = await self._client.get(url, headers=headers, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstreamError(
                service, f"{service} request failed: {exc}", _status_of(exc), _detail_of(exc)
            ) from exc

        value = response.json()
        self._cache[key] = (value, time.monotonic())
        return deepcopy(value)

    async def get_text(
        self,
        url: str,
        *,
        service: str,
        headers: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> str:
        """GET a resource as raw text, never cached.

        Exists for the handful of plex.tv v1 endpoints that answer with XML
        regardless of the ``Accept`` header -- ``get_json`` would choke on
        those trying to parse the body. Error semantics are identical to
        ``get_json``; nothing is cached because the only caller needs a live
        answer anyway.

        Args:
            url: Request URL.
            service: Short name of the upstream service, used in errors.
            headers: Optional request headers.
            params: Optional query params.

        Returns:
            The response body as text.

        Raises:
            UpstreamError: On a connect/transport error or non-2xx response.
        """
        try:
            response = await self._client.get(url, headers=headers, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstreamError(
                service, f"{service} request failed: {exc}", _status_of(exc), _detail_of(exc)
            ) from exc

        return response.text

    async def send_json(
        self,
        method: str,
        url: str,
        *,
        service: str,
        headers: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        """Send a non-cached JSON request (POST/PUT/DELETE/...).

        Args:
            method: HTTP method, e.g. "POST", "PUT", "DELETE".
            url: Request URL.
            service: Short name of the upstream service, used in errors.
            headers: Optional request headers.
            params: Optional query params.
            json: Optional JSON request body.

        Returns:
            Parsed JSON response body, or None if the response has no body
            (e.g. a typical DELETE response).

        Raises:
            UpstreamError: On a connect/transport error or non-2xx response.
        """
        try:
            response = await self._client.request(
                method, url, headers=headers, params=params, json=json
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstreamError(
                service, f"{service} request failed: {exc}", _status_of(exc), _detail_of(exc)
            ) from exc

        if not response.content:
            return None
        return response.json()

    def stale(
        self, url: str, params: dict[str, Any] | None = None
    ) -> tuple[Any, float] | None:
        """Return the last-known cached value for a GET, ignoring TTL.

        Args:
            url: Request URL used in the original ``get_json`` call.
            params: Query params used in the original ``get_json`` call.

        Returns:
            A (value, age_seconds) tuple, or None if nothing is cached for this
            key. The value is a deep copy, isolated from the cached entry, so
            callers are free to mutate what they get back.
        """
        key = self._cache_key(url, params)
        cached = self._cache.get(key)
        if cached is None:
            return None
        value, at = cached
        return deepcopy(value), time.monotonic() - at

    def invalidate(self, prefix: str) -> None:
        """Drop all cached GET entries whose URL starts with ``prefix``.

        Args:
            prefix: URL prefix to match for eviction.
        """
        self._cache = {
            key: entry for key, entry in self._cache.items() if not key[0].startswith(prefix)
        }
