"""Tests for web requests that try again, and for a daily run that survives a failure."""

import json

import pandas as pd
import pytest
import requests

from isem_data import __main__ as cli
from isem_data import storage, web


class Answer:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def fake_get(outcomes, calls):
    """A stand-in for requests.get that works through `outcomes`: answers or errors."""

    def get(url, params=None, timeout=None):
        calls.append(url)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return get


def test_a_timeout_then_a_server_error_are_retried(monkeypatch):
    calls, waits = [], []
    outcomes = [requests.Timeout("slow"), Answer(503), Answer(200)]
    monkeypatch.setattr(web.requests, "get", fake_get(outcomes, calls))
    monkeypatch.setattr(web.time, "sleep", waits.append)

    assert web.get("https://example.test/file").status_code == 200
    assert len(calls) == 3
    assert waits == [5, 20]  # a longer pause each time


def test_gives_up_after_four_tries_and_raises_the_last_error(monkeypatch):
    calls = []
    monkeypatch.setattr(web.requests, "get", fake_get([requests.Timeout("slow")] * 4, calls))
    monkeypatch.setattr(web.time, "sleep", lambda seconds: None)

    with pytest.raises(requests.Timeout):
        web.get("https://example.test/file")
    assert len(calls) == 4


def test_a_missing_page_is_not_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(web.requests, "get", fake_get([Answer(404)], calls))

    with pytest.raises(requests.HTTPError):
        web.get("https://example.test/nothing")
    assert len(calls) == 1


def test_an_interrupted_save_leaves_the_old_file_intact(monkeypatch, tmp_path):
    old = pd.DataFrame(
        {"timestamp_utc": [pd.Timestamp("2026-11-01", tz="UTC")], "region": ["ALL"],
         "series": ["wind_actual_mw"], "value": [1.0]}
    )  # fmt: skip
    storage.save(old, "wind", data_dir=tmp_path)

    def crash(self, path, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", crash)
    with pytest.raises(OSError):
        storage.save(old.assign(value=2.0), "wind", data_dir=tmp_path)
    monkeypatch.undo()

    pd.testing.assert_frame_equal(storage.load("wind", data_dir=tmp_path), old)


def test_one_dataset_failing_does_not_stop_the_others(monkeypatch, tmp_path, capsys):
    def fake_download(dataset, start, end, data_dir):
        if dataset == "wind":
            raise requests.Timeout("EirGrid is slow today")
        return "10 rows"

    monkeypatch.setattr(cli, "download", fake_download)
    monkeypatch.setattr(
        cli.sys, "argv", ["isem_data", "--datasets", "wind", "demand", "--data-dir", str(tmp_path)]
    )

    with pytest.raises(SystemExit) as stopped:
        cli.main()

    assert stopped.value.code == 1  # so the daily script knows something failed
    out = capsys.readouterr().out
    assert "FAILED wind" in out
    assert "demand: 10 rows" in out  # the dataset after the failure still ran
    status = json.loads((tmp_path / "status.json").read_text())
    assert "EirGrid is slow today" in status["wind"]["last_error"]
    assert "last_success_utc" in status["demand"]


def test_a_failure_keeps_the_record_of_the_last_success(tmp_path):
    storage.record_status("prices", tmp_path, result="48 rows")
    storage.record_status("prices", tmp_path, error="Timeout: slow")
    entry = storage.read_status(tmp_path)["prices"]
    assert entry["result"] == "48 rows"
    assert entry["last_error"] == "Timeout: slow"
    assert {"last_success_utc", "last_failure_utc"} <= set(entry)
