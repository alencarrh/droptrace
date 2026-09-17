"""HTTP API tests. No real measurements: every probe is disabled."""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from droptrace.api import _resolve_since, create_app
from droptrace.config import Settings
from droptrace.sampler import Sampler
from droptrace.storage import Store


def seed(db_path, rounds=6):
    """Write a few rounds in a throwaway loop before the app opens the file."""

    async def _seed():
        store = Store(db_path)
        await store.connect()
        base = time.time() - rounds * 2
        for round_id in range(1, rounds + 1):
            ts = base + round_id * 2
            down = round_id == rounds  # the last round is a full outage
            await store.add_many([
                {
                    "ts": ts, "kind": "latency", "target": "cloudflare", "role": "internet",
                    "round_id": round_id, "ok": not down, "probe_ms": None if down else 10.0 + round_id,
                    "jitter_ms": 0.4, "loss_pct": 100.0 if down else 0.0, "sent": 2,
                    "recv": 0 if down else 2, "error": "timeout" if down else None,
                },
                {
                    "ts": ts, "kind": "latency", "target": "google", "role": "internet",
                    "round_id": round_id, "ok": not down, "probe_ms": None if down else 20.0 + round_id,
                    "jitter_ms": 0.6, "loss_pct": 100.0 if down else 0.0, "sent": 2,
                    "recv": 0 if down else 2, "error": "timeout" if down else None,
                },
                {
                    "ts": ts, "kind": "latency", "target": "resolver", "role": "local",
                    "round_id": round_id, "ok": True, "probe_ms": 4.0, "jitter_ms": 0.1,
                    "loss_pct": 0.0, "sent": 2, "recv": 2,
                },
            ])
            if round_id == rounds:
                incident_id = await store.open_incident(
                    kind="internet", scope="isp", started_at=ts,
                    targets_total=3, targets_failed=2,
                    failed_targets=["cloudflare", "google"], detail={"errors": {"cloudflare": "timeout"}},
                )
                await store.close_incident(incident_id, ts + 7.5)
        for i in range(3):
            await store.add({
                "ts": base + i * 30, "kind": "speed", "tier": "sustained",
                "target": "speedtest", "role": "internet",
                "ok": True, "download_mbps": 90.0 + i, "upload_mbps": 40.0 + i,
                "download_bytes": 10 * 1024 * 1024, "upload_bytes": 5 * 1024 * 1024,
            })
        await store.close()

    asyncio.run(_seed())


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "api.db"
    seed(db_path)
    settings = Settings(
        db_path=db_path,
        auto_start=False,
        enable_latency=False,
        enable_download=False,
        enable_upload=False,
        probe_gateway=False,
    )
    store = Store(db_path)
    sampler = Sampler(settings, store)
    app = create_app(settings, store, sampler)
    # A real loopback client: the mutating endpoints trust localhost (it is the
    # operator's own screen) and require the token from the LAN.
    with TestClient(app, client=("127.0.0.1", 51234)) as test_client:
        yield test_client


@pytest.fixture
def lan_client(tmp_path):
    """The same app, seen from a LAN address rather than loopback."""
    db_path = tmp_path / "lan.db"
    seed(db_path)
    settings = Settings(
        db_path=db_path, auto_start=False, enable_latency=False,
        enable_download=False, enable_upload=False, probe_gateway=False,
    )
    store = Store(db_path)
    sampler = Sampler(settings, store)
    app = create_app(settings, store, sampler)
    with TestClient(app, client=("192.168.1.52", 51234)) as test_client:
        yield test_client


