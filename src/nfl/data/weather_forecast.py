"""Forward-only forecast weather capture (Phase 4, Open-Meteo, non-commercial personal research).

Captured in the SAME run as an odds snapshot and linked to it by snapshot_id. Only forecasts
actually retrieved before kickoff are stored; a forecast row never joins any earlier snapshot.
No API key. One batched request per snapshot (outdoor + retractable venues only).

  data/weather/raw/forecast_<UTC ts>.json   envelope `weather_raw_v1` (request params, response bytes
                                            + SHA-256, linked snapshot_id). Created exclusively.
  data/weather/forecasts_v1.csv             one row per (snapshot, game), append-only.

Roof handling: dome -> recorded, weather_actionable "no" (not requested); retractable ->
requested, "conditional" (excluded from the primary pre-registered test); outdoors -> "yes";
unknown venue -> "unavailable" (DATA UNAVAILABLE, never guessed).
Data: Open-Meteo, CC-BY 4.0.
"""
from __future__ import annotations

import hashlib
import json
import logging
import statistics
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import pandas as pd
import requests

from src.nfl.data.odds_snapshot import append_rows_atomic, write_exclusive
from src.nfl.market import phase4_config as C
from src.nfl.market.timing import parse_utc, snapshot_ts

logger = logging.getLogger(__name__)

WEATHER_DIR = Path("data/weather")
STADIUMS_CSV = Path(__file__).with_name("stadiums.csv")
RAW_SCHEMA = "weather_raw_v1"
FORECAST_FILE = "forecasts_v1.csv"
FIELDS = ["snapshot_id", "retrieved_at", "game_id", "venue_id", "lat", "lon", "grid_lat", "grid_lon", "roof_type",
          "weather_actionable", "forecast_model", "model_run_time", "kickoff_utc", "valid_start_utc", "valid_end_utc",
          "hours_ahead", "temp_f_kickoff", "wind_mph_kickoff", "wind_mph_window_mean", "wind_mph_window_max",
          "gust_mph_window_max", "precip_prob_window_max", "precip_in_window_sum", "snow_in_window_sum",
          "weather_code_kickoff", "humidity_kickoff", "flag_w15", "flag_w20", "flag_wet", "flag_extreme",
          "data_status", "raw_file", "response_sha256"]
ACTIONABLE = {"outdoors": "yes", "retractable": "conditional", "dome": "no"}


@lru_cache(maxsize=1)
def load_stadiums() -> pd.DataFrame:
    return pd.read_csv(STADIUMS_CSV, dtype={"stadium_id": str})


def venue_for(stadium_id, stadium_name) -> dict | None:
    st = load_stadiums()
    hit = st[st.stadium_id == stadium_id] if isinstance(stadium_id, str) and stadium_id else st.iloc[0:0]
    if hit.empty and isinstance(stadium_name, str):
        hit = st[(st.name == stadium_name) | (st.aliases.fillna("") == stadium_name)]
    if len(hit) != 1:
        return None
    return hit.iloc[0].to_dict()


def weather_flags(wind_mean, prob_max, precip_sum, snow_sum) -> dict:
    """Pre-registered flags (docs/phase4/PREREGISTRATION_WEATHER.md)."""
    w15 = wind_mean is not None and wind_mean >= C.W15_MPH
    w20 = wind_mean is not None and wind_mean >= C.W20_MPH
    wet = (prob_max is not None and precip_sum is not None and prob_max >= C.WET_PROB_PCT
           and precip_sum >= C.WET_PRECIP_IN)
    extreme = w20 or (snow_sum is not None and snow_sum >= C.EXTREME_SNOW_IN)
    return {"flag_w15": w15, "flag_w20": w20, "flag_wet": wet, "flag_extreme": extreme}


def _to_inches(values: list, unit: str) -> list:
    u = (unit or "").lower()
    f = 1.0 if u in ("inch", "in", "inches") else 1 / 25.4 if u == "mm" else 1 / 2.54 if u == "cm" else None
    if f is None:
        raise ValueError(f"unexpected precipitation unit {unit!r}")
    return [None if v is None else v * f for v in values]


def window_summary(loc: dict, kickoff: datetime) -> dict | None:
    """Kickoff hour + next 3 hours from one Open-Meteo location block (timeformat=unixtime)."""
    h = loc["hourly"]
    units = loc.get("hourly_units", {})
    times = h["time"]
    k_hour = int(kickoff.timestamp()) // 3600 * 3600
    if k_hour not in times:
        return None
    i = times.index(k_hour)
    sl = slice(i, i + C.WEATHER_WINDOW_HOURS)
    if len(times[sl]) < C.WEATHER_WINDOW_HOURS:
        return None
    if units.get("wind_speed_10m") not in ("mp/h", "mph"):
        raise ValueError(f"unexpected wind unit {units.get('wind_speed_10m')!r}")
    wind = h["wind_speed_10m"][sl]
    gust = h["wind_gusts_10m"][sl]
    prob = h["precipitation_probability"][sl]
    precip = _to_inches(h["precipitation"][sl], units.get("precipitation"))
    snow = _to_inches(h["snowfall"][sl], units.get("snowfall"))
    clean = lambda xs: [x for x in xs if x is not None]
    out = {"valid_start_utc": datetime.fromtimestamp(times[i], timezone.utc).isoformat(),
           "valid_end_utc": datetime.fromtimestamp(times[i + C.WEATHER_WINDOW_HOURS - 1] + 3600, timezone.utc).isoformat(),
           "temp_f_kickoff": h["temperature_2m"][i], "wind_mph_kickoff": wind[0],
           "wind_mph_window_mean": round(statistics.fmean(clean(wind)), 2) if clean(wind) else None,
           "wind_mph_window_max": max(clean(wind)) if clean(wind) else None,
           "gust_mph_window_max": max(clean(gust)) if clean(gust) else None,
           "precip_prob_window_max": max(clean(prob)) if clean(prob) else None,
           "precip_in_window_sum": round(sum(clean(precip)), 3) if clean(precip) else None,
           "snow_in_window_sum": round(sum(clean(snow)), 3) if clean(snow) else None,
           "weather_code_kickoff": h["weather_code"][i], "humidity_kickoff": h["relative_humidity_2m"][i]}
    out.update(weather_flags(out["wind_mph_window_mean"], out["precip_prob_window_max"],
                             out["precip_in_window_sum"], out["snow_in_window_sum"]))
    return out


