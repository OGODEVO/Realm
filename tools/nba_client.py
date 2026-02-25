
import os
import re
import httpx
from typing import Dict, Any, Optional
from dotenv import load_dotenv

from .log_context import slog, Timer

load_dotenv()

BASE_URL = os.getenv("RSC_BASE_URL", "https://rest.datafeeds.rolling-insights.com/api/v1")
RSC_TOKEN = os.getenv("RSC_TOKEN")

# Regex to strip any token/key that leaks into a URL string
_SENSITIVE_QS = re.compile(r"(RSC_token|api_?key|token|secret)=[^&]+", re.IGNORECASE)


def _sanitize_url(url: str) -> str:
    """Replace sensitive query-string values with <REDACTED>."""
    return _SENSITIVE_QS.sub(r"\1=<REDACTED>", str(url))


class NBAClient:
    def __init__(self):
        if not RSC_TOKEN:
            slog.warning("nba_client.init", detail="RSC_TOKEN not found — API calls will fail")

        self.headers: Dict[str, str] = {
            "Accept": "application/json",
        }

    def _make_request(self, endpoint: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        HTTP GET to the Rolling Insights NBA API.
        Token is sent as a query param (required by this API).
        All logged URLs are sanitized to strip the token value.
        """
        if params is None:
            params = {}

        # Rolling Insights requires the token as a query parameter.
        params["RSC_token"] = RSC_TOKEN

        url = f"{BASE_URL}/{endpoint}"

        timer = Timer()
        status_code: Optional[int] = None
        try:
            with timer:
                response = httpx.get(url, params=params, headers=self.headers, timeout=10.0)
                status_code = response.status_code

            slog.info(
                "nba_api.request",
                endpoint=endpoint,
                status=status_code,
                latency_ms=timer.elapsed_ms,
                url=_sanitize_url(str(response.url)),
            )

            if response.status_code == 200:
                try:
                    return response.json()
                except Exception:
                    return {"error": "Failed to parse JSON response", "raw": response.text[:500]}
            elif response.status_code == 304:
                return {"status": "No data updates (304)"}
            elif response.status_code == 404:
                return {"error": "Resource not found (404)", "message": response.text[:300]}
            else:
                return {"error": f"API Error {response.status_code}", "message": response.text[:300]}

        except httpx.RequestError as e:
            timer.stop()
            slog.error(
                "nba_api.request_error",
                endpoint=endpoint,
                latency_ms=timer.elapsed_ms,
                error=str(e),
            )
            return {"error": f"Request failed: {str(e)}"}

    def get_schedule(self, date_str: str, sport: str = "NBA", team_id: Optional[int] = None, game_id: Optional[str] = None) -> Dict[str, Any]:
        """Daily Schedule"""
        params = {}
        if team_id: params['team_id'] = team_id
        if game_id: params['game_id'] = game_id
        return self._make_request(f"schedule/{date_str}/{sport}", params)

    def get_weekly_schedule(self, date_str: str, sport: str = "NBA", team_id: Optional[int] = None) -> Dict[str, Any]:
        """Weekly Schedule"""
        params = {}
        if team_id: params['team_id'] = team_id
        return self._make_request(f"schedule-week/{date_str}/{sport}", params)

    def get_live_data(self, date_str: str, sport: str = "NBA", team_id: Optional[int] = None, game_id: Optional[str] = None) -> Dict[str, Any]:
        """Live Feed"""
        params = {'clear_state': '1'}
        if team_id: params['team_id'] = team_id
        if game_id: params['game_id'] = game_id
        return self._make_request(f"live/{date_str}/{sport}", params)

    def get_team_info(self, sport: str = "NBA", team_id: Optional[int] = None) -> Dict[str, Any]:
        """Team Info"""
        params = {}
        if team_id: params['team_id'] = team_id
        return self._make_request(f"team-info/{sport}", params)

    def get_team_stats(self, year: str, sport: str = "NBA", team_id: Optional[int] = None) -> Dict[str, Any]:
        """Team Season Stats"""
        params = {}
        if team_id: params['team_id'] = team_id
        return self._make_request(f"team-stats/{year}/{sport}", params)

    def get_player_info(self, sport: str = "NBA", team_id: Optional[int] = None) -> Dict[str, Any]:
        """Player Info"""
        params = {}
        if team_id: params['team_id'] = team_id
        return self._make_request(f"player-info/{sport}", params)

    def get_player_stats(self, year: str, sport: str = "NBA", team_id: Optional[int] = None, player_id: Optional[int] = None) -> Dict[str, Any]:
        """Player Season Stats"""
        params = {}
        if team_id: params['team_id'] = team_id
        if player_id: params['player_id'] = player_id
        return self._make_request(f"player-stats/{year}/{sport}", params)

    def get_injuries(self, sport: str = "NBA", team_id: Optional[int] = None) -> Dict[str, Any]:
        """Injuries"""
        params = {}
        if team_id: params['team_id'] = team_id
        return self._make_request(f"injuries/{sport}", params)

    def get_depth_charts(self, sport: str = "NBA", team_id: Optional[int] = None) -> Dict[str, Any]:
        """Depth Charts"""
        params = {}
        if team_id: params['team_id'] = team_id
        return self._make_request(f"depth-charts/{sport}", params)
