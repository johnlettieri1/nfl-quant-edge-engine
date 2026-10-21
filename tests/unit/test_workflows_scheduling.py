"""Workflow structure checks for the hybrid scheduler (static: parses the YAML, runs nothing)."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WF = ROOT / ".github" / "workflows"


def load(name):
    d = yaml.safe_load((WF / name).read_text())
    d["on"] = d.get("on", d.get(True))       # YAML 1.1 reads the key `on` as boolean True
    return d


def text(name):
    return (WF / name).read_text()


def test_every_collection_workflow_has_external_dispatch_and_github_cron_backup():
    for name in ("weekly.yml", "pregame.yml", "settle.yml", "monitor.yml"):
        d = load(name)
        assert d["on"]["schedule"], name                                     # GitHub cron backup kept
        assert d["on"]["workflow_dispatch"]["inputs"]["source"]["options"] == ["manual", "external"], name


def test_weekly_keeps_dual_dst_crons_and_records_trigger_and_target():
    d = load("weekly.yml")
    crons = [c["cron"] for c in d["on"]["schedule"]]
    assert crons == ["10 14 * * 2", "10 15 * * 2", "10 13 * * 4,6,0", "10 14 * * 4,6,0", "30 3 * * 1", "30 4 * * 1"]
    t = text("weekly.yml")
    assert "--scheduled-target" in t and 'steps.src.outputs.trigger' in t
    assert "trigger=github_cron" in t and "trigger=external_dispatch" in t
    assert "record-results" not in t and "run.py settle" not in t            # settlement moved out
    assert d["concurrency"] == {"group": "weekly-records", "cancel-in-progress": False}


def test_settlement_workflow_has_no_odds_key_and_writes_only_ledgers():
    t = text("settle.yml")
    assert "ODDS_API_KEY" not in t
    assert "python run.py settle" in t
    assert "totals_ledger_v2.csv totals_ledger_legacy_pre_spec.csv" in t
    assert "cp -a code/data/odds/. records" not in t and "cp -a code/data/predictions/. records" not in t
    d = load("settle.yml")
    crons = [c["cron"] for c in d["on"]["schedule"]]
    assert crons == ["30 14 * * 2,3,4", "30 15 * * 2,3,4"]                 # Tue + Wed/Thu retries, EDT/EST
    assert d["concurrency"]["group"] == "settle-records"


def test_monitor_is_read_only_for_records_and_has_no_odds_key():
    t = text("monitor.yml")
    d = load("monitor.yml")
    assert "ODDS_API_KEY" not in t
    assert d["permissions"] == {"contents": "read", "actions": "read", "issues": "write"}
    assert "git push" not in t and "x-access-token" not in t
    assert "nfl-monitor$suffix" in t                                         # direct external alert path


def test_pushes_only_ever_target_records_branch():
    for name in ("weekly.yml", "pregame.yml", "settle.yml"):
        for line in text(name).splitlines():
            if "git push" in line and not line.strip().startswith("#"):
                assert "HEAD:refs/heads/records" in line, (name, line)


def test_heartbeat_key_only_from_secret_env_and_never_in_a_query_string():
    for name in ("weekly.yml", "pregame.yml", "settle.yml", "monitor.yml"):
        t = text(name)
        assert "HC_PING_KEY: ${{ secrets.HC_PING_KEY }}" in t, name
        assert "hc-ping.com/${{" not in t, name                              # never interpolated into the script
        assert not re.search(r"hc-ping\.com/[^\"]*\?", t), name             # no query string
        assert '[ -n "$HC_PING_KEY" ]' in t or '[ -z "$HC_PING_KEY" ] && exit 0' in t, name


def test_pregame_targets_and_trigger_passed_via_env_not_inline_interpolation():
    t = text("pregame.yml")
    assert "--targets \"$TARGETS\"" in t and "--trigger \"$TRIGGER\"" in t
    assert "--targets '${{" not in t


# ---- credential exposure --------------------------------------------------------------------------

TOKEN_PATTERNS = [r"github_pat_[A-Za-z0-9_]{20,}", r"\bghp_[A-Za-z0-9]{30,}", r"\bgh[osu]_[A-Za-z0-9]{30,}",
                  r"hc-ping\.com/[A-Za-z0-9_-]{20,}/", r"apiKey=[A-Za-z0-9]{16,}"]


def _repo_text_files():
    for p in ROOT.rglob("*"):
        if any(part in (".git", "__pycache__", ".pytest_cache", "data", ".venv", "node_modules") for part in p.parts):
            continue
        if p.is_file() and p.suffix in (".py", ".yml", ".yaml", ".md", ".toml", ".txt", ".json", ".cfg", ".ini"):
            yield p


def test_no_credentials_in_code_workflows_or_docs():
    me = Path(__file__).resolve()
    for p in _repo_text_files():
        if p.resolve() == me:
            continue
        s = p.read_text(errors="ignore")
        for pat in TOKEN_PATTERNS:
            assert not re.search(pat, s), f"{pat} in {p.relative_to(ROOT)}"


def test_setup_guide_uses_authorization_header_not_query_string():
    g = (ROOT / "docs" / "phase4" / "EXTERNAL_SCHEDULER_SETUP.md").read_text()
    assert "Authorization: Bearer" in g and "access_token=" not in g and "?token=" not in g
    assert "/actions/workflows/weekly.yml/dispatches" in g
    assert "dispatch-only" in g          # the guide states plainly that the token is NOT dispatch-only
