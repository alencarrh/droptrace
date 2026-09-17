"""The browser launcher, including its WSL handling."""

from __future__ import annotations

import threading
import time

import pytest

from droptrace import launcher


def test_local_urls_for_a_specific_bind():
    assert launcher.local_urls(8777, "127.0.0.1") == [("local", "http://127.0.0.1:8777/")]
    assert launcher.local_urls(9000, "192.168.1.5") == [("local", "http://192.168.1.5:9000/")]


def test_local_urls_for_a_wildcard_bind_lists_real_addresses(monkeypatch):
    """`http://0.0.0.0:8777/` is not openable, so a wildcard bind must resolve to
    the actual addresses -- that is what another device needs."""
    monkeypatch.setattr(
        launcher.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("192.168.1.51", 0))],
    )
    urls = launcher.local_urls(8777, "0.0.0.0")
    assert urls[0] == ("local", "http://127.0.0.1:8777/")
    assert ("network", "http://192.168.1.51:8777/") in urls
    # Never advertise the wildcard or a duplicate loopback.
    assert all("0.0.0.0" not in url for _, url in urls)
    assert len(urls) == len({url for _, url in urls})


def test_local_urls_survives_a_broken_resolver(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("no resolver")

    monkeypatch.setattr(launcher.socket, "getaddrinfo", boom)
    urls = launcher.local_urls(8777, "0.0.0.0")
    # Still returns the loopback entry rather than raising.
    assert urls[0] == ("local", "http://127.0.0.1:8777/")


def test_is_wsl_detects_env(monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert launcher.is_wsl() is True


def test_is_wsl_without_env_reads_proc_version(monkeypatch):
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    # Must decide from /proc/version and never raise.
    assert isinstance(launcher.is_wsl(), bool)


def test_is_wsl_survives_unreadable_proc_version(monkeypatch):
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    real_open = open

    def fake_open(path, *args, **kwargs):
        if str(path) == "/proc/version":
            raise OSError("no proc here")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    assert launcher.is_wsl() is False


def test_open_browser_can_be_disabled(monkeypatch):
    monkeypatch.setenv("DROPTRACE_NO_BROWSER", "1")
    assert "skipped" in launcher.open_browser("http://127.0.0.1:8777/")


def test_spawn_reports_missing_binary():
    assert launcher._spawn(["definitely-not-a-real-binary-xyz", "arg"]) is False


def test_spawn_runs_a_real_command():
    # `true` exists everywhere; spawning it must not raise.
    assert launcher._spawn(["true"]) is True


def test_open_browser_falls_back_without_a_browser(monkeypatch):
    """Nothing available must return a label, never raise."""
    monkeypatch.delenv("DROPTRACE_NO_BROWSER", raising=False)
    monkeypatch.setattr(launcher, "is_wsl", lambda: False)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: False)
    assert launcher.open_browser("http://127.0.0.1:8777/") == "not opened"


def test_open_browser_uses_wslview_when_present(monkeypatch):
    monkeypatch.delenv("DROPTRACE_NO_BROWSER", raising=False)
    monkeypatch.setattr(launcher, "is_wsl", lambda: True)
    calls: list[list[str]] = []
    monkeypatch.setattr(launcher, "_spawn", lambda cmd: (calls.append(cmd), True)[1])
    assert launcher.open_browser("http://x/") == "wslview"
    assert calls[0][0] == "wslview"


def test_open_browser_prefers_windows_over_webbrowser_in_wsl(monkeypatch):
    monkeypatch.delenv("DROPTRACE_NO_BROWSER", raising=False)
    monkeypatch.setattr(launcher, "is_wsl", lambda: True)
    monkeypatch.setattr(launcher, "_spawn", lambda cmd: cmd[0] == launcher._WINDOWS_CMD)
    used: list[str] = []
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: used.append(url) or True)
    assert launcher.open_browser("http://x/") == "Windows browser"
    assert used == []  # webbrowser is not even tried


def test_wait_and_open_waits_for_the_health_endpoint(monkeypatch):
    attempts: list[str] = []

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(url, timeout=None):
        attempts.append(url)
        if len(attempts) < 3:
            raise OSError("not up yet")
        return FakeResponse()

    monkeypatch.setattr(launcher.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(launcher, "open_browser", lambda url: "default browser")
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)

    lines: list[str] = []
    thread = launcher.wait_and_open(
        "http://127.0.0.1:8777/", "http://127.0.0.1:8777/api/health", timeout=5, log=lines.append
    )
    assert isinstance(thread, threading.Thread)
    thread.join(timeout=5)
    assert len(attempts) == 3
    assert "default browser" in lines[-1]


def test_wait_and_open_gives_up_and_says_so(monkeypatch):
    monkeypatch.setattr(
        launcher.urllib.request, "urlopen",
        lambda url, timeout=None: (_ for _ in ()).throw(OSError("nope")),
    )
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)
    lines: list[str] = []
    thread = launcher.wait_and_open("http://127.0.0.1:9/", timeout=0.05, log=lines.append)
    thread.join(timeout=5)
    assert any("open" in line and "manually" in line for line in lines)
