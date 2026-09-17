"""Scheduler behaviour: cadence, incident tracking, target probation, reconfig."""

from __future__ import annotations

import asyncio
import time

import pytest

import droptrace.sampler as sampler_module
from droptrace.config import Settings
from droptrace.sampler import Sampler
from droptrace.storage import Store
from droptrace.targets import Target


def fake_round_factory(state: dict):
    """A stand-in for measure_round driven by ``state``.

    ``state["internet_ok"]`` / ``state["lan_ok"]`` / ``state["local_ok"]`` /
    ``state["dns_ok"]`` control the verdict; ``state["rounds"]`` counts calls.
    """

    async def _measure(settings, targets, round_id=None):
        state["rounds"] = state.get("rounds", 0) + 1
        ts = time.time()
        samples = []
        for target in targets:
            # Per-target control, for the case that matters most here: the
            # primary endpoints dark while the corroboration pool still answers.
            if target.name in state.get("failed_targets", ()):
                ok = False
            elif target.fact_check:
                ok = state.get("fact_check_ok", True)
            elif target.role == "internet":
                ok = state.get("internet_ok", True)
            elif target.role == "lan":
                ok = state.get("lan_ok", True)
            elif target.role == "local":
                ok = state.get("local_ok", True)
            else:
                ok = state.get("dns_ok", True)
            samples.append(
                {
                    "ts": ts,
                    "kind": "latency",
                    "target": target.name,
                    "role": target.role,
                    "round_id": round_id or int(ts * 1000),
                    "ok": ok,
                    "error": None if ok else "timeout on 443",
                    "probe_ms": 12.0 if ok else None,
                    "jitter_ms": 0.3,
                    "loss_pct": 0.0 if ok else 100.0,
                    "sent": 2,
                    "recv": 2 if ok else 0,
                }
            )
        return samples

    return _measure


def fake_speed(down: float = 100.0, up: float = 50.0):
    async def _measure(settings, round_id=None, tier="sustained", trigger="scheduled",
                       progress=None):
        return {
            "ts": time.time(),
            "kind": "speed",
            "tier": tier,
            "trigger": trigger,
            "target": "speedtest",
            "role": "internet",
            "ok": True,
            "download_mbps": down,
            "upload_mbps": up,
            "download_bytes": 10 * 1024 * 1024,
            "upload_bytes": 5 * 1024 * 1024,
            "download_ttfb_ms": 25.0,
            "streams": 1,
        }

    return _measure


TEST_TARGETS = [
    Target("resolver", "local", "resolver", host="127.0.0.1", ports=(9,), guessed=True),
    # The router: the only role that can say the LAN was fine.
    Target("gateway", "lan", "router", host="127.0.0.1", ports=(9,), guessed=True),
    Target("cloudflare", "internet", "cloudflare", host="127.0.0.1", ports=(9,)),
    Target("google", "internet", "google", host="127.0.0.1", ports=(9,)),
    Target("dns", "dns", "dns", kind="dns", host="127.0.0.1", ports=(9,), probe_name="x.com"),
]


@pytest.fixture
async def store(tmp_path):
    instance = Store(tmp_path / "sampler.db")
    await instance.connect()
    yield instance
    await instance.close()


@pytest.fixture
def patched(monkeypatch):
    state: dict = {}
    monkeypatch.setattr(sampler_module, "measure_round", fake_round_factory(state))
    monkeypatch.setattr(sampler_module, "measure_speed", fake_speed())
    monkeypatch.setattr(sampler_module, "build_targets", lambda settings: list(TEST_TARGETS))
    return state


def make_sampler(tmp_path, store, **overrides) -> Sampler:
    defaults = dict(
        db_path=tmp_path / "sampler.db",
        latency_interval=0.05,
        quick_interval=0.0,      # no throughput tests unless a test asks
        sustained_interval=0.0,
        duration=0.0,
        auto_start=False,
    )
    defaults.update(overrides)
    return Sampler(Settings(**defaults), store)


async def test_round_cadence_and_persistence(tmp_path, store, patched):
    sampler = make_sampler(tmp_path, store)
    await sampler.start()
    deadline = time.time() + 5
    while sampler.counts["rounds"] < 3 and time.time() < deadline:
        await asyncio.sleep(0.02)
    await sampler.stop()

    assert sampler.counts["rounds"] >= 3
    # 5 targets per round: resolver, router, two internet targets, DNS.
    assert await store.count("latency") == sampler.counts["rounds"] * 5
    assert sampler.last_round["verdict"]["internet_ok"] is True
    assert sampler.last_round["scope"] == ""


async def test_a_cadenced_target_is_probed_on_its_own_schedule(tmp_path, store, patched, monkeypatch):
    """The uncached DNS query goes upstream for real, so it must not run often.

    Everything else in a round runs every round; this one carries a cadence and
    appears in the record only when it is due.
    """
    slow = Target(
        "dns-upstream", "dns-upstream", "dns", kind="dns", host="127.0.0.1", ports=(9,),
        probe_name="probe-{random}.x.com", expect="negative", cadence=0.15,
    )
    monkeypatch.setattr(sampler_module, "build_targets", lambda settings: [*TEST_TARGETS, slow])
    seen: list[list[str]] = []
    base = fake_round_factory(patched)

    async def counting(settings, targets, round_id=None):
        seen.append([t.name for t in targets])
        return await base(settings, targets, round_id)

    monkeypatch.setattr(sampler_module, "measure_round", counting)
    sampler = make_sampler(tmp_path, store, latency_interval=0.03)
    await sampler.start()
    deadline = time.time() + 5
    while len(seen) < 12 and time.time() < deadline:
        await asyncio.sleep(0.01)
    await sampler.stop()

    assert "dns-upstream" in seen[0], "due on the first round, so the record starts early"
    assert "dns-upstream" not in seen[1], "not due again nine rounds early"
    rounds_with_it = sum(1 for names in seen if "dns-upstream" in names)
    assert 2 <= rounds_with_it < len(seen), "back when due, and not every round"


async def test_a_drop_traces_the_path_and_healthy_rounds_baseline_it(tmp_path, store, patched, monkeypatch):
    """One trace when the drop starts, one while healthy, both kept.

    The drop trace on its own is a hop list through private addresses; the
    baseline is what tells the reader that the path normally goes three hops
    further. Neither may delay a round: the trace runs as a task, and the fake
    here is awaited only by that task.
    """
    seen: list[str] = []

    async def fake_trace(host, **_kwargs):
        seen.append(host)
        # Far slower than a round: if the sampler waited for this, the probe
        # cadence would collapse exactly when the drop is happening.
        await asyncio.sleep(0.2)
        return {
            "ts": time.time(), "host": host, "tracer": "fake", "reached": False,
            "hops": 2, "answered": 1, "max_hops": 20, "last_hop": "192.168.1.1",
            "duration_ms": 5.0, "error": None,
            "hop_list": [{"ttl": 1, "host": "192.168.1.1", "rtt_ms": 1.0, "note": ""}],
        }

    monkeypatch.setattr(sampler_module, "trace_path", fake_trace)
    sampler = make_sampler(
        tmp_path, store, trace_host="1.1.1.1", trace_interval=3600.0,
        trace_cooldown=0.0, fast_interval=0.0,
    )
    await sampler.start()
    try:
        deadline = time.time() + 5
        while await store.count_traces() < 1 and time.time() < deadline:
            await asyncio.sleep(0.02)
        assert seen == ["1.1.1.1"], "the first healthy round takes the baseline"
        assert sampler.counts["rounds"] >= 3, "rounds keep probing while a trace runs"

        # Now the link goes down: the next round opens an incident and traces.
        patched["internet_ok"] = False
        deadline = time.time() + 5
        while await store.count_traces() < 2 and time.time() < deadline:
            await asyncio.sleep(0.02)
    finally:
        await sampler.stop()

    rows = await store.traces()
    assert sorted(r["trigger"] for r in rows) == ["baseline", "drop"]
    drop = next(r for r in rows if r["trigger"] == "drop")
    assert drop["incident_id"] is not None, "the hop list belongs to the incident"
    assert drop["hop_list"] == [{"ttl": 1, "host": "192.168.1.1", "rtt_ms": 1.0, "note": ""}]
    assert drop["reached"] is False and drop["last_hop"] == "192.168.1.1"