def test_health(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["running"] is False
    assert body["samples"] == 21


def test_index_and_static_assets(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "DropTrace" in page.text
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/vendor/chart.umd.min.js").status_code == 200
    assert client.get("/favicon.svg").status_code == 200


def test_the_ui_is_never_served_from_a_stale_cache(client):
    """A cached app.js keeps running last week's dashboard with no way to tell.

    An "invisible" revalidate costs one 304 and makes a refresh mean refresh.
    """
    for path in ("/", "/favicon.svg", "/static/app.js", "/static/styles.css"):
        response = client.get(path)
        assert response.headers.get("cache-control") == "no-cache, must-revalidate", path


def test_config_exposes_settings_and_updatable_keys(client):
    body = client.get("/api/config").json()
    assert body["settings"]["latency_interval"] == 5.0
    assert body["settings"]["sustained_interval"] == 3600.0
    assert "1h" in body["windows"]
    assert "latency_interval" in body["updatable"]


def test_status_reports_stored_counts_and_targets(client):
    body = client.get("/api/status").json()
    assert body["running"] is False
    assert body["stored"]["total"] == 21
    assert body["stored"]["latency"] == 18
    assert body["stored"]["speed"] == 3
    assert body["current_incidents"] == []
    assert isinstance(body["targets"], list)


def test_summary_shape(client):
    body = client.get("/api/summary?window=all").json()
    assert body["counts"]["latency"] == 18
    # Only internet-role samples with a value: the final round failed outright,
    # so its two internet samples carry no probe_ms and are not counted.
    assert body["probe_ms"]["n"] == 10
    assert body["probe_ms"]["min"] == pytest.approx(11.0)
    assert body["probe_ms"]["max"] == pytest.approx(25.0)
    assert body["download_mbps"]["avg"] == pytest.approx(91.0)
    assert body["uptime"]["rounds"] == 6
    assert body["uptime"]["rounds_down"] == 1
    assert body["uptime"]["up_pct"] == pytest.approx(83.333, abs=0.01)
    assert body["uptime"]["downtime_pct"] == pytest.approx(16.667, abs=0.01)
    assert body["incidents"]["count"] == 1
    assert body["incidents"]["downtime_s"] == pytest.approx(7.5)
    assert body["incidents"]["by_kind"]["dns"]["count"] == 0
    assert set(body["by_target"]) == {"cloudflare", "google", "resolver"}


def test_series_is_per_target_with_a_round_timeline(client):
    body = client.get("/api/series?window=all&max_points=600").json()
    assert sorted(body["latency"]["targets"]) == ["cloudflare", "google", "resolver"]
    assert len(body["latency"]["series"]["cloudflare"]) == 6
    assert len(body["latency"]["series"]["resolver"]) == 6
    assert body["rounds"]["total"] == 6
    assert any(not r["up"] for r in body["rounds"]["rounds"])
    assert body["incidents"][0]["scope"] == "isp"
    assert body["incidents"][0]["failed_targets"] == ["cloudflare", "google"]


def test_samples_endpoint_filters_by_kind(client):
    body = client.get("/api/samples?kind=speed&limit=5").json()
    assert len(body["samples"]) == 3
    assert {s["kind"] for s in body["samples"]} == {"speed"}
    assert client.get("/api/samples?kind=bogus").status_code == 422


def test_rounds_endpoint(client):
    body = client.get("/api/rounds?limit=2").json()
    assert len(body["rounds"]) == 2
    assert {s["target"] for s in body["rounds"][0]["samples"]} == {"cloudflare", "google", "resolver"}


def test_incidents_endpoint(client):
    body = client.get("/api/incidents?window=all").json()
    assert len(body["incidents"]) == 1
    incident = body["incidents"][0]
    assert incident["kind"] == "internet"
    assert incident["scope"] == "isp"
    assert incident["duration_s"] == pytest.approx(7.5)
    assert incident["label"]
    assert isinstance(incident["detail"], dict)
    assert body["stats"]["count"] == 1
    assert body["stats"]["by_scope"] == {"isp": 1}


def test_csv_exports(client):
    samples = client.get("/api/export.csv?window=all")
    assert samples.status_code == 200
    assert "attachment" in samples.headers["content-disposition"]
    assert samples.text.startswith("id,ts,kind,target,role,ok")
    assert len(samples.text.strip().splitlines()) == 22

    outages = client.get("/api/incidents.csv?window=all")
    assert outages.status_code == 200
    assert "outages" in outages.headers["content-disposition"]
    assert "started_iso" in outages.text.splitlines()[0]
    assert "isp" in outages.text


def test_reset_clears_samples_and_incidents(client):
    assert client.post("/api/reset?confirm=yes").json()["removed"] == 21
    assert client.get("/api/status").json()["stored"]["total"] == 0
    assert client.get("/api/incidents?window=all").json()["incidents"] == []


def test_control_stop_is_idempotent(client):
    assert client.post("/api/control/stop").json()["running"] is False


def test_control_config_changes_settings_live(client):
    response = client.post("/api/control/config", json={"latency_interval": 1.0, "quick_interval": 60})
    body = response.json()
    assert body["applied"] == {"latency_interval": 1.0, "quick_interval": 60.0}
    assert body["rejected"] == []
    assert body["settings"]["latency_interval"] == 1.0

    status = client.get("/api/status").json()
    assert status["latency_interval"] == 1.0


def test_control_config_accepts_targets_and_payload_settings(client):
    """Everything the dashboard's advanced panel sends must be applicable."""
    payload = {
        "public_targets": "9.9.9.9:443",
        "extra_targets": "router=10.0.0.1:80",
        "dns_probe_name": "example.org",
        "probe_gateway": True,
        "probe_resolver": False,
        "download_seconds": 3.0,
        "upload_seconds": 0.5,
        "quick_interval": 300,
        "quick_download_bytes": 1048576,
        "streams": 2,
        "target_timeout": 0.5,
        "incident_min_rounds": 2,
    }
    body = client.post("/api/control/config", json=payload).json()
    assert body["rejected"] == []
    for key, value in payload.items():
        assert body["applied"][key] == value, key

    settings = client.get("/api/config").json()["settings"]
    assert settings["public_targets"] == "9.9.9.9:443"
    assert settings["download_seconds"] == 3.0
    assert settings["probe_resolver"] is False

    # The rebuilt target list reflects the change.
    targets = {t["name"]: t for t in client.get("/api/targets").json()["targets"]}
    assert "quad9" in targets
    assert targets["router"]["address"] == "10.0.0.1:80"
    assert "resolver" not in targets


def test_control_config_reports_rejected_keys(client):
    body = client.post("/api/control/config", json={"nope": 1, "quick_interval": 120}).json()
    assert body["rejected"] == ["nope"]
    assert body["applied"] == {"quick_interval": 120.0}


def test_probe_runs_inline_when_sampler_is_idle(client):
    body = client.post("/api/probe?kind=all").json()
    assert body["running"] is False
    assert body["ran"] == "all"


def test_resolve_since_accepts_presets_and_seconds():
    now = time.time()
    assert _resolve_since("15m", None) == pytest.approx(now - 900, abs=5)
    assert _resolve_since("all", None) == 0.0
    assert _resolve_since("2h", None) == pytest.approx(now - 7200, abs=5)
    assert _resolve_since("7d", None) == pytest.approx(now - 604800, abs=5)
    assert _resolve_since("120", None) == pytest.approx(now - 120, abs=5)
    assert _resolve_since(None, 12345.0) == 12345.0
    assert _resolve_since("nonsense", None) == pytest.approx(now - 3600, abs=5)


def test_summary_and_series_accept_an_explicit_until(client):
    """What the timeline brush sends: an explicit start and end."""
    now = time.time()
    full = client.get("/api/summary?window=all").json()
    bounded = client.get(f"/api/summary?since={now - 3600:.3f}&until={now - 900:.3f}").json()
    assert bounded["until"] is not None
    assert bounded["counts"]["total"] <= full["counts"]["total"]

    series = client.get(
        f"/api/series?since={now - 3600:.3f}&until={now - 900:.3f}&max_points=600"
    ).json()
    assert series["until"] is not None
    assert series["latency"]["total"] <= full["counts"]["latency"]

    incidents = client.get(f"/api/incidents?since=0&until={now - 900:.3f}").json()
    assert isinstance(incidents["incidents"], list)
    assert incidents["stats"]["count"] == 0


def test_export_respects_until(client):
    now = time.time()
    bounded = client.get(f"/api/export.csv?since=0&until={now - 900:.3f}")
    everything = client.get("/api/export.csv?window=all")
    assert len(bounded.text.strip().splitlines()) < len(everything.text.strip().splitlines())


def test_the_live_device_endpoint_answers_now_not_the_page_load(client):
    """/api/devices/live is what keeps the on/off pill from going stale.

    It reports the recent past only: a device whose last report is two hours old
    must be absent rather than listed with a stale-but-plausible age.
    """
    body = client.get("/api/devices/live").json()
    assert body["now"] == pytest.approx(time.time(), abs=5)
    assert [d["source"] for d in body["devices"]] == ["local"]
    assert 0 <= body["devices"][0]["last_seen_ago_s"] < 60, "the seed's last round is 'now'"

    device = client.post("/api/agent/devices", json={"source": "phone-wifi"}).json()
    headers = {"X-Agent-Token": device["token"]}

    def report(ts):
        return client.post(
            "/api/agent",
            json={"probes": [{"ts": ts, "target": "github-com", "role": "internet",
                              "ok": True, "probe_ms": 12.0}]},
            headers=headers,
        )

    assert report(time.time() - 7200).json()["stored"] == 1
    assert "phone-wifi" not in {d["source"] for d in client.get("/api/devices/live").json()["devices"]}

    assert report(time.time()).json()["stored"] == 1
    listed = {d["source"]: d["last_seen_ago_s"] for d in client.get("/api/devices/live").json()["devices"]}
    assert listed["phone-wifi"] < 60
    assert sorted(listed) == ["local", "phone-wifi"]


def test_stats_page_and_endpoint(client):
    page = client.get("/stats")
    assert page.status_code == 200
    assert "DropTrace · statistics" in page.text
    for anchor in ("stat-cards", "daily-body", "target-body", "inc-hours",
                   "stats-refresh", "stats-updated"):
        assert anchor in page.text

    body = client.get("/api/stats?window=all").json()
    for key in ("window", "coverage", "totals", "daily", "targets", "incidents",
                "throughput", "failures", "devices", "paths", "loss"):
        assert key in body, key
    assert body["loss"]["count"] == 0 and body["loss"]["rows"] == []
    assert body["window"]["label"] == "all"
    assert set(body["throughput"]) == {"quick", "sustained"}
    assert {"count", "downtime_s", "longest_s", "histogram", "by_hour"} <= set(body["incidents"])
    assert {"total", "top_errors", "by_target"} <= set(body["failures"])
    assert body["totals"]["rounds"] >= 1
    assert "paths" in body, "the hop lists travel with the page payload"
    assert set(body["paths"]) == {"drop", "baseline", "count"}


def test_traces_can_be_listed_and_taken_on_demand(client, monkeypatch):
    """The page shows what was traced; the button traces now.

    A manual trace must land in the record like any other, because "I saw it
    break and ran a trace" is the same evidence as the sampler's own.
    """
    empty = client.get("/api/traces?window=all").json()
    assert empty == {"traces": [], "total": 0}

    taken = {
        "ts": time.time(), "host": "1.1.1.1", "tracer": "fake", "reached": False,
        "hops": 2, "answered": 1, "max_hops": 20, "last_hop": "100.64.0.1",
        "duration_ms": 12.0, "error": None,
        "hop_list": [{"ttl": 2, "host": "100.64.0.1", "rtt_ms": 9.0, "note": ""}],
    }

    async def fake_trace(host, **_kwargs):
        assert host == "1.1.1.1"
        return dict(taken)

    monkeypatch.setattr("droptrace.api.trace_path", fake_trace)
    body = client.post("/api/trace").json()
    assert body["trigger"] == "manual" and body["id"] > 0
    assert body["last_hop"] == "100.64.0.1"

    listed = client.get("/api/traces").json()
    assert listed["total"] == 1
    assert listed["traces"][0]["trigger"] == "manual"
    assert listed["traces"][0]["hop_list"] == taken["hop_list"]
    assert client.get("/api/traces?trigger=drop").json()["traces"] == []


def test_a_lan_client_cannot_trace(lan_client):
    """Tracing runs a process on the host; the LAN does not get to ask for it."""
    response = lan_client.post("/api/trace")
    assert response.status_code in (401, 403), response.text
    assert lan_client.get("/api/traces").status_code == 200, "reading stays open"


# --------------------------------------------------------- remote vantage points
def test_a_lan_client_cannot_reset_or_start_tests(lan_client):
    """Reads are open by choice; anything that changes state is not.

    Anyone on the network being able to wipe the record or start a speed test
    would make the evidence worthless -- and cost data.
    """
    assert lan_client.get("/api/health").status_code == 200
    for path in ("/api/reset?confirm=yes", "/api/control/start", "/api/control/stop", "/api/probe"):
        assert lan_client.post(path).status_code == 401, path
    assert lan_client.post("/api/control/config", json={"latency_interval": 1}).status_code == 401


def test_the_agent_token_is_not_readable_from_the_lan(lan_client):
    assert lan_client.get("/api/agent/info").status_code == 403


def test_a_device_token_decides_the_label(client):
    """The token is the device, so a body cannot claim another device's name.

    This is the whole reason for one token per device: filing the laptop's drops
    under the phone's label would silently corrupt the comparison the feature
    exists to produce.
    """
    device = client.post("/api/agent/devices", json={"source": "s24-phone"}).json()
    assert device["source"] == "s24-phone" and device["token"]
    assert device["links"]["browser_agent"].endswith(f"/agent?token={device['token']}")
    token = device["token"]

    body = {
        # A body trying to file under something else is simply ignored.
        "source": "local",
        "platform": "Android",
        "agent": "browser",
        "probes": [
            {"ts": time.time(), "target": "github-com", "role": "internet",
             "ok": False, "error": "TypeError: Failed to fetch"},
            {"ts": time.time(), "target": "github-com", "role": "internet",
             "ok": True, "probe_ms": 31.5},
        ],
    }
    stored = client.post("/api/agent", json=body, headers={"X-Agent-Token": token}).json()
    assert stored == {"stored": 2, "source": "s24-phone"}

    rows = client.get("/api/samples?kind=latency&limit=20").json()["samples"]
    remote = [r for r in rows if r.get("source") == "s24-phone"]
    assert len(remote) == 2 and sum(1 for r in remote if not r["ok"]) == 1
    assert not [r for r in rows if r.get("source") == "local" and r["ok"] is False and r["target"] == "github-com"]

    # Unknown or revoked tokens are refused outright.
    assert client.post("/api/agent", json=body, headers={"X-Agent-Token": "nope"}).status_code == 401
    assert client.post("/api/agent", json=body).status_code == 401
    assert client.delete("/api/agent/devices/s24-phone").json() == {"removed": "s24-phone"}
    assert client.post("/api/agent", json=body, headers={"X-Agent-Token": token}).status_code == 401

    # Labels stay sane, and a device cannot be called "local".
    for bad in ("local", "has spaces!", "", "x" * 40):
        assert client.post("/api/agent/devices", json={"source": bad}).status_code == 422, bad


def test_whoami_tells_the_device_which_label_is_its_own(client):
    token = client.post("/api/agent/devices", json={"source": "macbook-eth"}).json()["token"]
    who = client.get(f"/api/agent/whoami?token={token}").json()
    assert who["source"] == "macbook-eth"
    assert client.get("/api/agent/whoami?token=wrong").status_code == 401


def test_rotating_a_token_invalidates_the_old_one(client):
    first = client.post("/api/agent/devices", json={"source": "s24-phone"}).json()["token"]
    second = client.post("/api/agent/devices", json={"source": "s24-phone"}).json()["token"]
    assert first != second
    probes = [{"ts": time.time(), "target": "x", "role": "internet", "ok": True, "probe_ms": 1.0}]
    assert client.post("/api/agent", json={"probes": probes},
                       headers={"X-Agent-Token": first}).status_code == 401
    assert client.post("/api/agent", json={"probes": probes},
                       headers={"X-Agent-Token": second}).status_code == 200


def test_the_dashboard_lists_devices_with_their_links(client):
    client.post("/api/agent/devices", json={"source": "s24-phone"})
    info = client.get("/api/agent/info").json()
    assert info["control_token"]
    assert [d["source"] for d in info["devices"]] == ["s24-phone"]
    assert info["devices"][0]["links"]["python_agent"].startswith("python3 -m droptrace agent")


def test_the_agent_page_asks_before_it_measures(client):
    """The mistake to catch is opening the phone's link on the laptop."""
    page = client.get("/agent").text
    assert 'id="agent-confirm"' in page
    assert 'id="btn-continue"' in page
    assert "nothing is measured until you continue" in page
    # Keeping the screen on: the button, and the honest status line beside it.
    assert 'id="btn-awake"' in page and 'id="awake-status-line"' in page
    assert "Wake Lock API" in page


def test_stats_auto_refresh_is_opt_in_and_bookmarkable(client):
    """The page must not poll unless asked, and the choice must live in the URL."""
    js = client.get("/static/stats.js").text
    for label in ('key: "off"', 'key: "10s"', 'key: "30s"', 'key: "1m"', 'key: "10m"'):
        assert label in js, label
    assert 'params.get("refresh")' in js, "the interval should be readable from the URL"
    assert 'visibilityState === "visible"' in js, "polling should pause on a hidden tab"


def test_every_page_can_keep_the_display_awake(client):
    """The dashboard works on localhost, where the Wake Lock API is available."""
    for path, marker in (("/", "btn-awake"), ("/stats", "btn-awake"), ("/agent", "btn-awake")):
        page = client.get(path).text
        assert marker in page, path
        assert "awake-status" in page, path
        assert "/static/keepawake.js" in page, path
    script = client.get("/static/keepawake.js").text
    assert "wakeLock.request" in script
    assert "captureStream" in script, "the plain-HTTP fallback is what the phone relies on"
    assert "not requested" in script, "the status line must say what is in force"


def test_every_page_offers_the_style_switch(client):
    """One style, two modes, and the switch is on every page.

    The mode has to be applied before anything paints, and it must not be able
    to *fail* into a broken page: with no stored choice and no ?theme= the light
    mode is what you get.
    """
    import re

    for path in ("/", "/stats", "/agent"):
        page = client.get(path).text
        assert 'id="theme-chips"' in page, path
        # The default is in the markup too, so a page whose theme.js never loads
        # still renders the modern style rather than the bare base sheet.
        assert 'data-style="modern" data-theme="light"' in page, path
        # Loaded in <head>, so the mode lands before the first paint.
        head = page.split("</head>")[0]
        assert "/static/theme.js" in head, path
        assert "/static/theme-modern.css" in head, path

    script = client.get("/static/theme.js").text
    assert 'dataset.style = STYLE' in script and "localStorage" in script
    for key in ("light", "dark"):
        assert f'key: "{key}"' in script, key
    assert 'const DEFAULT = "light"' in script, "light is the default mode"
    # Bookmarks and stored choices from the earlier three-style switch still work.
    assert "modern: \"light\"" in script and "report: \"light\"" in script

    # One style sheet, and every selector of every rule scoped to it: nothing
    # in it can reach the base sheet.
    response = client.get("/static/theme-modern.css")
    assert response.status_code == 200
    scope = 'html[data-style="modern"]'
    body = re.sub(r"/\*.*?\*/", "", response.text, flags=re.S)
    preludes = [chunk.rsplit("}", 1)[-1] for chunk in body.split("{")][:-1]
    selectors = [part.strip() for prelude in preludes for part in prelude.split(",")]
    selectors = [selector for selector in selectors if selector]
    assert selectors, "the style sheet has no rules"
    for selector in selectors:
        assert selector.startswith(scope), selector
    # Both modes are token blocks on the same family.
    for mode in ("light", "dark"):
        assert f'{scope}[data-theme="{mode}"] {{' in response.text, mode