def build_params(venues: list[dict]) -> dict:
    return {"latitude": ",".join(f"{v['lat']:.4f}" for v in venues),
            "longitude": ",".join(f"{v['lon']:.4f}" for v in venues),
            "hourly": ",".join(C.WEATHER_HOURLY_VARS), "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
            "precipitation_unit": "inch", "timezone": "GMT", "timeformat": "unixtime",
            "forecast_days": C.WEATHER_FORECAST_DAYS}


def capture_for_snapshot(snapshot_id: str, games: list[tuple[str, str]], schedules: pd.DataFrame,
                         weather_dir: Path = WEATHER_DIR, now: datetime | None = None,
                         http_get=requests.get) -> dict:
    """games = [(game_id, kickoff_utc_iso)] from the odds snapshot just taken (pregame only)."""
    retrieved = now or datetime.now(timezone.utc)
    sched = schedules.set_index("game_id")
    # Open-Meteo's forecast_days window starts at 00:00 GMT of the request day, not at the request time.
    horizon_end = retrieved.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) \
        + timedelta(days=C.WEATHER_FORECAST_DAYS)
    plan = []
    for gid, kick in games:
        kickoff = parse_utc(kick)
        hours = (kickoff - retrieved).total_seconds() / 3600
        if hours <= 0 or kickoff + timedelta(hours=C.WEATHER_WINDOW_HOURS) > horizon_end or gid not in sched.index:
            continue  # never record weather at or after kickoff; never beyond the forecast horizon
        r = sched.loc[gid]
        v = venue_for(r.get("stadium_id"), r.get("stadium"))
        plan.append((gid, kickoff, hours, v))
    req = [p for p in plan if p[3] is not None and p[3]["roof_type"] in ("outdoors", "retractable")]
    venues = list({p[3]["stadium_id"]: p[3] for p in req}.values())
    blocks, sha, raw_name = {}, None, None
    if venues:
        params = build_params(venues)
        started = datetime.now(timezone.utc).isoformat()
        resp = http_get(C.WEATHER_ENDPOINT, params=params, timeout=30,
                        headers={"User-Agent": "nfl-quant-edge-engine (non-commercial research)"})
        body = resp.content
        sha = hashlib.sha256(body).hexdigest()
        env = {"schema_version": RAW_SCHEMA, "snapshot_id": snapshot_id, "provider": C.WEATHER_PROVIDER,
               "endpoint": C.WEATHER_ENDPOINT, "params": params, "venues_requested": [v["stadium_id"] for v in venues],
               "retrieval_started_at": started, "fetched_at": datetime.now(timezone.utc).isoformat(),
               "http_status": resp.status_code, "response_sha256": sha, "license": "CC-BY 4.0 (Open-Meteo)",
               "response_body": body.decode("utf-8", "replace")}
        path = write_exclusive(weather_dir / "raw" / f"forecast_{snapshot_ts(retrieved)}.json",
                               json.dumps(env, indent=1).encode("utf-8"))
        raw_name = path.name
        if resp.status_code == 200:
            data = json.loads(body)
            data = data if isinstance(data, list) else [data]
            if len(data) != len(venues):
                raise RuntimeError("Open-Meteo returned a different number of locations than requested")
            blocks = {v["stadium_id"]: d for v, d in zip(venues, data)}
    rows = []
    for gid, kickoff, hours, v in plan:
        base = {"snapshot_id": snapshot_id, "retrieved_at": retrieved.isoformat(), "game_id": gid,
                "kickoff_utc": kickoff.isoformat(), "hours_ahead": round(hours, 2),
                "forecast_model": "best_match", "model_run_time": "DATA UNAVAILABLE",
                "raw_file": raw_name, "response_sha256": sha}
        if v is None:
            rows.append({**base, "roof_type": "unknown", "weather_actionable": "unavailable", "data_status": "DATA UNAVAILABLE: venue not in stadium table"})
            continue
        base.update({"venue_id": v["stadium_id"], "lat": v["lat"], "lon": v["lon"], "roof_type": v["roof_type"],
                     "weather_actionable": ACTIONABLE[v["roof_type"]]})
        if v["roof_type"] == "dome":
            rows.append({**base, "data_status": "not requested (fixed roof)"})
            continue
        blk = blocks.get(v["stadium_id"])
        summ = window_summary(blk, kickoff) if blk else None
        if summ is None:
            rows.append({**base, "data_status": "DATA UNAVAILABLE: no forecast for the game window"})
            continue
        rows.append({**base, "grid_lat": blk.get("latitude"), "grid_lon": blk.get("longitude"), **summ, "data_status": "ok"})
    append_rows_atomic(weather_dir / FORECAST_FILE, rows, FIELDS)
    return {"snapshot_id": snapshot_id, "venues_requested": len(venues), "rows": len(rows), "raw_file": raw_name}


def load_forecasts(weather_dir: Path = WEATHER_DIR) -> pd.DataFrame:
    p = weather_dir / FORECAST_FILE
    return pd.read_csv(p) if p.exists() else pd.DataFrame(columns=FIELDS)