async def test_tracing_can_be_switched_off(tmp_path, store, patched, monkeypatch):
    async def explode(*_args, **_kwargs):
        raise AssertionError("tracing is off")

    monkeypatch.setattr(sampler_module, "trace_path", explode)
    sampler = make_sampler(tmp_path, store, trace_host="", trace_interval=3600.0)
    await sampler.start()
    deadline = time.time() + 3
    while sampler.counts["rounds"] < 3 and time.time() < deadline:
        await asyncio.sleep(0.02)
    await sampler.stop()
    assert await store.count_traces() == 0


async def test_stops_after_duration(tmp_path, store, patched):
    sampler = make_sampler(tmp_path, store, duration=0.3)
    await sampler.start()
    deadline = time.time() + 5
    while sampler.running and time.time() < deadline:
        await asyncio.sleep(0.02)
    assert not sampler.running
    assert sampler.stop_reason == "duration"


async def test_outage_is_recorded_and_closed(tmp_path, store, patched):
    sampler = make_sampler(tmp_path, store)
    await sampler.start()
    await asyncio.sleep(0.12)          # healthy rounds
    healthy_rounds = sampler.counts["rounds"]

    patched["internet_ok"] = False     # the internet drops
    await asyncio.sleep(0.2)
    ongoing = await store.current_incidents()
    assert len(ongoing) == 1
    incident = ongoing[0]
    assert incident["kind"] == "internet"
    assert incident["scope"] == "isp"              # the resolver still answered
    assert incident["ongoing"] is True
    assert set(incident["failed_targets"].split(",")) == {"cloudflare", "google"}

    patched["internet_ok"] = True      # ...and comes back
    deadline = time.time() + 3
    while await store.current_incidents() and time.time() < deadline:
        await asyncio.sleep(0.02)
    await sampler.stop()

    assert await store.current_incidents() == []
    rows = await store.incidents(0.0)
    assert len(rows) == 1
    assert rows[0]["ongoing"] is False
    assert rows[0]["duration_s"] > 0
    assert rows[0]["rounds"] >= 1
    assert sampler.counts["rounds"] > healthy_rounds


async def test_outage_scope_is_local_when_everything_fails(tmp_path, store, patched):
    sampler = make_sampler(tmp_path, store)
    patched["internet_ok"] = False
    patched["lan_ok"] = False
    await sampler.start()
    deadline = time.time() + 3
    while not await store.current_incidents() and time.time() < deadline:
        await asyncio.sleep(0.02)
    await sampler.stop()
    rows = await store.incidents(0.0)
    assert rows[0]["scope"] == "local"
    assert "Local network" in rows[0]["scope"] or rows[0]["scope"] == "local"


async def test_dns_outage_is_tracked_independently(tmp_path, store, patched):
    """The internet can be fine over raw IPs while DNS is broken."""
    sampler = make_sampler(tmp_path, store)
    patched["dns_ok"] = False
    await sampler.start()
    deadline = time.time() + 3
    while not await store.current_incidents() and time.time() < deadline:
        await asyncio.sleep(0.02)
    ongoing = await store.current_incidents()
    await sampler.stop()

    assert len(ongoing) == 1
    assert ongoing[0]["kind"] == "dns"
    assert ongoing[0]["scope"] == "dns"


async def test_incident_min_rounds_delays_opening(tmp_path, store, patched):
    sampler = make_sampler(tmp_path, store, incident_min_rounds=3)
    patched["internet_ok"] = False
    await sampler.start()
    await asyncio.sleep(0.07)          # fewer than 3 rounds
    assert await store.current_incidents() == []
    deadline = time.time() + 3
    while not await store.current_incidents() and time.time() < deadline:
        await asyncio.sleep(0.02)
    await sampler.stop()
    rows = await store.incidents(0.0)
    assert rows
    # The recorded start is the first failing round, not the third.
    assert rows[0]["rounds"] >= 3


async def test_guessed_target_is_stood_down_when_it_never_answers(tmp_path, store, patched, monkeypatch):
    sampler = make_sampler(tmp_path, store, target_probation_rounds=2)
    await sampler.start()

    # Make the guessed resolver target alone fail.
    async def only_resolver_fails(settings, targets, round_id=None):
        ts = time.time()
        samples = []
        for target in targets:
            ok = target.name != "resolver"
            samples.append({
                "ts": ts, "kind": "latency", "target": target.name, "role": target.role,
                "round_id": round_id or int(ts * 1000), "ok": ok,
                "probe_ms": 12.0 if ok else None, "error": None if ok else "timeout",
                "sent": 2, "recv": 2 if ok else 0, "loss_pct": 0.0 if ok else 100.0,
            })
        return samples

    monkeypatch.setattr(sampler_module, "measure_round", only_resolver_fails)
    deadline = time.time() + 3
    while "resolver" not in sampler.disabled_targets and time.time() < deadline:
        await asyncio.sleep(0.02)
    await sampler.stop()

    assert "resolver" in sampler.disabled_targets
    assert "may filter TCP" in sampler.disabled_targets["resolver"]
    snapshot = {t["name"]: t for t in sampler.targets_snapshot()}
    assert snapshot["resolver"]["disabled"] is True
    # The DNS target is still healthy, so the verdict was not skewed.
    assert sampler.last_round["verdict"]["internet_ok"] is True
    # A stood-down guess is not a connectivity fault, so its failures must not
    # keep inflating the error count.
    assert sampler.counts["errors"] == 0


async def test_probe_exception_is_captured_not_raised(tmp_path, store, monkeypatch):
    async def exploding(settings, targets, round_id=None):
        raise RuntimeError("network stack on fire")

    monkeypatch.setattr(sampler_module, "measure_round", exploding)
    monkeypatch.setattr(sampler_module, "build_targets", lambda settings: list(TEST_TARGETS))
    sampler = make_sampler(tmp_path, store)
    await sampler.run_once("latency")
    assert sampler.counts["errors"] >= 1
    assert "network stack on fire" in (sampler.last_error or "")


