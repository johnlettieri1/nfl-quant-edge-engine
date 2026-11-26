"""Phase 5D: West Coast body-clock test against the closing spread.

Pre-registered in docs/phase5/PHASE_5D_WEST_COAST_PREREGISTRATION.md (approved Nov 26 2026, frozen).
Free nflverse schedule data only. No fitted parameters. Seasons 2000-2022 (1999 has no kickoff times);
every 2023+ row is dropped before anything is computed (holdout sealed).

Pass bar, primary (night) window only, all three required:
  1. West team cover rate, Wilson 95% lower bound > 50%
  2. cover rate > 50% in both 2000-2014 and 2015-2022
  3. cover rate > 50% both when the West team is home and when it is away
The secondary (4:00-6:59 PM ET) window is exploratory and cannot pass.

Run: python -m src.nfl.backtest.phase5d_west_coast   (writes docs/phase5/results/phase5d_*)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.nfl.data.ingest import load_cached_schedules
from src.nfl.market.body_clock import classify
from src.nfl.market.sheet_check import wilson

HOLDOUT_START = 2023
FIRST_SEASON = 2000
SPLIT = 2015
BREAKEVEN = 110 / 210
GAME_COLS = ["game_id", "season", "week", "gameday", "weekday", "gametime", "away_team", "home_team", "window",
             "west_team", "west_home", "home_score", "away_score", "spread_line", "west_spread", "west_margin",
             "west_cover", "west_su"]


def build_games(schedules: pd.DataFrame) -> pd.DataFrame:
    g = schedules[schedules["season"] < HOLDOUT_START].copy()          # holdout dropped first
    assert g["season"].max() < HOLDOUT_START
    g = g[(g["game_type"] == "REG") & (g["season"] >= FIRST_SEASON)]
    for c in ("spread_line", "home_score", "away_score"):
        g[c] = pd.to_numeric(g[c], errors="coerce")
    g = g[g["spread_line"].notna() & g["home_score"].notna() & g["away_score"].notna() & g["gametime"].notna()]
    mins = pd.to_numeric(g["gametime"].str[:2], errors="coerce") * 60 + pd.to_numeric(g["gametime"].str[3:5], errors="coerce")
    rows = []
    for r, m in zip(g.itertuples(index=False), mins):
        spot = classify(r.home_team, r.away_team, int(r.season), m, neutral=(r.location != "Home"))
        if spot is None:
            continue
        margin = r.home_score - r.away_score
        resid = margin - r.spread_line                     # > 0: home covers (spread_line > 0 = home favored)
        sign = 1 if spot["west_home"] else -1
        wr = sign * resid
        rows.append({"game_id": r.game_id, "season": int(r.season), "week": int(r.week), "gameday": r.gameday,
                     "weekday": r.weekday, "gametime": r.gametime, "away_team": r.away_team, "home_team": r.home_team,
                     **spot, "home_score": int(r.home_score), "away_score": int(r.away_score),
                     "spread_line": float(r.spread_line), "west_spread": sign * float(r.spread_line),
                     "west_margin": sign * float(margin),
                     "west_cover": "W" if wr > 0 else ("L" if wr < 0 else "P"),
                     "west_su": "W" if sign * margin > 0 else ("L" if sign * margin < 0 else "T")})
    return pd.DataFrame(rows, columns=GAME_COLS)


def rate(d: pd.DataFrame) -> dict:
    k, l, p = int((d.west_cover == "W").sum()), int((d.west_cover == "L").sum()), int((d.west_cover == "P").sum())
    r, lo, hi = wilson(k, k + l)
    return {"games": int(len(d)), "covers": k, "losses": l, "pushes": p, "cover_rate": r, "ci95": [lo, hi]}


def summarize(games: pd.DataFrame) -> dict:
    out = {}
    for w in ("night", "4pm"):
        d = games[games.window == w]
        s = {"all": rate(d), "2000_2014": rate(d[d.season < SPLIT]), "2015_2022": rate(d[d.season >= SPLIT]),
             "west_home": rate(d[d.west_home]), "west_away": rate(d[~d.west_home]),
             "by_decade": {k: rate(d[(d.season >= a) & (d.season <= b)])
                           for k, a, b in (("2000s", 2000, 2009), ("2010s", 2010, 2019), ("2020_2022", 2020, 2022))},
             "by_day": {str(k): rate(v) for k, v in d.groupby("weekday")},
             "straight_up": {"wins": int((d.west_su == "W").sum()), "losses": int((d.west_su == "L").sum()),
                             "ties": int((d.west_su == "T").sum())},
             "avg_west_spread": float(d.west_spread.mean()) if len(d) else float("nan"),
             "avg_west_margin": float(d.west_margin.mean()) if len(d) else float("nan")}
        a = s["all"]
        s["clears_breakeven_point"] = bool(a["cover_rate"] > BREAKEVEN)
        s["clears_breakeven_lower95"] = bool(a["ci95"][0] > BREAKEVEN)
        if w == "night":
            s["bar1_lower95_above_50"] = bool(a["ci95"][0] > 0.5)
            s["bar2_both_halves_above_50"] = bool(s["2000_2014"]["cover_rate"] > 0.5 and s["2015_2022"]["cover_rate"] > 0.5)
            s["bar3_home_and_away_above_50"] = bool(s["west_home"]["cover_rate"] > 0.5 and s["west_away"]["cover_rate"] > 0.5)
            s["PASS"] = s["bar1_lower95_above_50"] and s["bar2_both_halves_above_50"] and s["bar3_home_and_away_above_50"]
        else:
            s["PASS"] = None        # exploratory window cannot pass
        out[w] = s
    return out


def run(out_dir: Path = Path("docs/phase5/results")) -> dict:
    games = build_games(load_cached_schedules())
    assert games.season.max() < HOLDOUT_START and games.season.min() >= FIRST_SEASON
    out_dir.mkdir(parents=True, exist_ok=True)
    games.to_csv(out_dir / "phase5d_west_coast_games.csv", index=False)
    summary = summarize(games)
    meta = {"generated_at": datetime.now(timezone.utc).isoformat(), "seasons": [FIRST_SEASON, HOLDOUT_START - 1],
            "holdout_untouched": [HOLDOUT_START, 2025], "summary": summary}
    (out_dir / "phase5d_west_coast_summary.json").write_text(json.dumps(meta, indent=1, default=float))
    return summary


if __name__ == "__main__":
    s = run()
    for w, v in s.items():
        a = v["all"]
        print(f"{w}: n={a['games']} {a['covers']}-{a['losses']}-{a['pushes']} cover={a['cover_rate']:.3f} "
              f"ci95=({a['ci95'][0]:.3f},{a['ci95'][1]:.3f}) | early={v['2000_2014']['cover_rate']:.3f} "
              f"late={v['2015_2022']['cover_rate']:.3f} | home={v['west_home']['cover_rate']:.3f} "
              f"away={v['west_away']['cover_rate']:.3f} | SU {v['straight_up']} avg spread {v['avg_west_spread']:+.2f} "
              f"| PASS={v['PASS']}")
