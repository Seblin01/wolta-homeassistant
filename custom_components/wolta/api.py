"""Thin async API client for the Wolta backend."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Chunk size for PUT /data. Kept small so each request body stays well under the
# reverse-proxy body-size limit in front of wolta.se (nginx client_max_body_size;
# NPM/nginx defaults can be as low as 1 MB). ~5000 15-min rows ≈ 0.8 MB → passes
# even a 1 MB limit. The backend allows up to 40k/PUT, but the proxy is the binding
# limit — see the 413 incident 2026-07-05 (backfill against Bronäs).
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

    @staticmethod
    def _auth(token: str) -> dict[str, str]:
        """Bearer transport (server >= 0.44.0): profile token travels in the
        Authorization header instead of the URL path, so it never lands in server
        access logs or browser history."""
        return {"Authorization": f"Bearer {token}"}

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
        grid_var_ore: float | None = None,
        surcharge_ore: float | None = None,
        export_extra_ore: float | None = None,
        grid_var_pct: float | None = None,
        export_extra_pct: float | None = None,
        reserve_pct: float | None = None,
        nameplate_kwh: float | None = None,
        nameplate_kw: float | None = None,
        client_plant_id: str | None = None,
        control_system: str | None = None,
        control_system_name: str | None = None,
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
        if grid_var_ore is not None:
            payload["grid_var_ore"] = grid_var_ore
        if surcharge_ore is not None:
            payload["surcharge_ore"] = surcharge_ore
        if export_extra_ore is not None:
            payload["export_extra_ore"] = export_extra_ore
        if grid_var_pct is not None:
            payload["grid_var_pct"] = grid_var_pct
        if export_extra_pct is not None:
            payload["export_extra_pct"] = export_extra_pct
        # Plain `is not None` (not `or None`): reserve_pct=0 is a legitimate value
        # (a user whose control system keeps zero reserve floor) and must reach
        # the backend, not be swallowed as "unset". Mirrors the tariff fields above.
        if reserve_pct is not None:
            payload["reserve_pct"] = reserve_pct
        if nameplate_kwh is not None:
            payload["nameplate_kwh"] = nameplate_kwh
        if nameplate_kw is not None:
            payload["nameplate_kw"] = nameplate_kw
        # Stable plant identity (see const.CONF_PLANT_ID). Lets the backend recognise a
        # re-onboarded plant and keep its streamed history instead of starting a new row.
        if client_plant_id is not None:
            payload["client_plant_id"] = client_plant_id
        # Omission is meaningful (server >= 0.79.0): on re-onboarding of a known plant
        # an OMITTED control_system PRESERVES the stored value, so entries from before
        # v0.29.0 (no stored field) must not send anything here.
        if control_system is not None:
            payload["control_system"] = control_system
        if control_system_name is not None:
            payload["control_system_name"] = control_system_name
        data = await self._request("POST", "/profile", json=payload)
        return data["profile_token"]

    async def patch_profile(self, token: str, **fields: Any) -> dict:
        """Update profile fields (cost_sek, purchase_date, …).

        PATCH /api/v1/profile with the given JSON fields; the profile token travels
        in the Authorization header (see _auth).
        Returns the response dict.
        """
        return await self._request("PATCH", "/profile", json=fields, headers=self._auth(token))

    async def get_profile(self, token: str) -> dict:
        """Fetch the stored profile parameters (server is source of truth).

        GET /api/v1/profile (Authorization: Bearer <token>) → 200 JSON with
        zone/battery/eff/economy/tariff fields. Raises WoltaAuthError on 404
        (purged/unknown token).
        """
        return await self._request("GET", "/profile", headers=self._auth(token))

    async def adopt_profile(self, token: str, client_plant_id: str | None = None) -> dict:
        """Convert a web-created (upload-kind) profile into an integration profile.

        POST /api/v1/profile/adopt (Authorization: Bearer <token>) → 200
        {"adopted": bool}. Without this, the backend's kind-gate 404s
        PUT/recompute/results for linked web profiles.
        Raises WoltaAuthError on 404, WoltaApiError(status=422) for battery-less
        profiles.

        client_plant_id stamps this entry's stable plant identity on the adopted row so a
        later re-onboard finds it. The backend rebinds the row to the id we send, including
        when the row already carries a different one — that is the normal case when a linked
        profile is re-linked from a new entry. It refuses (409) only when the id already
        belongs to a *different* row, so it never takes an identity away from another plant.

        The response carries `plant_id_set`: false means either "already had this id" (a repeat
        link, the common case) or "no id was sent" — not a failure.
        """
        payload = {} if client_plant_id is None else {"client_plant_id": client_plant_id}
        return await self._request("POST", "/profile/adopt", json=payload, headers=self._auth(token))

    async def put_data(self, token: str, rows: list[dict]) -> dict:
        """Upload energy rows for the given profile.

        PUT /api/v1/profile/data (Authorization: Bearer <token>) with {"rows": [...]}
        Automatically chunks if len(rows) > MAX_ROWS_PER_PUT.
        Returns the last chunk's response dict.
        """
        last_response: dict = {}
        for start in range(0, max(len(rows), 1), MAX_ROWS_PER_PUT):
            chunk = rows[start : start + MAX_ROWS_PER_PUT]
            last_response = await self._request(
                "PUT",
                "/profile/data",
                json={"rows": chunk},
                headers=self._auth(token),
            )
        return last_response

    async def recompute(self, token: str) -> None:
        """Trigger a server-side recomputation for the profile.

        POST /api/v1/profile/recompute (Authorization: Bearer <token>) → 202
        Raises WoltaRateLimitError on 429.
        """
        await self._request("POST", "/profile/recompute", headers=self._auth(token))

    async def results(self, token: str) -> dict:
        """Fetch grading results for the profile.

        GET /api/v1/profile/results (Authorization: Bearer <token>) → 200 JSON
        """
        return await self._request("GET", "/profile/results", headers=self._auth(token))

    async def mint_link(self, token: str) -> str | None:
        """Get-or-create anläggningens läslänk. Returnerar None vid fel (offline, äldre
        backend utan endpointen, krypto otillgängligt server-side) – anroparen faller
        tillbaka på sin cache och i sista hand på en tokenlös URL. Ett misslyckat mint
        får aldrig fälla entryn. isinstance-garden: _request returnerar None på tom
        kropp (api.py:90-96), och ett oväntat tomt 200 ska bete sig som ett fel, inte
        kasta AttributeError.

        (WoltaApiError, aiohttp.ClientError, TimeoutError) – samma mönster som
        coordinator.py:386/:552: ett riktigt offline-läge (DNS/anslutningsfel, timeout)
        kastas av self._session.request(...) INNAN _request hinner konvertera det till en
        WoltaApiError, så ett smalare except hade läckt ut och fällt entryn – precis det
        anropskedjan ska förhindra."""
        try:
            data = await self._request("POST", "/profile/link", headers=self._auth(token))
        except (WoltaApiError, aiohttp.ClientError, TimeoutError) as err:
            # Debug, not warning/error: this degrades silently by design (see
            # docstring above) - the log line only exists so "why is the Visit
            # button tokenless" is diagnosable. Never interpolate the token or a
            # minted code here; `err` cannot carry either (see mint_claim_code's
            # separate hardening for why that other path takes more care).
            _LOGGER.debug(
                "mint_link failed; falling back to cached/tokenless link: %s", err
            )
            return None
        return data.get("link_token") if isinstance(data, dict) else None

    async def mint_claim_code(self, token: str) -> str:
        """Create a one-time claim code linking this plant to a wolta.se account.

        POST /api/v1/profile/claim-code (Authorization: Bearer <token>) → 200
        {"code": "XXXX-XXXX", "expires_in": 600}.

        Unlike mint_link (which degrades to None on any error by design), this
        RAISES on failure: the user is standing in the options flow waiting for a
        code to type in on wolta.se, and a silently swallowed error would just show
        a blank form with nothing to act on. The caller checks WoltaAuthError (404 -
        purged token, start reauth) before the broader WoltaApiError, which it turns
        into an abort(reason="cannot_connect") - the same order async_step_settings
        already used for get_profile.

        Everything it raises lands in the WoltaApiError family (WoltaAuthError is a
        WoltaApiError subclass, raised by _request itself on a 404 - see below), so
        the caller's except clauses stay exhaustive:
          - An empty/unparseable 2xx body (_request returns None, api.py:90-96 - same
            gap mint_link's isinstance guard exists for immediately above) or a body
            without "code" would otherwise TypeError on the subscript below.
          - A real offline failure (DNS, connection refused, timeout) raises
            aiohttp.ClientError/TimeoutError from inside self._session.request(...),
            BEFORE _request gets a chance to turn it into a WoltaApiError - the same
            gap mint_link's except tuple exists for (see its docstring, and
            coordinator.py:386/:552 for the same pattern).
        """
        try:
            data = await self._request(
                "POST", "/profile/claim-code", headers=self._auth(token)
            )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise WoltaApiError(f"Network error minting claim code: {err}") from err
        if not isinstance(data, dict):
            raise WoltaApiError("Unexpected claim-code response: not a JSON object")
        if "code" not in data:
            # Do not interpolate the body here: if the backend ever renamed the
            # field, a valid one-time code could still be present under a
            # different key and would otherwise land in home-assistant.log,
            # breaking the "the code is never logged" invariant (see the
            # config_flow.py caller, which logs this exception's message).
            raise WoltaApiError("Unexpected claim-code response: missing 'code' key")
        return data["code"]

    async def delete(self, token: str) -> None:
        """Right-to-erasure: delete the profile and all associated data.

        DELETE /api/v1/calibration (token in the Authorization: Bearer header).
        """
        await self._request("DELETE", "/calibration", headers=self._auth(token))