async def test_maintenance_prunes_and_checkpoints_on_a_timer(tmp_path, store, patched, monkeypatch):
    """Retention has to apply during a long run, not only at startup."""
    monkeypatch.setattr(sampler_module, "MAINTENANCE_INTERVAL", 0.0)
    sampler = make_sampler(tmp_path, store, retention_days=1)
    calls: list[str] = []
    real_checkpoint = store.checkpoint

    async def counted_checkpoint(mode="PASSIVE"):
        calls.append(mode)
        await real_checkpoint(mode)

    monkeypatch.setattr(store, "checkpoint", counted_checkpoint)

    # One sample old enough to fall outside the retention window.
    await store.add({
        "ts": time.time() - 5 * 86400, "kind": "latency", "target": "cloudflare",
        "role": "internet", "round_id": 1, "ok": True, "probe_ms": 10.0,
    })
    await sampler.run_once("latency")
    await sampler._maintain()

    assert calls, "checkpoint was never called"
    assert sampler.maintenance is not None
    assert sampler.maintenance["pruned_rows"] == 1
    # The five-day-old sample is gone; the round just probed is not.
    remaining = await store.recent("latency", limit=100)
    assert remaining, "the fresh round was pruned by mistake"
    assert all(row["ts"] > time.time() - 3600 for row in remaining)


async def test_maintenance_is_rate_limited(tmp_path, store, patched, monkeypatch):
    monkeypatch.setattr(sampler_module, "MAINTENANCE_INTERVAL", 3600.0)
    sampler = make_sampler(tmp_path, store)
    calls: list[int] = []

    async def counted(mode="PASSIVE"):
        calls.append(1)

    monkeypatch.setattr(store, "checkpoint", counted)
    await sampler._maintain()
    await sampler._maintain()
    await sampler._maintain()
    # The first call runs (last_maintenance starts at 0), the rest are inside
    # the interval and must be skipped.
    assert len(calls) == 1


async def test_on_demand_speed_test_works_when_the_schedule_is_off(tmp_path, store, patched):
    """Setting the speed interval to 0 (the gaming recommendation) must not
    break the "Speed test" button."""
    sampler = make_sampler(tmp_path, store, quick_interval=0.0, sustained_interval=0.0)
    await sampler.start()
    await asyncio.sleep(0.1)
    assert sampler.counts["speed"] == 0

    sampler.request_run("speed")
    deadline = time.time() + 3
    while sampler.counts["speed"] == 0 and time.time() < deadline:
        await asyncio.sleep(0.02)

    assert sampler.counts["speed"] == 1, "the on-demand test never ran"
    assert sampler.last_speed is not None
    assert await store.count("speed") == 1
    # A one-off must not schedule a repeat.
    assert sampler.snapshot()["next_sustained_in"] is None
    await sampler.stop()


async def test_speed_test_is_stored_and_accounted(tmp_path, store, patched):
    sampler = make_sampler(tmp_path, store)
    await sampler.run_once("speed")
    assert sampler.counts["speed"] == 1
    assert sampler.bytes_used == 15 * 1024 * 1024
    assert sampler.last_speed["download_mbps"] == 100.0
    assert await store.count("speed") == 1


async def test_rate_limited_speed_test_is_not_counted_as_an_error(tmp_path, store, monkeypatch):
    async def throttled(settings, round_id=None, tier="sustained", trigger="scheduled",
                        progress=None):
        return {
            "ts": time.time(), "kind": "speed", "tier": tier, "trigger": trigger,
            "target": "speedtest", "role": "internet",
            "ok": False, "throttled": True, "error": "down: rate limited (HTTP 429)",
            "download_mbps": 0.0, "download_bytes": 0, "upload_mbps": 50.0, "upload_bytes": 2 * 1024 * 1024,
        }

    monkeypatch.setattr(sampler_module, "measure_speed", throttled)
    monkeypatch.setattr(sampler_module, "build_targets", lambda settings: list(TEST_TARGETS))
    sampler = make_sampler(tmp_path, store)

    await sampler.run_once("speed")
    assert sampler.counts["throttled"] == 1
    assert sampler.counts["errors"] == 0        # not a connectivity fault
    assert sampler.last_error is None
    assert sampler._speed_backoff == 2          # and it backs off

    await sampler.run_once("speed")
    assert sampler._speed_backoff == 4
    await sampler.run_once("speed")
    assert sampler._speed_backoff == 8
    await sampler.run_once("speed")
    assert sampler._speed_backoff == 8          # capped


async def test_speed_backoff_resets_on_success(tmp_path, store, monkeypatch):
    responses = [
        {"throttled": True, "ok": False},
        {"throttled": False, "ok": True},
    ]

    async def scripted(settings, round_id=None, tier="sustained", trigger="scheduled",
                       progress=None):
        state = responses.pop(0)
        return {
            "ts": time.time(), "kind": "speed", "target": "speedtest", "role": "internet",
            "ok": state["ok"], "throttled": state["throttled"],
            "download_mbps": 100.0 if state["ok"] else 0.0, "upload_mbps": 50.0,
        }

    monkeypatch.setattr(sampler_module, "measure_speed", scripted)
    monkeypatch.setattr(sampler_module, "build_targets", lambda settings: list(TEST_TARGETS))
    sampler = make_sampler(tmp_path, store)
    await sampler.run_once("speed")
    assert sampler._speed_backoff == 2
    await sampler.run_once("speed")
    assert sampler._speed_backoff == 1


async def test_run_now_triggers_an_extra_round(tmp_path, store, patched):
    sampler = make_sampler(tmp_path, store, latency_interval=30)
    await sampler.start()
    await asyncio.sleep(0.1)
    before = sampler.counts["rounds"]
    sampler.request_run("latency")
    deadline = time.time() + 2
    while sampler.counts["rounds"] <= before and time.time() < deadline:
        await asyncio.sleep(0.02)
    await sampler.stop()
    assert sampler.counts["rounds"] > before


async def test_update_settings_applies_live(tmp_path, store, patched):
    sampler = make_sampler(tmp_path, store, latency_interval=30)
    await sampler.start()
    applied = sampler.update_settings(
        {"latency_interval": 1.5, "sustained_interval": 0, "nonsense": 5}
    )
    assert applied == {"latency_interval": 1.5, "sustained_interval": 0.0}
    assert sampler.settings.latency_interval == 1.5
    # A shorter interval must pull the next probe forward.
    assert sampler.snapshot()["next_latency_in"] <= 1.5
    await sampler.stop()


async def test_update_settings_clamps_and_validates(tmp_path, store, patched):
    sampler = make_sampler(tmp_path, store)
    applied = sampler.update_settings({
        "latency_interval": 0.001,     # below the floor
        "streams": 999,                # above the ceiling
        "download_bytes": "not a number",
        "enable_upload": False,
        "public_targets": "9.9.9.9:443",
    })
    assert applied["latency_interval"] == 0.2
    assert applied["streams"] == 16
    assert "download_bytes" not in applied
    assert applied["enable_upload"] is False
    assert applied["public_targets"] == "9.9.9.9:443"
    assert sampler.settings.enable_upload is False


async def test_changing_targets_gives_stood_down_targets_another_chance(tmp_path, store, patched):
    sampler = make_sampler(tmp_path, store, target_probation_rounds=1)
    sampler.refresh_targets()
    sampler.disabled_targets["gateway"] = "was filtered"
    sampler._target_fail["gateway"] = 4

    # A cadence change must not resurrect it, even though the dashboard posts
    # every target field alongside it.
    sampler.update_settings({"latency_interval": 3, "probe_gateway": True})
    assert "gateway" in sampler.disabled_targets

    # ...but actually reconfiguring a target does give it a fresh chance.
    applied = sampler.update_settings({"probe_gateway": False})
    assert applied["probe_gateway"] is False
    assert sampler.disabled_targets == {}
    assert sampler._target_fail == {} or set(sampler._target_fail.values()) == {0}


