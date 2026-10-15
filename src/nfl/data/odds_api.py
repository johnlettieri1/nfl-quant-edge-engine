"""Live odds from The Odds API (the-odds-api.com) -- game-level lines and
player props for the CURRENT week, used to augment nflverse's
schedule-embedded lines with a real, multi-book market view.

Read-only, real data only: requires ODDS_API_KEY in the environment (see
.env.example); raises clearly if missing rather than silently falling back
to a fabricated or stale value. nflverse remains the source of truth for
ALL historical backtesting -- this client only ever fetches *current*
odds (the API does not provide historical odds on this plan), so nothing
here can leak into a walk-forward backtest; it only feeds the live weekly
prediction snapshot.

Every response is tagged with fetched_at / requests_remaining, same
provenance discipline as data/ingest.py's nflverse pulls.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

BASE_URL = "https://api.the-odds-api.com/v4"
SPORT_KEY = "americanfootball_nfl"


class OddsAPIError(RuntimeError):
    pass


def _api_key() -> str:
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        raise OddsAPIError(
            "ODDS_API_KEY not set. Copy .env.example to .env and add your The Odds API key."
        )
    return key


def _request(path: str, params: dict, timeout: int = 30) -> requests.Response:
    """GET with the key added only at call time. Network errors are re-raised WITHOUT the URL,
    because requests' exception text includes the full query string (and so the key)."""
    key = _api_key()
    try:
        return requests.get(f"{BASE_URL}{path}", params={**params, "apiKey": key}, timeout=timeout,
                            headers={"User-Agent": "nfl-quant-edge-engine"})
    except requests.RequestException as e:
        raise OddsAPIError(f"The Odds API {path} request failed: {type(e).__name__}") from None


def _usage(headers) -> dict:
    h = {k.lower(): v for k, v in headers.items()}
    return {"x_requests_remaining": h.get("x-requests-remaining"), "x_requests_used": h.get("x-requests-used"),
            "x_requests_last": h.get("x-requests-last")}


def _get(path: str, params: dict) -> tuple[list | dict, dict]:
    r = _request(path, params, timeout=20)
    if r.status_code != 200:
        raise OddsAPIError(f"The Odds API {path} returned {r.status_code}: {r.text[:300]}")
    meta = {
        "requests_remaining": r.headers.get("x-requests-remaining"),
        "requests_used": r.headers.get("x-requests-used"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    return r.json(), meta


def fetch_game_odds(markets: str = "h2h,spreads,totals", regions: str = "us") -> tuple[list[dict], dict]:
    """Live NFL odds for all currently listed games. One entry per game,
    each with a `bookmakers` list (each book's own lines per market)."""
    data, meta = _get(
        f"/sports/{SPORT_KEY}/odds",
        {"markets": markets, "regions": regions, "oddsFormat": "american"},
    )
    logger.info("Fetched live game odds for %d games (%s requests remaining)",
                len(data), meta["requests_remaining"])
    return data, meta


def fetch_balance() -> dict:
    """Free call (/sports does not count against the quota) that reports the current credit balance."""
    r = _request("/sports", {})
    if r.status_code != 200:
        raise OddsAPIError(f"The Odds API /sports returned {r.status_code}: {r.text[:300]}")
    u = _usage(r.headers)
    return {"remaining": None if u["x_requests_remaining"] is None else int(float(u["x_requests_remaining"])),
            "used": None if u["x_requests_used"] is None else int(float(u["x_requests_used"]))}


def fetch_totals_raw(bookmakers: list[str]) -> dict:
    """Phase 4 paid pull: NFL totals for an explicit book list (<= 10 books = 1 credit).
    Returns the exact response bytes so the stored hash is verifiable. Never retries."""
    if not bookmakers or len(bookmakers) > 10:
        raise OddsAPIError("Phase 4 pulls must name 1-10 bookmakers (1 credit)")
    params = {"markets": "totals", "oddsFormat": "american", "dateFormat": "iso", "bookmakers": ",".join(bookmakers)}
    started = datetime.now(timezone.utc).isoformat()
    r = _request(f"/sports/{SPORT_KEY}/odds", params)
    return {"endpoint": f"/v4/sports/{SPORT_KEY}/odds", "request_params": params, "http_status": r.status_code,
            "retrieval_started_at": started, "fetched_at": datetime.now(timezone.utc).isoformat(),
            "body": r.content, **_usage(r.headers)}


def fetch_events() -> tuple[list[dict], dict]:
    """Lightweight list of upcoming events (id, teams, commence_time) --
    needed before fetching player props, which are keyed by event id."""
    data, meta = _get(f"/sports/{SPORT_KEY}/events", {})
    logger.info("Fetched %d upcoming events (%s requests remaining)", len(data), meta["requests_remaining"])
    return data, meta


def fetch_event_player_props(event_id: str, markets: str, regions: str = "us") -> tuple[dict, dict]:
    """Player prop odds for one event.

    `markets` e.g. "player_pass_yds,player_rush_yds,player_reception_yds,
    player_receptions,player_pass_tds,player_anytime_td". Costs API credits
    per (event, region, market) -- called once per game, not per player.
    """
    data, meta = _get(
        f"/sports/{SPORT_KEY}/events/{event_id}/odds",
        {"markets": markets, "regions": regions, "oddsFormat": "american"},
    )
    logger.info("Fetched player props for event %s (%s requests remaining)", event_id, meta["requests_remaining"])
    return data, meta
