"""At least 60 credits must remain AFTER a normal pull. Phase 5A: a pull is totals + spreads = 2 credits,
so pay only with 62+ remaining (was 61+ with 1-credit pulls).

A balance check made before the request cannot stop a provider that charges more than the expected credit;
that case is caught from the response headers afterwards (HALT file), which is tested here too."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.nfl.data import odds_snapshot as O
from src.nfl.market import phase4_config as C
from tests.unit._phase4_fixtures import PAYLOAD, SCHED

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


class Fake:
    def __init__(self, remaining, last="2"):
        self.remaining, self.last, self.calls = remaining, last, 0

    def balance(self):
        return {"remaining": self.remaining, "used": 100}

    def totals(self, books):
        self.calls += 1
        return {"endpoint": "/v4/sports/americanfootball_nfl/odds", "request_params": {}, "http_status": 200,
                "retrieval_started_at": NOW.isoformat(), "fetched_at": NOW.isoformat(), "body": json.dumps(PAYLOAD).encode(),
                "x_requests_remaining": str((self.remaining or 0) - int(self.last)), "x_requests_used": "1",
                "x_requests_last": self.last}


@pytest.fixture
def fake(monkeypatch):
    def make(remaining, last="2"):
        f = Fake(remaining, last)
        monkeypatch.setattr(O, "fetch_balance", f.balance)
        monkeypatch.setattr(O, "fetch_totals_raw", f.totals)
        return f
    return make


def test_policy_constants_unchanged():
    assert (C.CREDIT_RESERVE, C.EXPECTED_CREDITS_PER_PULL, C.WEEKLY_CREDIT_CAP) == (60, 2, 60)


@pytest.mark.parametrize("remaining", [0, 1, 59, 60, 61])
def test_refuses_when_the_reserve_would_not_survive_the_pull(tmp_path, fake, remaining):
    f = fake(remaining)
    with pytest.raises(O.BudgetError, match="credit reserve"):
        O.take_snapshot("manual", schedules=SCHED, odds_dir=tmp_path, now=NOW)
    assert f.calls == 0 and not (tmp_path / "raw").exists()


@pytest.mark.parametrize("remaining", [62, 63, 495])
def test_pays_when_60_still_remain_after_a_two_credit_pull(tmp_path, fake, remaining):
    f = fake(remaining)
    s = O.take_snapshot("manual", schedules=SCHED, odds_dir=tmp_path, now=NOW)
    assert f.calls == 1 and int(s["x_requests_remaining"]) >= C.CREDIT_RESERVE


def test_unknown_balance_fails_closed(tmp_path, fake):
    f = fake(None)
    with pytest.raises(O.BudgetError, match="fail closed"):
        O.take_snapshot("manual", schedules=SCHED, odds_dir=tmp_path, now=NOW)
    assert f.calls == 0


def test_weekly_cap_still_applies_with_enough_balance(tmp_path, fake):
    f = fake(400)
    raw = tmp_path / "raw"; raw.mkdir()
    for i in range(30):
        (raw / f"totals_202610{14 + i // 24:02d}T{i % 24:02d}0000Z_pregame_window.json").write_text("{}")
    with pytest.raises(O.BudgetError, match="weekly cap"):
        O.take_snapshot("manual", schedules=SCHED, odds_dir=tmp_path, now=NOW)
    assert f.calls == 0


def test_unexpected_provider_charge_cannot_be_prevented_but_halts_everything_after(tmp_path, fake):
    """At 62 remaining the preflight allows a 2-credit pull. If the provider charges 4, the reserve is
    breached (58 left) and only the post-response check can react: data kept, HALT written, next pull refused."""
    f = fake(62, last="4")
    with pytest.raises(O.CostAnomalyError):
        O.take_snapshot("manual", schedules=SCHED, odds_dir=tmp_path, now=NOW)
    assert f.calls == 1 and any(p.name.startswith("HALT_") for p in (tmp_path / "raw").iterdir())
    f.remaining, f.last = 400, "2"
    with pytest.raises(O.BudgetError, match="halted"):
        O.take_snapshot("manual", schedules=SCHED, odds_dir=tmp_path, now=NOW)
    assert f.calls == 1