async def test_subscribers_receive_round_and_outage_events(tmp_path, store, patched):
    sampler = make_sampler(tmp_path, store)
    queue = sampler.subscribe()

    await sampler.run_once("latency")
    events = [queue.get_nowait() for _ in range(queue.qsize())]
    assert any(e["type"] == "round" for e in events)
    assert any(e["type"] == "probing" for e in events)

    patched["internet_ok"] = False
    await sampler.run_once("latency")
    events = [queue.get_nowait() for _ in range(queue.qsize())]
    assert any(e["type"] == "outage_start" for e in events)

    patched["internet_ok"] = True
    await sampler.run_once("latency")
    events = [queue.get_nowait() for _ in range(queue.qsize())]
    assert any(e["type"] == "outage_end" for e in events)

    sampler.unsubscribe(queue)
    assert sampler.snapshot()["subscribers"] == 0


async def test_snapshot_reports_schedule_and_targets(tmp_path, store, patched):
    sampler = make_sampler(tmp_path, store, latency_interval=2, sustained_interval=300)
    await sampler.start()
    await asyncio.sleep(0.1)
    snapshot = sampler.snapshot()
    assert snapshot["running"] is True
    assert snapshot["latency_interval"] == 2
    assert snapshot["sustained_interval"] == 300
    assert snapshot["duration_s"] == 0.0
    assert snapshot["remaining_s"] is None
    assert {t["name"] for t in snapshot["targets"]} == {
        "resolver", "gateway", "cloudflare", "google", "dns",
    }
    assert snapshot["last_round"]["verdict"]["internet_ok"] is True
    assert set(snapshot["incidents"]) == {"internet", "dns"}
    await sampler.stop()
    assert sampler.snapshot()["running"] is False


async def test_stop_closes_a_dangling_incident(tmp_path, store, patched):
    """Stopping mid-outage must not leave an incident open forever."""
    sampler = make_sampler(tmp_path, store)
    patched["internet_ok"] = False
    await sampler.start()
    deadline = time.time() + 3
    while not await store.current_incidents() and time.time() < deadline:
        await asyncio.sleep(0.02)
    assert await store.current_incidents()
    await sampler.stop()
    assert await store.current_incidents() == []
    assert (await store.incidents(0.0))[0]["interrupted"] is True


async def test_both_tiers_are_scheduled_at_start(tmp_path, store, patched):
    """A tier with an interval must be scheduled, or it never runs at all."""
    sampler = make_sampler(tmp_path, store, quick_interval=5.0, sustained_interval=30.0)
    await sampler.start()
    await asyncio.sleep(0.2)
    snapshot = sampler.snapshot()
    # Cheap burst fires immediately so the dashboard has a number...
    assert snapshot["counts"]["speed"] >= 1
    assert sampler.last_quick is not None
    # ...the costly sustained test waits a full interval instead of firing on
    # every restart, but it must be scheduled.
    assert snapshot["next_sustained_in"] is not None
    assert snapshot["next_sustained_in"] > 25
    await sampler.stop()


async def test_a_disabled_tier_is_never_scheduled(tmp_path, store, patched):
    sampler = make_sampler(tmp_path, store, quick_interval=0.0, sustained_interval=0.0)
    await sampler.start()
    await asyncio.sleep(0.2)
    snapshot = sampler.snapshot()
    assert snapshot["counts"]["speed"] == 0
    assert snapshot["next_quick_in"] is None
    assert snapshot["next_sustained_in"] is None
    await sampler.stop()


async def test_sustained_test_runs_on_demand_when_disabled(tmp_path, store, patched):
    sampler = make_sampler(tmp_path, store, quick_interval=0.0, sustained_interval=0.0)
    await sampler.start()
    await asyncio.sleep(0.1)
    sampler.request_run("speed")
    deadline = time.time() + 3
    while sampler.last_sustained is None and time.time() < deadline:
        await asyncio.sleep(0.02)
    assert sampler.last_sustained is not None
    assert sampler.last_sustained["tier"] == "sustained"
    assert sampler.snapshot()["next_sustained_in"] is None
    await sampler.stop()


async def test_probing_speeds_up_during_an_outage_and_back_after(tmp_path, store, patched, monkeypatch):
    """The whole point: normal cadence to detect, fast cadence to time it."""
    monkeypatch.setattr(sampler_module, "MAINTENANCE_INTERVAL", 3600.0)
    sampler = make_sampler(
        tmp_path, store,
        latency_interval=30.0, fast_interval=0.05,
        fast_hold_seconds=0.15, fast_max_seconds=30.0,
    )
    await sampler.start()
    await asyncio.sleep(0.1)
    assert sampler.fast_mode is False
    assert sampler.latency_interval_now == 30.0

    patched["internet_ok"] = False
    sampler.request_run("latency")
    deadline = time.time() + 3
    while not sampler.fast_mode and time.time() < deadline:
        await asyncio.sleep(0.01)
    assert sampler.fast_mode is True, "an outage should switch to the fast cadence"
    assert sampler.latency_interval_now == 0.05
    fast = sampler.snapshot()["fast"]
    assert fast["active"] is True and fast["interval"] == 0.05

    # It keeps probing fast while the link is down.
    before = sampler.counts["rounds"]
    await asyncio.sleep(0.3)
    assert sampler.counts["rounds"] - before >= 2

    # Recovery holds the fast cadence briefly, then reverts.
    patched["internet_ok"] = True
    deadline = time.time() + 3
    while sampler.fast_mode and time.time() < deadline:
        await asyncio.sleep(0.02)
    assert sampler.fast_mode is False, "should return to the normal cadence"
    assert sampler.latency_interval_now == 30.0
    assert sampler.snapshot()["fast"]["reason"] == "recovered"
    await sampler.stop()


async def test_fast_mode_is_capped_so_a_long_outage_cannot_flood(tmp_path, store, patched, monkeypatch):
    monkeypatch.setattr(sampler_module, "MAINTENANCE_INTERVAL", 3600.0)
    sampler = make_sampler(
        tmp_path, store,
        latency_interval=30.0, fast_interval=0.05,
        fast_hold_seconds=10.0, fast_max_seconds=0.15,
    )
    patched["internet_ok"] = False
    await sampler.start()
    deadline = time.time() + 3
    while sampler.fast_mode and time.time() < deadline:
        await asyncio.sleep(0.02)
        if sampler.fast_mode is False:
            break
    # Either it never went fast (already capped) or it left with the cap reason.
    assert sampler.fast_mode is False
    await sampler.stop()


async def test_adaptive_probing_can_be_disabled(tmp_path, store, patched):
    sampler = make_sampler(tmp_path, store, latency_interval=0.05, fast_interval=0.0)
    patched["internet_ok"] = False
    await sampler.start()
    await asyncio.sleep(0.2)
    assert sampler.fast_mode is False
    assert sampler.latency_interval_now == 0.05
    await sampler.stop()


