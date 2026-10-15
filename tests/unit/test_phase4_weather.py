"""Phase 4: forward-only weather capture, roof handling, pre-registered flags."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd

from src.nfl.data import weather_forecast as W
from tests.unit._phase4_fixtures import SCHED

UTC = timezone.utc
KICK = datetime(2026, 10, 18, 17, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 16, 13, 10, tzinfo=UTC)


class Resp:
    def __init__(self, body, status=200):
        self.content, self.status_code = body, status


def om_block(start, n=240, wind=16.0, prob=70, precip=0.05, snow=0.0):
    t0 = int(start.timestamp()) // 3600 * 3600
    return {"latitude": 39.75, "longitude": -105.0, "hourly_units": {"wind_speed_10m": "mp/h", "precipitation": "inch", "snowfall": "inch"},
            "hourly": {"time": [t0 + 3600 * i for i in range(n)], "temperature_2m": [50.0] * n, "relative_humidity_2m": [40] * n,
                       "precipitation_probability": [prob] * n, "precipitation": [precip] * n, "snowfall": [snow] * n,
                       "weather_code": [61] * n, "wind_speed_10m": [wind] * n, "wind_gusts_10m": [wind + 8] * n}}


def run(tmp_path, games, block=None, now=NOW):
    calls = []
    def http_get(url, params, timeout, headers):
        calls.append(params)
        return Resp(json.dumps([block or om_block(now)]).encode())
    s = W.capture_for_snapshot("sid1", games, SCHED, weather_dir=tmp_path, now=now, http_get=http_get)
    return s, calls, W.load_forecasts(tmp_path)


def test_outdoor_game_window_and_preregistered_flags(tmp_path):
    s, calls, df = run(tmp_path, [("2026_07_SEA_DEN", KICK.isoformat())])
    r = df.iloc[0]
    assert r.roof_type == "outdoors" and r.weather_actionable == "yes" and r.data_status == "ok"
    assert r.wind_mph_window_mean == 16.0 and bool(r.flag_w15) and not bool(r.flag_w20)
    assert r.precip_in_window_sum == 0.2 and bool(r.flag_wet)      # 4 h x 0.05 in, prob 70
    assert r.valid_start_utc == KICK.isoformat() and r.hours_ahead > 0
    assert calls[0]["wind_speed_unit"] == "mph" and "apikey" not in json.dumps(calls[0]).lower()


def test_weather_retrieval_precedes_kickoff_and_post_kickoff_is_never_recorded(tmp_path):
    s, calls, df = run(tmp_path, [("2026_07_SEA_DEN", KICK.isoformat())], now=KICK + timedelta(minutes=1))
    assert df.empty and calls == []
    s, calls, df = run(tmp_path, [("2026_07_SEA_DEN", KICK.isoformat())])
    assert (pd.to_datetime(df.retrieved_at) < pd.to_datetime(df.kickoff_utc)).all()


def test_indoor_flagged_and_not_requested(tmp_path):
    s, calls, df = run(tmp_path, [("2026_07_DET_MIN", KICK.isoformat())])
    r = df.iloc[0]
    assert r.roof_type == "dome" and r.weather_actionable == "no" and calls == []
    assert pd.isna(r.wind_mph_window_mean)


def test_unknown_venue_is_data_unavailable(tmp_path):
    sched = SCHED.copy(); sched.loc[0, ["stadium_id", "stadium"]] = ["ZZZ00", "Nowhere Field"]
    df_rows = W.capture_for_snapshot("sid", [("2026_07_SEA_DEN", KICK.isoformat())], sched, weather_dir=tmp_path,
                                     now=NOW, http_get=lambda *a, **k: None)
    r = W.load_forecasts(tmp_path).iloc[0]
    assert r.weather_actionable == "unavailable" and str(r.data_status).startswith("DATA UNAVAILABLE")


def test_weather_never_backfills_earlier_snapshots(tmp_path):
    run(tmp_path, [("2026_07_SEA_DEN", KICK.isoformat())])
    first = (tmp_path / W.FORECAST_FILE).read_bytes()
    W.capture_for_snapshot("sid2", [("2026_07_SEA_DEN", KICK.isoformat())], SCHED, weather_dir=tmp_path,
                           now=NOW + timedelta(hours=20), http_get=lambda *a, **k: Resp(json.dumps([om_block(NOW + timedelta(hours=20), wind=25)]).encode()))
    df = W.load_forecasts(tmp_path)
    assert (tmp_path / W.FORECAST_FILE).read_bytes().startswith(first)
    assert df[df.snapshot_id == "sid1"].wind_mph_window_mean.iloc[0] == 16.0      # unchanged
    assert df[df.snapshot_id == "sid2"].wind_mph_window_mean.iloc[0] == 25.0
    assert len(list((tmp_path / "raw").glob("forecast_*.json"))) == 2


def test_flag_thresholds_exact():
    f = W.weather_flags(15.0, 60, 0.10, 0.0)
    assert f["flag_w15"] and not f["flag_w20"] and f["flag_wet"] and not f["flag_extreme"]
    assert not W.weather_flags(14.99, 59, 0.5, 0.0)["flag_w15"] and not W.weather_flags(14.99, 59, 0.5, 0.0)["flag_wet"]
    assert W.weather_flags(20.0, 0, 0, 0)["flag_extreme"] and W.weather_flags(5, 0, 0, 1.0)["flag_extreme"]


def test_every_2026_venue_is_mapped():
    import pathlib
    p = pathlib.Path("data/raw/schedules.parquet")
    if not p.exists():
        import pytest; pytest.skip("schedules cache not present")
    s = pd.read_parquet(p)
    s = s[s.season == 2026]
    missing = [r.game_id for r in s.itertuples() if W.venue_for(r.stadium_id, r.stadium) is None]
    assert missing == []


def test_horizon_counts_from_gmt_midnight_and_skips_games_beyond_it(tmp_path):
    from src.nfl.market import phase4_config as C
    late = NOW.replace(hour=0, minute=0) + timedelta(days=C.WEATHER_FORECAST_DAYS) - timedelta(hours=2)
    s, calls, df = run(tmp_path, [("2026_07_SEA_DEN", late.isoformat())])
    assert df.empty and calls == []        # window would run past the forecast horizon: not recorded at all
