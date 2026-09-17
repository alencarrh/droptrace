"""Settings: environment overrides, CLI flags and duration parsing."""

from __future__ import annotations

import pytest

from droptrace.__main__ import build_settings, make_parser, parse_duration
from droptrace.config import Settings, parse_seconds


def test_parse_seconds_accepts_suffixes():
    assert parse_seconds("90") == 90
    assert parse_seconds("90s") == 90
    assert parse_seconds("10m") == 600
    assert parse_seconds("2h") == 7200
    assert parse_seconds("8h") == 28800
    assert parse_seconds("1d") == 86400
    assert parse_seconds("0") == 0
    assert parse_seconds("all") == 0
    assert parse_seconds("forever") == 0
    assert parse_seconds(45) == 45
    assert parse_seconds("nonsense") is None
    assert parse_seconds("") is None
    assert parse_seconds(None) is None


def test_parse_duration_raises_on_garbage():
    assert parse_duration("15m") == 900
    with pytest.raises(Exception):
        parse_duration("soon")


def test_defaults_are_tuned_for_catching_drops():
    settings = Settings()
    # Long unattended runs are the point: no default time limit.
    assert settings.duration == 0
    # A ~5s drop must span several rounds to be timed accurately.
    assert settings.latency_interval <= 5
    # Throughput tests must not dominate a multi-hour run.
    # Two throughput tiers: a cheap burst often, a sustained test rarely.
    assert settings.quick_interval >= 60
    assert settings.sustained_interval >= settings.quick_interval
    assert settings.quick_download_bytes and settings.quick_upload_bytes
    assert settings.incident_min_rounds >= 1
    assert settings.enable_latency is True
    # Several targets, including a local hop, so outages can be attributed.
    assert settings.public_targets.count(",") >= 1
    assert settings.probe_resolver is True
    assert settings.dns_probe_name
    # Throughput tests are duration-based, so they measure sustained speed
    # rather than a sub-second burst.
    assert settings.download_seconds >= 5
    assert settings.upload_seconds >= 5
    assert settings.max_test_bytes == 0
    assert "latency round every" in settings.describe()


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("DROPTRACE_LATENCY_INTERVAL", "1.5")
    monkeypatch.setenv("DROPTRACE_QUICK_INTERVAL", "300")
    monkeypatch.setenv("DROPTRACE_SUSTAINED_INTERVAL", "7200")
    monkeypatch.setenv("DROPTRACE_DURATION", "28800")
    monkeypatch.setenv("DROPTRACE_STREAMS", "4")
    monkeypatch.setenv("DROPTRACE_INCIDENT_MIN_ROUNDS", "2")
    monkeypatch.setenv("DROPTRACE_PUBLIC_TARGETS", "9.9.9.9:443")
    monkeypatch.setenv("DROPTRACE_PROBE_GATEWAY", "false")
    monkeypatch.setenv("DROPTRACE_ENABLE_UPLOAD", "false")
    monkeypatch.setenv("DROPTRACE_DB_PATH", "/tmp/custom-droptrace.db")
    monkeypatch.setenv("DROPTRACE_NO_SUCH", "ignored")

    settings = Settings.from_env()
    assert settings.latency_interval == 1.5
    assert settings.quick_interval == 300
    assert settings.sustained_interval == 7200
    assert settings.duration == 28800
    assert settings.streams == 4
    assert settings.incident_min_rounds == 2
    assert settings.public_targets == "9.9.9.9:443"
    assert settings.probe_gateway is False
    assert settings.enable_upload is False
    assert str(settings.db_path) == "/tmp/custom-droptrace.db"
    assert settings.enable_download is True


def test_settings_from_env_ignores_bad_values(monkeypatch):
    monkeypatch.setenv("DROPTRACE_LATENCY_INTERVAL", "not-a-number")
    monkeypatch.setenv("DROPTRACE_PING_COUNT", "")
    settings = Settings.from_env()
    assert settings.latency_interval == 5.0
    assert settings.ping_count == 2


def test_cli_flags_override_env(monkeypatch):
    monkeypatch.setenv("DROPTRACE_LATENCY_INTERVAL", "9")
    args = make_parser().parse_args([
        "serve",
        "--latency-interval", "0.5",
        "--quick-interval", "120",
        "--sustained-interval", "7200",
        "--duration", "8h",
        "--streams", "2",
        "--public-targets", "9.9.9.9:443",
        "--extra-targets", "router=10.0.0.1:80",
        "--incident-min-rounds", "2",
        "--no-upload",
        "--no-gateway",
        "--download-seconds", "3",
    ])
    settings = build_settings(args)
    assert settings.latency_interval == 0.5
    assert settings.quick_interval == 120
    assert settings.sustained_interval == 7200
    assert settings.duration == 28800
    assert settings.streams == 2
    assert settings.public_targets == "9.9.9.9:443"
    assert settings.extra_targets == "router=10.0.0.1:80"
    assert settings.incident_min_rounds == 2
    assert settings.enable_upload is False
    assert settings.probe_gateway is False
    assert settings.download_seconds == 3.0


def test_cli_clamps_dangerous_intervals():
    args = make_parser().parse_args(["serve", "--latency-interval", "0"])
    settings = build_settings(args)
    # A zero (or negative) cadence would hammer the network.
    assert settings.latency_interval == 0.2


def test_default_command_is_serve():
    args = make_parser().parse_args([])
    assert args.command is None


def test_to_dict_is_json_friendly():
    payload = Settings().to_dict()
    assert isinstance(payload["db_path"], str)
    assert payload["latency_interval"] == 5.0


def test_adaptive_probing_flags():
    args = make_parser().parse_args([
        "serve", "--latency-interval", "5",
        "--fast-interval", "1", "--fast-hold-seconds", "15", "--fast-max-seconds", "120",
    ])
    settings = build_settings(args)
    assert settings.latency_interval == 5.0
    assert settings.fast_interval == 1.0
    assert settings.fast_hold_seconds == 15.0
    assert settings.fast_max_seconds == 120.0
    # Defaults: normal cadence detects, the fast one only resolves.
    assert Settings().fast_interval == 1.0
    assert Settings().fast_hold_seconds == 10.0
    assert Settings().fast_max_seconds == 300.0