async def test_throughput_tests_are_deferred_while_resolving_an_outage(tmp_path, store, patched):
    """A speed test would block the fine-grained probing and fail anyway."""
    sampler = make_sampler(
        tmp_path, store,
        latency_interval=0.05, fast_interval=0.05,
        # Fast mode holds briefly after recovery, so keep the hold short here.
        fast_hold_seconds=0.2, quick_interval=0.2, sustained_interval=0.0,
    )
    await sampler.start()
    # The burst that fires at startup is expected; let it finish first.
    deadline = time.time() + 3
    while sampler.counts["speed"] == 0 and time.time() < deadline:
        await asyncio.sleep(0.02)
    ran = sampler.counts["speed"]

    patched["internet_ok"] = False
    deadline = time.time() + 3
    while not sampler.fast_mode and time.time() < deadline:
        await asyncio.sleep(0.01)
    assert sampler.fast_mode is True

    # Several quick intervals pass without a throughput test being started.
    await asyncio.sleep(0.5)
    assert sampler.counts["speed"] == ran, "no throughput test should run during an outage"

    # Once recovered (and the hold elapsed), the deferred test gets its turn.
    patched["internet_ok"] = True
    deadline = time.time() + 6
    while sampler.counts["speed"] == ran and time.time() < deadline:
        await asyncio.sleep(0.02)
    assert sampler.counts["speed"] > ran, "the deferred test never ran after recovery"
    await sampler.stop()


async def test_a_run_now_pressed_during_an_outage_does_not_relabel_a_timed_run(
    tmp_path, store, patched
):
    """The dashboard must not open a "started by you" modal for a timed test.

    "Run now" during an outage is postponed by a whole interval, so keeping the
    manual mark queued for an hour means the next run *the clock* starts inherits
    it and is reported as hand-started.
    """
    sampler = make_sampler(
        tmp_path, store,
        latency_interval=0.05, fast_interval=0.05,
        fast_hold_seconds=0.1, quick_interval=0.0, sustained_interval=0.4,
    )
    await sampler.start()
    deadline = time.time() + 3
    while sampler.counts["speed"] == 0 and time.time() < deadline:
        await asyncio.sleep(0.02)
    ran = sampler.counts["speed"]
    assert sampler.last_sustained["trigger"] == "scheduled"

    patched["internet_ok"] = False
    deadline = time.time() + 3
    while not sampler.fast_mode and time.time() < deadline:
        await asyncio.sleep(0.01)
    assert sampler.fast_mode is True

    sampler.request_run("speed")            # "Run now", pressed mid-outage
    await asyncio.sleep(0.3)
    assert sampler.counts["speed"] == ran, "a test must not run while the link is down"

    patched["internet_ok"] = True
    deadline = time.time() + 6
    while sampler.counts["speed"] == ran and time.time() < deadline:
        await asyncio.sleep(0.02)
    assert sampler.counts["speed"] > ran, "the postponed test never ran after recovery"
    assert sampler.last_sustained["trigger"] == "scheduled", (
        "the postponed click relabelled a run the clock started"
    )
    await sampler.stop()


async def test_incident_records_how_precisely_the_start_is_known(tmp_path, store, patched):
    sampler = make_sampler(tmp_path, store, latency_interval=0.05, fast_interval=0.05)
    await sampler.start()
    await asyncio.sleep(0.15)          # healthy rounds establish a last-good time

    patched["internet_ok"] = False
    deadline = time.time() + 3
    while not await store.current_incidents() and time.time() < deadline:
        await asyncio.sleep(0.02)
    ongoing = (await store.current_incidents())[0]
    # The drop began somewhere between the last good probe and the first bad one.
    assert ongoing["start_uncertainty_s"] is not None
    assert 0 <= ongoing["start_uncertainty_s"] <= 1.0
    # While it is still down there is no end to bound yet.
    assert ongoing["end_uncertainty_s"] is None

    patched["internet_ok"] = True
    deadline = time.time() + 4
    while await store.current_incidents() and time.time() < deadline:
        await asyncio.sleep(0.02)
    await sampler.stop()

    row = (await store.incidents(0.0))[0]
    assert row["start_uncertainty_s"] is not None
    assert row["end_uncertainty_s"] is not None
    # With adaptive probing the end is bounded within a probe interval or so.
    assert 0 <= row["end_uncertainty_s"] <= 1.0


async def test_a_failing_round_breaks_the_recovery_streak(tmp_path, store, patched, monkeypatch):
    """A flapping link must not drop out of fast mode mid-flap.

    Successes have to be *consecutive*; otherwise a link that recovers briefly
    between failures accumulates the hold time across the gaps.
    """
    monkeypatch.setattr(sampler_module, "MAINTENANCE_INTERVAL", 3600.0)
    sampler = make_sampler(
        tmp_path, store,
        latency_interval=30.0, fast_interval=0.05,
        fast_hold_seconds=0.3, fast_max_seconds=60.0,
    )
    patched["internet_ok"] = False
    await sampler.start()
    deadline = time.time() + 3
    while not sampler.fast_mode and time.time() < deadline:
        await asyncio.sleep(0.01)
    assert sampler.fast_mode is True

    # Brief success, then failure again, repeatedly: the streak keeps restarting,
    # so the hold never completes and we stay in fast mode.
    for _ in range(4):
        patched["internet_ok"] = True
        await asyncio.sleep(0.1)          # less than the 0.3s hold
        patched["internet_ok"] = False
        await asyncio.sleep(0.1)
        assert sampler.fast_mode is True, "flapping dropped us out of fast mode"
    await sampler.stop()


async def test_fast_mode_probes_once_with_a_short_timeout(tmp_path, store, patched, monkeypatch):
    """The round must fit inside the fast cadence, or 1s is a fiction."""
    monkeypatch.setattr(sampler_module, "MAINTENANCE_INTERVAL", 3600.0)
    sampler = make_sampler(
        tmp_path, store,
        latency_interval=30.0, fast_interval=1.0, fast_timeout=0.5,
        ping_count=2, target_timeout=3.0,
    )
    plain = sampler._probe_settings()
    assert plain.ping_count == 2 and plain.target_timeout == 3.0

    patched["internet_ok"] = False
    await sampler.start()
    deadline = time.time() + 3
    while not sampler.fast_mode and time.time() < deadline:
        await asyncio.sleep(0.01)
    fast = sampler._probe_settings()
    assert fast.ping_count == 1        # one attempt is enough to confirm
    assert fast.target_timeout == 0.5  # short, so the round fits the cadence
    await sampler.stop()


async def test_applying_an_unrelated_setting_keeps_target_probation(tmp_path, store, patched):
    """Regression: the dashboard posts every field on every Apply.

    Keying the probation reset off the *posted* keys meant changing the probe
    interval re-probed the gateway that is already known not to answer, and its
    timeouts showed up as the connection's last error.
    """
    sampler = make_sampler(tmp_path, store, latency_interval=2.0, probe_gateway=True)
    sampler.refresh_targets()
    sampler.disabled_targets["gateway"] = "no response in 3 rounds - may filter TCP"
    sampler._target_fail["gateway"] = 3

    # What the dashboard sends when only the cadence was touched.
    full_payload = {
        "latency_interval": 5,
        "quick_interval": sampler.settings.quick_interval,
        "sustained_interval": sampler.settings.sustained_interval,
        "public_targets": sampler.settings.public_targets,
        "extra_targets": sampler.settings.extra_targets,
        "dns_probe_name": sampler.settings.dns_probe_name,
        "probe_gateway": sampler.settings.probe_gateway,
        "probe_resolver": sampler.settings.probe_resolver,
    }
    applied = sampler.update_settings(full_payload)
    assert applied["latency_interval"] == 5.0
    assert "gateway" in sampler.disabled_targets, "an unrelated Apply re-armed a dead target"

    # Actually changing a target field does give it a fresh chance.
    sampler.update_settings({"extra_targets": "router=10.0.0.1:80"})
    assert sampler.disabled_targets == {}


