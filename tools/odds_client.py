import os
import re
from typing import Any, Dict, Optional

import httpx
from dotenv import load_dotenv

from .log_context import slog, Timer

load_dotenv()

ODDS_API_KEY = os.getenv("ODDS_API_KEY") or os.getenv("THE_ODDS_API_KEY")
ODDS_BASE_URL = os.getenv("ODDS_API_BASE_URL", "https://api.the-odds-api.com/v4")

# Regex to strip any token/key that leaks into a URL string
_SENSITIVE_QS = re.compile(r"(apiKey|api_?key|token|secret)=[^&]+", re.IGNORECASE)


def _sanitize_url(url: str) -> str:
    """Replace sensitive query-string values with <REDACTED>."""
    return _SENSITIVE_QS.sub(r"\1=<REDACTED>", str(url))


def _to_bool_str(value: Optional[bool]) -> Optional[str]:
    if value is None:
        return None
    return "true" if value else "false"


class OddsClient:
    """
    Client for The Odds API.

    NOTE: The Odds API *requires* the key as a query parameter (`apiKey=...`).
    We cannot move it to a header.  Instead we sanitize all logged URLs so the
    key never appears in log output.
    """

    def __init__(self):
        if not ODDS_API_KEY:
            slog.warning("odds_client.init", detail="ODDS_API_KEY not found — odds calls will fail")
        self.headers = {"Accept": "application/json"}

    def _make_request(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if params is None:
            params = {}

        if not ODDS_API_KEY:
            return {"error": "ODDS_API_KEY is missing. Add it to .env to enable odds tools."}

        # Odds API requires key as query param — cannot use header auth
        params["apiKey"] = ODDS_API_KEY
        url = f"{ODDS_BASE_URL.rstrip('/')}/{endpoint.lstrip('/')}"

        timer = Timer()
        status_code: Optional[int] = None
        try:
            with timer:
                response = httpx.get(url, params=params, headers=self.headers, timeout=12.0)
                status_code = response.status_code

            slog.info(
                "odds_api.request",
                endpoint=endpoint,
                status=status_code,
                latency_ms=timer.elapsed_ms,
                url=_sanitize_url(str(response.url)),
            )

            if response.status_code == 200:
                payload = response.json()
                return {
                    "data": payload,
                    "meta": {
                        "requests_remaining": response.headers.get("x-requests-remaining"),
                        "requests_used": response.headers.get("x-requests-used"),
                    },
                }
            if response.status_code in (401, 403):
                return {"error": f"Odds API auth error ({response.status_code})", "message": response.text[:300]}
            if response.status_code == 422:
                return {"error": "Odds API validation error (422).", "message": response.text[:300]}
            if response.status_code == 429:
                return {"error": "Odds API rate limit exceeded (429).", "message": response.text[:300]}
            return {"error": f"Odds API error {response.status_code}", "message": response.text[:300]}

        except httpx.RequestError as exc:
            timer.stop()
            slog.error(
                "odds_api.request_error",
                endpoint=endpoint,
                latency_ms=timer.elapsed_ms,
                error=str(exc),
            )
            return {"error": f"Odds API request failed: {exc}"}

    def get_odds(
        self,
        sport: str = "basketball_nba",
        regions: str = "us",
        markets: str = "h2h",
        date_format: str = "iso",
        odds_format: str = "american",
        event_ids: Optional[str] = None,
        bookmakers: Optional[str] = None,
        commence_time_from: Optional[str] = None,
        commence_time_to: Optional[str] = None,
        include_links: Optional[bool] = None,
        include_sids: Optional[bool] = None,
        include_bet_limits: Optional[bool] = None,
        include_rotation_numbers: Optional[bool] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "regions": regions,
            "markets": markets,
            "dateFormat": date_format,
            "oddsFormat": odds_format,
        }
        if event_ids:
            params["eventIds"] = event_ids
        if bookmakers:
            params["bookmakers"] = bookmakers
        if commence_time_from:
            params["commenceTimeFrom"] = commence_time_from
        if commence_time_to:
            params["commenceTimeTo"] = commence_time_to
        if include_links is not None:
            params["includeLinks"] = _to_bool_str(include_links)
        if include_sids is not None:
            params["includeSids"] = _to_bool_str(include_sids)
        if include_bet_limits is not None:
            params["includeBetLimits"] = _to_bool_str(include_bet_limits)
        if include_rotation_numbers is not None:
            params["includeRotationNumbers"] = _to_bool_str(include_rotation_numbers)

        return self._make_request(f"sports/{sport}/odds", params=params)
