"""Phase 4: records guard (paths, immutability, append-only, secrets), against a real git repo."""
from __future__ import annotations

import subprocess

from src.nfl.ops.records_guard import evaluate, main


def ev(changes, old=None, new=None, secrets=None):
    old, new = old or {}, new or {}
    return evaluate(changes, lambda p: old.get(p, b""), lambda p: new.get(p, b""), secrets)


def test_blocks_source_and_workflow_paths():
    assert ev([("A", "src/nfl/market/clv.py")]) and ev([("M", ".github/workflows/weekly.yml")])
    assert ev([("A", "README.md")])


def test_allows_new_records_files():
    assert ev([("A", "data/odds/raw/totals_x.json"), ("A", "data/weather/raw/forecast_x.json")],
              new={"data/odds/raw/totals_x.json": b"{}", "data/weather/raw/forecast_x.json": b"{}"}) == []


def test_append_only_must_grow_at_end():
    p = "data/odds/quotes_v2.csv"
    assert ev([("M", p)], {p: b"h\n1\n"}, {p: b"h\n1\n2\n"}) == []
    assert ev([("M", p)], {p: b"h\n1\n"}, {p: b"h\n9\n2\n"})


def test_immutable_records_cannot_change_or_disappear():
    assert ev([("M", "data/odds/raw/totals_20261015T180351Z_current.json")], new={"x": b""})
    assert ev([("D", "data/odds/raw/totals_20261015T180351Z_current.json")])
    assert ev([("D", "data/odds/quotes_v2.csv")])
    assert ev([("M", "data/predictions/totals_index.json")]) == []      # the one mutable index


def test_secret_detection():
    p = "data/odds/raw/totals_y.json"
    assert ev([("A", p)], new={p: b"...apiKey=abc..."})
    assert ev([("A", p)], new={p: b"value KEY123 here"}, secrets=["KEY123"])


def git(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)


def test_cli_against_a_real_records_checkout(tmp_path):
    r = tmp_path / "records"; (r / "data/odds/raw").mkdir(parents=True)
    git(tmp_path, "init", "-q", str(r))
    (r / "data/odds/raw/totals_a.json").write_text("{}")
    (r / "data/odds/quotes_v2.csv").write_text("h\n1\n")
    git(r, "add", "-A"); git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    (r / "data/odds/quotes_v2.csv").write_text("h\n1\n2\n")
    (r / "data/odds/raw/totals_b.json").write_text("{}")
    git(r, "add", "-A")
    assert main(["--repo", str(r)]) == 0
    (r / "src.py").write_text("x")
    (r / "data/odds/raw/totals_a.json").write_text('{"changed": 1}')
    git(r, "add", "-A")
    assert main(["--repo", str(r)]) == 1