async def test_a_target_that_never_answered_is_not_a_connection_error(
    tmp_path, store, patched, monkeypatch
):
    """A guess that never speaks TCP is a config fact, not an error."""
    async def only_gateway_fails(settings, targets, round_id=None):
        ts = time.time()
        return [
            {
                "ts": ts, "kind": "latency", "target": t.name, "role": t.role,
                "round_id": round_id or int(ts * 1000),
                "ok": t.name != "gateway",
                "probe_ms": None if t.name == "gateway" else 9.0,
                "error": "timeout on 53; timeout on 80; timeout on 443"
                if t.name == "gateway" else None,
                "sent": 2, "recv": 0 if t.name == "gateway" else 2,
                "loss_pct": 100.0 if t.name == "gateway" else 0.0,
            }
            for t in targets
        ]

    monkeypatch.setattr(sampler_module, "measure_round", only_gateway_fails)
    sampler = make_sampler(tmp_path, store, target_probation_rounds=99)
    # A guessed local target that will never answer, alongside healthy ones.
    sampler.targets = [
        Target("gateway", "local", "gateway", host="192.0.2.1", ports=(53, 80, 443),
               guessed=True),
        Target("cloudflare", "internet", "cloudflare", host="127.0.0.1", ports=(9,)),
    ]

    await sampler.run_once("latency")
    # The gateway is still within probation, so it is in the round...
    failing = [s for s in sampler.last_round["samples"] if not s["ok"]]
    assert [s["target"] for s in failing] == ["gateway"]
    # ...but it must not be reported as the connection's error.
    assert sampler.last_error is None
    assert sampler.counts["errors"] == 0


async def test_a_manual_run_is_recorded_as_manual(tmp_path, store, patched):
    """The view has to tell a test you asked for from one the clock started."""
    sampler = make_sampler(tmp_path, store, quick_interval=0.0, sustained_interval=0.0)
    await sampler.start()
    await asyncio.sleep(0.1)

    sampler.request_run("speed")
    deadline = time.time() + 3
    while sampler.last_sustained is None and time.time() < deadline:
        await asyncio.sleep(0.02)
    assert sampler.last_sustained["trigger"] == "manual"
    assert sampler.last_sustained["tier"] == "sustained"
    await sampler.stop()


async def test_a_scheduled_run_is_recorded_as_scheduled(tmp_path, store, patched):
    sampler = make_sampler(tmp_path, store, quick_interval=0.0, sustained_interval=0.0)
    await sampler.run_once("speed")
    assert sampler.last_sustained["trigger"] == "scheduled"


async def test_a_fresh_start_warms_up_instead_of_waiting_a_whole_interval(
    tmp_path, store, patched
):
    """A wiped database must not leave the per-second panel empty for an hour."""
    sampler = make_sampler(tmp_path, store, quick_interval=600.0, sustained_interval=3600.0)
    await sampler.start()
    await asyncio.sleep(0.2)
    snapshot = sampler.snapshot()
    # Nothing stored yet, so the first sustained test is minutes away at worst
    # rather than a full interval.
    assert snapshot["next_sustained_in"] is not None
    assert snapshot["next_sustained_in"] <= 61
    await sampler.stop()


async def test_a_recent_test_is_not_repeated_on_restart(tmp_path, store, patched):
    """Restarting must not spend hundreds of megabytes again."""
    await store.add({
        "ts": time.time() - 60, "kind": "speed", "tier": "sustained",
        "trigger": "scheduled", "target": "speedtest", "role": "internet",
        "ok": True, "download_mbps": 100.0, "upload_mbps": 50.0,
    })
    sampler = make_sampler(tmp_path, store, quick_interval=600.0, sustained_interval=3600.0)
    await sampler.start()
    await asyncio.sleep(0.2)
    # One minute into a one hour interval, so ~59 minutes remain.
    assert sampler.snapshot()["next_sustained_in"] > 3000
    await sampler.stop()


async def test_a_restart_puts_the_last_stored_test_back_on_the_dashboard(
    tmp_path, store, patched
):
    """The per-second panel sat empty until the next test, an hour later.

    The samples are in the database, so a restart has no reason to show nothing.
    """
    await store.add({
        "ts": time.time() - 120, "kind": "speed", "tier": "sustained",
        "trigger": "scheduled", "target": "speedtest", "role": "internet",
        "ok": True, "download_mbps": 100.0, "upload_mbps": 50.0,
        "download_intervals": [{"t": 0, "mbps": 90.0}, {"t": 1, "mbps": 110.0}],
    })
    sampler = make_sampler(tmp_path, store, quick_interval=600.0, sustained_interval=3600.0)
    await sampler.start()
    await asyncio.sleep(0.1)
    restored = sampler.snapshot()["last_sustained"]
    assert restored is not None, "a restart forgot the last test it had stored"
    assert restored["download_mbps"] == 100.0
    # Decoded for the chart, not left as the JSON text the column holds.
    assert restored["download_intervals"] == [{"t": 0, "mbps": 90.0}, {"t": 1, "mbps": 110.0}]
    await sampler.stop()


async def test_fast_mode_also_engages_when_only_dns_is_failing(tmp_path, store, patched, monkeypatch):
    """A resolver that stops answering looks like the internet dying.

    In practice this is common: raw IP connectivity stays up while the resolver
    hangs for a few seconds, and a browser or a game cannot tell the difference.
    """
    monkeypatch.setattr(sampler_module, "MAINTENANCE_INTERVAL", 3600.0)
    sampler = make_sampler(
        tmp_path, store,
        latency_interval=30.0, fast_interval=0.05,
        fast_hold_seconds=0.15, fast_max_seconds=30.0,
    )
    patched["dns_ok"] = False          # internet stays fine
    await sampler.start()
    deadline = time.time() + 3
    while not sampler.fast_mode and time.time() < deadline:
        await asyncio.sleep(0.01)
    assert sampler.fast_mode is True, "a DNS-only failure should speed probing up"
    assert sampler.snapshot()["fast"]["reason"] == "dns failing"

    patched["dns_ok"] = True
    deadline = time.time() + 3
    while sampler.fast_mode and time.time() < deadline:
        await asyncio.sleep(0.02)
    assert sampler.fast_mode is False
    await sampler.stop()


async def test_a_dns_incident_records_its_own_boundary_precision(tmp_path, store, patched):
    sampler = make_sampler(tmp_path, store, latency_interval=0.05, fast_interval=0.05)
    await sampler.start()
    await asyncio.sleep(0.15)          # healthy rounds first
    patched["dns_ok"] = False
    deadline = time.time() + 3
    while not await store.current_incidents() and time.time() < deadline:
        await asyncio.sleep(0.02)
    await sampler.stop()

    rows = await store.incidents(0.0)
    dns_rows = [row for row in rows if row["kind"] == "dns"]
    assert dns_rows, "the DNS blip should have been recorded"
    assert dns_rows[0]["start_uncertainty_s"] is not None
    assert 0 <= dns_rows[0]["start_uncertainty_s"] <= 1.0


async def test_healthy_rounds_do_not_leave_fast_mode_when_dns_is_down(tmp_path, store, patched, monkeypatch):
    """Recovery needs *everything* healthy, not just raw connectivity."""
    monkeypatch.setattr(sampler_module, "MAINTENANCE_INTERVAL", 3600.0)
    sampler = make_sampler(
        tmp_path, store,
        latency_interval=0.05, fast_interval=0.05,
        fast_hold_seconds=0.2, fast_max_seconds=60.0,
    )
    patched["dns_ok"] = False
    await sampler.start()
    deadline = time.time() + 3
    while not sampler.fast_mode and time.time() < deadline:
        await asyncio.sleep(0.01)
    # Internet stays up the whole time, DNS never recovers.
    await asyncio.sleep(0.5)
    assert sampler.fast_mode is True, "healthy internet alone should not end the hold"
    await sampler.stop()


