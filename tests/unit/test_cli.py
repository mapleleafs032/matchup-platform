import json

import pytest

from edge.cli import main


def test_config_check_reports_secret_presence_not_values(monkeypatch, capsys) -> None:
    monkeypatch.setenv("EDGE_ODDS_API_KEY", "SECRET123")
    monkeypatch.delenv("EDGE_DATABASE_URL", raising=False)
    assert main(["config", "check"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["odds_api_key_set"] is True and "SECRET123" not in out
    assert data["enabled_sports"] == ["nfl"]


def test_missing_database_url_is_a_clean_error(monkeypatch, capsys) -> None:
    monkeypatch.delenv("EDGE_DATABASE_URL", raising=False)
    monkeypatch.chdir("/")  # ensure no .env file is picked up
    assert main(["db", "seed"]) == 1
    assert "EDGE_DATABASE_URL is not set" in capsys.readouterr().err


def test_disabled_sport_refused(capsys) -> None:
    assert main(["ingest", "odds", "--sport", "mlb"]) == 2
    assert "not enabled" in capsys.readouterr().err


def test_sources_list_shows_honest_status(capsys) -> None:
    assert main(["sources", "list"]) == 0
    out = capsys.readouterr().out
    assert "odds_api" in out and "implemented" in out
    assert "vsin_splits" in out and "blocked" in out


def test_unknown_command_exits_nonzero() -> None:
    with pytest.raises(SystemExit):
        main(["nope"])


def test_logs_go_to_stderr_not_stdout(capsys) -> None:
    import logging

    from edge.core.logging import configure_logging, log_event

    configure_logging()
    log_event(logging.getLogger("t"), logging.INFO, "hello", n=1)
    captured = capsys.readouterr()
    assert "hello" not in captured.out
