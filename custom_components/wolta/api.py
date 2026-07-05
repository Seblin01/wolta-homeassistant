"""Thin async API client for the Wolta backend."""

from __future__ import annotations

from typing import Any

import aiohttp

# Chunk size for PUT /data. Kept small so each request body stays well under the
# reverse-proxy body-size limit in front of wolta.se (nginx client_max_body_size;
# NPM/nginx defaults can be as low as 1 MB). ~5000 15-min-rader ≈ 0.8 MB → passerar
# även en 1 MB-gräns. Backenden tillåter upp till 40k/PUT, men proxyn är den bindande
# gränsen — se 413-incidenten 2026-07-05 (backfill mot Bronäs).
MAX_ROWS_PER_PUT = 5_000


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class WoltaApiError(Exception):
    """Base error for all Wolta API problems."""

    def __init__(self, message: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class WoltaAuthError(WoltaApiError):
    """404 – profile token purged or unknown; integration should re-authenticate."""


class WoltaRateLimitError(WoltaApiError):
    """429 – request rate-limited by the Wolta backend."""

    def __init__(self, retry_after: int = 3600) -> None:
        self.retry_after = retry_after
        super().__init__(f"Rate limited; retry after {retry_after}s")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class WoltaApiClient:
    """Async HTTP client wrapping the Wolta /api/v1 endpoints."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str = "https://wolta.se",
    ) -> None:
        self._session = session
        self._base = base_url.rstrip("/") + "/api/v1"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        """Issue a single HTTP request and return the parsed JSON body.

        Raises:
            WoltaAuthError: for HTTP 404.
            WoltaRateLimitError: for HTTP 429 (reads Retry-After header).
            WoltaApiError: for any other non-2xx response.
        """
        url = f"{self._base}{path}"
        async with self._session.request(method, url, **kwargs) as resp:
            if resp.status == 404:
                raise WoltaAuthError(f"404 from {url}")
            if resp.status == 429:
                retry_after_str = resp.headers.get("Retry-After", "")
                try:
                    retry_after = int(retry_after_str)
                except (ValueError, TypeError):
                    retry_after = 3600
                raise WoltaRateLimitError(retry_after=retry_after)
            if not (200 <= resp.status < 300):
                body = await resp.text()
                raise WoltaApiError(f"HTTP {resp.status} from {url}: {body}", status=resp.status)
            # Return JSON body for 2xx responses (None for no-content)
            if resp.status == 204:
                return None
            # Try JSON; fall back to None if body is empty
            try:
                return await resp.json()
            except Exception:
                return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_profile(
        self,
        *,
        zone: str,
        battery_kwh: float,
        battery_kw: float,
        eff: float,
        has_solar: bool,
        share_profile: bool,
        cost_sek: float | None = None,
        purchase_date: str | None = None,
    ) -> str:
        """Create a new profile and return its token.

        POST /api/v1/profile → 201 {"profile_token": "<tok>"}
        """
        payload: dict[str, Any] = {
            "zone": zone,
            "battery_kwh": battery_kwh,
            "battery_kw": battery_kw,
            "eff": eff,
            "has_solar": has_solar,
            "share_profile": share_profile,
        }
        if cost_sek is not None:
            payload["cost_sek"] = cost_sek
        if purchase_date is not None:
            payload["purchase_date"] = purchase_date
        data = await self._request("POST", "/profile", json=payload)
        return data["profile_token"]

    async def patch_profile(self, token: str, **fields: Any) -> dict:
        """Update profile fields (cost_sek, purchase_date, …).

        PATCH /api/v1/profile/{token} with the given JSON fields.
        Returns the response dict.
        """
        return await self._request("PATCH", f"/profile/{token}", json=fields)

    async def put_data(self, token: str, rows: list[dict]) -> dict:
        """Upload energy rows for the given profile.

        PUT /api/v1/profile/{token}/data with {"rows": [...]}
        Automatically chunks if len(rows) > MAX_ROWS_PER_PUT.
        Returns the last chunk's response dict.
        """
        last_response: dict = {}
        for start in range(0, max(len(rows), 1), MAX_ROWS_PER_PUT):
            chunk = rows[start : start + MAX_ROWS_PER_PUT]
            last_response = await self._request(
                "PUT",
                f"/profile/{token}/data",
                json={"rows": chunk},
            )
        return last_response

    async def recompute(self, token: str) -> None:
        """Trigger a server-side recomputation for the profile.

        POST /api/v1/profile/{token}/recompute → 202
        Raises WoltaRateLimitError on 429.
        """
        await self._request("POST", f"/profile/{token}/recompute")

    async def results(self, token: str) -> dict:
        """Fetch grading results for the profile.

        GET /api/v1/profile/{token}/results → 200 JSON
        """
        return await self._request("GET", f"/profile/{token}/results")

    async def delete(self, token: str) -> None:
        """Right-to-erasure: delete the profile and all associated data.

        DELETE /api/v1/calibration/{token}
        """
        await self._request("DELETE", f"/calibration/{token}")