async def test_progress_is_reported_while_a_test_runs(tmp_path, store, patched, monkeypatch):
    """The dashboard has to be able to show this happening, not just the result."""
    seen: list[dict] = []

    async def slow_speed(settings, round_id=None, tier="sustained", trigger="scheduled",
                         progress=None):
        # Stand in for the real transfer: report a couple of frames, then finish.
        for step in range(3):
            if progress:
                progress({
                    "phase": "download", "elapsed_s": float(step), "expected_s": 10.0,
                    "bytes": step * 50_000_000, "mbps": 400.0 + step,
                    "intervals": [{"t": step, "mbps": 400.0 + step}],
                    "tier": tier, "trigger": trigger, "phases": ["download", "upload"],
                    "results": {},
                })
            seen.append(dict(sampler.speed_progress or {}))
            await asyncio.sleep(0.02)
        return {
            "ts": time.time(), "kind": "speed", "tier": tier, "trigger": trigger,
            "target": "speedtest", "role": "internet", "ok": True,
            "download_mbps": 402.0, "upload_mbps": 150.0,
        }

    monkeypatch.setattr(sampler_module, "measure_speed", slow_speed)
    monkeypatch.setattr(sampler_module, "build_targets", lambda settings: list(TEST_TARGETS))
    sampler = make_sampler(tmp_path, store)

    queue = sampler.subscribe()
    await sampler.run_once("speed")

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    progress_events = [e for e in events if e["type"] == "speed_progress"]
    assert progress_events, "no live progress was published"
    assert progress_events[0]["phase"] == "download"
    assert progress_events[0]["expected_s"] == 10.0
    assert progress_events[-1]["mbps"] > progress_events[0]["mbps"]
    # A page loading mid-test can read the same thing from /api/status, and it is
    # cleared once the test is over.
    assert all(frame.get("running") for frame in seen)
    assert sampler.snapshot()["speed_progress"] is None


# ------------------------------------------------------ corroboration hosts
CHECK_TARGETS = [
    Target("cloudflare", "internet", "cf", host="127.0.0.1", ports=(9,)),
    Target("google", "internet", "gg", host="127.0.0.1", ports=(9,)),
    Target("github", "internet", "gh", host="127.0.0.1", ports=(9,), fact_check=True),
    Target("wikipedia", "internet", "wk", host="127.0.0.1", ports=(9,), fact_check=True),
]


def make_check_sampler(tmp_path, store, patched, monkeypatch, **overrides):
    monkeypatch.setattr(
        sampler_module, "build_targets", lambda settings: list(CHECK_TARGETS)
    )
    return make_sampler(tmp_path, store, **overrides)


async def test_corroboration_hosts_are_not_probed_when_all_is_well(
    tmp_path, store, patched, monkeypatch
):
    """Ten thousand extra connects a day would buy nothing."""
    sampler = make_check_sampler(
        tmp_path, store, patched, monkeypatch,
        fact_check_interval=0.0,        # no baseline pass either
    )
    await sampler.run_once("latency")
    probed = {s["target"] for s in sampler.last_round["samples"]}
    assert probed == {"cloudflare", "google"}


async def test_a_drop_is_confirmed_against_other_networks(
    tmp_path, store, patched, monkeypatch
):
    """The point of the feature: the incident carries five networks' worth of no."""
    sampler = make_check_sampler(
        tmp_path, store, patched, monkeypatch, fact_check_interval=0.0
    )
    patched["internet_ok"] = False
    patched["fact_check_ok"] = False          # the other networks are dark too
    await sampler.run_once("latency")

    probed = [s["target"] for s in sampler.last_round["samples"]]
    assert "github" in probed and "wikipedia" in probed, "the drop was not corroborated"
    assert set(sampler.last_round["verdict"]["failed"]) == {
        "cloudflare", "google", "github", "wikipedia",
    }
    rows = await store.incidents(0.0)
    assert rows[0]["failed_targets"].count(",") == 3, "every silent host is part of the evidence"


async def test_a_healthy_other_network_cancels_the_alarm(
    tmp_path, store, patched, monkeypatch
):
    """If five other networks answer, the link is up -- whatever our two endpoints say.

    This is the difference between "1.1.1.1 is unreachable from here" and "the
    internet is down", and it is worth not waking the user up for.
    """
    sampler = make_check_sampler(
        tmp_path, store, patched, monkeypatch, fact_check_interval=0.0
    )
    patched["internet_ok"] = False        # the two primary targets time out
    patched["fact_check_ok"] = True       # but GitHub, Wikipedia, ... answer
    await sampler.run_once("latency")

    assert sampler.last_round["verdict"]["internet_ok"] is True, (
        "a provider-specific failure was reported as an outage"
    )
    assert sampler.last_round["scope"] == ""
    assert await store.incidents(0.0) == []


async def test_the_baseline_pass_runs_on_its_own_timer(
    tmp_path, store, patched, monkeypatch
):
    """So we know which corroboration hosts are reachable here at all."""
    sampler = make_check_sampler(
        tmp_path, store, patched, monkeypatch, fact_check_interval=0.2
    )
    await sampler.run_once("latency")
    assert {s["target"] for s in sampler.last_round["samples"]} == {"cloudflare", "google"}
    await asyncio.sleep(0.25)
    await sampler.run_once("latency")
    probed = {s["target"] for s in sampler.last_round["samples"]}
    assert {"github", "wikipedia"} <= probed, "the baseline pass never ran"


async def test_corroboration_can_be_switched_off(tmp_path, store, patched, monkeypatch):
    monkeypatch.setattr(
        sampler_module, "build_targets",
        lambda settings: [t for t in CHECK_TARGETS if not t.fact_check],
    )
    sampler = make_sampler(tmp_path, store, fact_check_interval=0.0)
    patched["internet_ok"] = False
    await sampler.run_once("latency")
    assert {s["target"] for s in sampler.last_round["samples"]} == {"cloudflare", "google"}


async def test_a_failed_round_fires_a_counted_burst(tmp_path, store, patched, monkeypatch):
    """The loss figure has to be measured at the drop, not inferred from counts.

    A round probe can only report 0% or 100%; the burst says how much of the
    traffic was getting through. It is backgrounded like a trace, because on a
    dead path twenty handshakes take seconds.
    """
    calls: list[tuple[str, int]] = []

    async def fake_burst(name, host, port, *, count, window, timeout, max_seconds,
                         round_id, ts):
        calls.append((name, count))
        return {
            "ts": ts, "kind": "burst", "target": name, "role": "internet",
            "round_id": round_id, "ok": False, "error": "14/20 handshakes lost: timeout on 443",
            "probe_ms": None, "refused": 0, "loss_pct": 70.0, "sent": 20, "recv": 6,
            "tcp_min_ms": None, "tcp_avg_ms": None, "tcp_max_ms": None,
        }

    monkeypatch.setattr(sampler_module, "measure_burst", fake_burst)
    sampler = make_sampler(
        tmp_path, store, burst_host="1.1.1.1", burst_cooldown=0.0, fast_interval=0.0
    )
    await sampler.start()
    try:
        deadline = time.time() + 5
        while not calls and time.time() < deadline:
            await asyncio.sleep(0.02)
        assert not calls, "a healthy connection must not burst"
        patched["internet_ok"] = False
        deadline = time.time() + 5
        while not calls and time.time() < deadline:
            await asyncio.sleep(0.02)
    finally:
        await sampler.stop()

    assert calls and calls[0][1] == 20
    # The row is a burst, not a failed probe, so the probe health stays honest.
    rows = await store.bursts()
    assert len(rows) == 1 and rows[0]["loss_pct"] == 70.0
    assert await store.count("latency") == sampler.counts["rounds"] * 5, (
        "the burst row is not a round probe: the counts stay 5 per round"
    )
    assert sampler.last_burst and sampler.last_burst["loss_pct"] == 70.0


