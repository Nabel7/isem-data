"""Tests for the download run: one more try for anything that failed."""

from datetime import date

from isem_data import __main__ as cli
from isem_data import storage

START, END = date(2026, 11, 10), date(2026, 11, 16)


def test_a_dataset_that_fails_once_is_tried_again_and_recorded_as_a_success(tmp_path, monkeypatch):
    calls = []

    def flaky(dataset, start, end, data_dir):
        calls.append(dataset)
        if dataset == "demand" and calls.count("demand") == 1:
            raise cli.NoData("no rows at all")  # EirGrid answering empty, once
        return "fine"

    monkeypatch.setattr(cli, "download", flaky)

    failed = cli.run(["wind", "demand", "prices"], START, END, tmp_path)

    assert failed == []
    assert calls == ["wind", "demand", "prices", "demand"]  # the retry comes at the end
    status = storage.read_status(tmp_path)
    assert "last_error" not in status["demand"]


def test_a_dataset_that_fails_twice_is_reported(tmp_path, monkeypatch):
    def broken(dataset, start, end, data_dir):
        if dataset == "demand":
            raise cli.NoData("no rows at all")
        return "fine"

    monkeypatch.setattr(cli, "download", broken)

    assert cli.run(["wind", "demand"], START, END, tmp_path) == ["demand"]
    assert storage.read_status(tmp_path)["demand"]["last_error"] == "NoData: no rows at all"