async def test_a_drop_the_pool_cancels_still_measures_its_loss(tmp_path, store, patched, monkeypatch):
    """The regression that made the burst useless on this connection.

    Measured here: in half an hour, 47 rounds had *both* primary endpoints
    black-holed ("No route to host") while wikipedia and youtube answered, so
    the round stayed "up", no incident opened and nothing was investigated. The
    loss at those moments is the whole complaint, so the burst fires on the
    primary endpoints failing -- not on the round being down.
    """
    calls: list[str] = []

    async def fake_burst(name, host, port, *, count, window, timeout, max_seconds,
                         round_id, ts):
        calls.append(name)
        return {
            "ts": ts, "kind": "burst", "target": name, "role": "internet",
            "round_id": round_id, "ok": False, "error": "18/20 handshakes lost: timeout on 443",
            "probe_ms": None, "refused": 0, "loss_pct": 90.0, "sent": 20, "recv": 2,
            "tcp_min_ms": None, "tcp_avg_ms": None, "tcp_max_ms": None,
        }

    monkeypatch.setattr(sampler_module, "measure_burst", fake_burst)
    # One host on another network, the way the real pool is configured.
    pool = Target("wikipedia-org", "internet", "wikipedia", host="127.0.0.1",
                  ports=(9,), fact_check=True)
    monkeypatch.setattr(sampler_module, "build_targets",
                        lambda settings: [*TEST_TARGETS, pool])
    sampler = make_sampler(
        tmp_path, store, burst_host="1.1.1.1", burst_cooldown=0.0, fast_interval=0.0
    )
    await sampler.start()
    try:
        # The primary endpoints fail; the pool answers, so internet_ok stays True.
        patched["failed_targets"] = {"cloudflare", "google"}
        deadline = time.time() + 5
        while not calls and time.time() < deadline:
            await asyncio.sleep(0.02)
        assert calls, "a cancelled drop still gets its loss measured"
        # And the verdict really is "up", which is what used to hide it.
        assert sampler.last_round["verdict"]["internet_ok"] is True
    finally:
        await sampler.stop()

    # The fixture's targets live on 127.0.0.1, so the burst falls back to the
    # configured host as its label; the name matching is tested separately.
    assert calls[0] == "1.1.1.1"
    assert (await store.bursts())[0]["loss_pct"] == 90.0


async def test_the_burst_row_carries_the_endpoints_own_name(tmp_path, store, monkeypatch):
    # A burst at the same endpoint as a round probe is filed under that name, so
    # the loss panel and the per-target table talk about the same thing.
    monkeypatch.setattr(sampler_module, "build_targets", lambda settings: [
        Target("cloudflare", "internet", "cloudflare", host="1.1.1.1", ports=(443,)),
        Target("google", "internet", "google", host="8.8.8.8", ports=(443,)),
    ])
    sampler = make_sampler(tmp_path, store, burst_host="1.1.1.1")
    sampler.refresh_targets()
    assert sampler._burst_endpoint() == ("cloudflare", "1.1.1.1", 443)

    sampler.settings.burst_host = "203.0.113.9"
    assert sampler._burst_endpoint() == ("203.0.113.9", "203.0.113.9", 443)
    await sampler.stop()


async def test_a_heavy_loss_burst_earns_a_trace(tmp_path, store, patched, monkeypatch):
    """The partial blackouts get a path too, not only a loss figure.

    They never open an incident, so nothing else would trace them -- and the
    path is broken at that moment, which is when a trace says the most.
    """
    traces: list[str] = []

    async def fake_burst(name, host, port, *, count, window, timeout, max_seconds,
                         round_id, ts):
        return {
            "ts": ts, "kind": "burst", "target": name, "role": "internet",
            "round_id": round_id, "ok": False, "error": "14/20 handshakes lost",
            "probe_ms": None, "refused": 0, "loss_pct": 70.0, "sent": 20, "recv": 6,
            "tcp_min_ms": None, "tcp_avg_ms": None, "tcp_max_ms": None,
        }

    async def fake_trace(host, **_kwargs):
        traces.append(host)
        return {
            "ts": time.time(), "host": host, "tracer": "fake", "reached": False,
            "hops": 4, "answered": 3, "max_hops": 20, "last_hop": "100.64.0.1",
            "duration_ms": 5.0, "error": None, "hop_list": [],
        }

    monkeypatch.setattr(sampler_module, "measure_burst", fake_burst)
    monkeypatch.setattr(sampler_module, "trace_path", fake_trace)
    # No incident (min_rounds high): this is about the blackout trace alone,
    # since an incident would trace by itself and hide which one fired.
    sampler = make_sampler(
        tmp_path, store, burst_host="1.1.1.1", burst_cooldown=0.0, fast_interval=0.0,
        trace_interval=0.0, trace_cooldown=0.0, incident_min_rounds=99,
    )
    await sampler.start()
    try:
        patched["failed_targets"] = {"cloudflare", "google"}
        deadline = time.time() + 5
        while not traces and time.time() < deadline:
            await asyncio.sleep(0.02)
    finally:
        await sampler.stop()

    assert traces == ["1.1.1.1"], "a 70% loss burst is worth a path"
    row = (await store.traces())[0]
    assert row["trigger"] == "drop" and row["last_hop"] == "100.64.0.1"


async def test_a_light_loss_burst_does_not_trace(tmp_path, store, patched, monkeypatch):
    """One handshake in twenty is noise; it must not spend a tracer run."""
    traces: list[str] = []

    async def light_burst(name, host, port, **kwargs):
        return {
            "ts": kwargs["ts"], "kind": "burst", "target": name, "role": "internet",
            "round_id": kwargs["round_id"], "ok": True, "error": None, "probe_ms": 9.0,
            "refused": 0, "loss_pct": 5.0, "sent": 20, "recv": 19,
            "tcp_min_ms": 9.0, "tcp_avg_ms": 9.0, "tcp_max_ms": 9.0,
        }

    async def fake_trace(host, **_kwargs):
        traces.append(host)
        raise AssertionError("a 5% burst must not trace")

    monkeypatch.setattr(sampler_module, "measure_burst", light_burst)
    monkeypatch.setattr(sampler_module, "trace_path", fake_trace)
    sampler = make_sampler(
        tmp_path, store, burst_host="1.1.1.1", burst_cooldown=0.0, fast_interval=0.0,
        trace_interval=0.0, incident_min_rounds=99,
    )
    await sampler.start()
    try:
        patched["failed_targets"] = {"cloudflare", "google"}
        deadline = time.time() + 2
        while await store.count_traces() == 0 and time.time() < deadline:
            await asyncio.sleep(0.05)
    finally:
        await sampler.stop()
    assert traces == [] and await store.count_traces() == 0
