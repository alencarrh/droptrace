"""Unit tests for the probe math and per-target probing (no internet needed)."""

from __future__ import annotations

import asyncio
import time
import socket
import struct
import threading

import pytest

from droptrace.config import Settings
from droptrace.probes import (
    probe_target,
    as_sample,
    build_dns_query,
    classify_outage,
    evaluate_round,
    fresh_probe_name,
    jitter,
    make_payload,
    mbps,
    measure_burst,
    measure_round,
    percentile,
    probe_dns,
    probe_tcp,
)
from droptrace.targets import Target


def tcp_target(host="127.0.0.1", port=9, role="internet", name="probe"):
    return Target(name=name, role=role, label=name, host=host, ports=(port,))


# ------------------------------------------------------------------- math
def test_percentile_interpolates():
    values = [1.0, 2.0, 3.0, 4.0]
    assert percentile(values, 0) == 1.0
    assert percentile(values, 100) == 4.0
    assert percentile(values, 50) == pytest.approx(2.5)
    assert percentile([5.0], 95) == 5.0
    assert percentile([], 95) == 0.0


def test_jitter_is_mean_absolute_consecutive_delta():
    assert jitter([10.0, 12.0, 9.0]) == pytest.approx((2 + 3) / 2)
    assert jitter([10.0]) == 0.0
    assert jitter([]) == 0.0


def test_mbps_conversion():
    assert mbps(1024 * 1024, 1.0) == pytest.approx(8.388608, rel=1e-6)
    assert mbps(1_000_000, 1.0) == pytest.approx(8.0)
    assert mbps(1000, 0) == 0.0
    assert mbps(0, 1.0) == 0.0


def test_make_payload_is_incompressible_and_exact():
    payload = make_payload(200_000)
    assert len(payload) == 200_000
    assert len(set(payload)) > 200
    assert len(make_payload(10)) == 1024


def test_as_sample_rounds_and_drops_non_finite():
    cleaned = as_sample({"a": 1.23456, "b": float("nan"), "c": float("inf"), "d": 2})
    assert cleaned["a"] == 1.235
    assert cleaned["b"] is None
    assert cleaned["c"] is None
    assert cleaned["d"] == 2


def test_merge_buckets_sums_workers_per_second():
    from droptrace.probes import _merge_buckets

    merged = _merge_buckets({0: {0: 1_000_000, 1: 2_000_000}, 1: {0: 1_000_000, 2: 500_000}}, elapsed=3.0)
    assert [point["t"] for point in merged] == [0, 1, 2]
    assert merged[0]["mbps"] == pytest.approx(16.0)   # 2 MB in second 0
    assert merged[1]["mbps"] == pytest.approx(16.0)
    assert merged[2]["mbps"] == pytest.approx(4.0)


def test_merge_buckets_drops_the_partial_final_second():
    """The test stops mid-second; that sliver must not be reported as a rate."""
    from droptrace.probes import _merge_buckets

    buckets = {0: {0: 40_000_000, 1: 40_000_000, 2: 100_000}}
    # 2.05s elapsed: bucket 2 holds 0.05s of traffic and would read as ~16 Mbps.
    dropped = _merge_buckets(buckets, elapsed=2.05)
    assert [p["t"] for p in dropped] == [0, 1]
    # A whole number of seconds keeps every bucket.
    kept = _merge_buckets(buckets, elapsed=3.0)
    assert [p["t"] for p in kept] == [0, 1, 2]


def test_decay_pct_flags_a_throttled_test():
    from droptrace.probes import _decay_pct

    steady = [{"t": i, "mbps": 100.0} for i in range(10)]
    assert _decay_pct(steady) == pytest.approx(0.0)

    # Full speed, then half: this is the pattern a burst test cannot see.
    throttled = [{"t": i, "mbps": v} for i, v in enumerate([20, 100, 100, 100, 100, 100, 50, 50, 50, 50])]
    decay = _decay_pct(throttled)
    assert decay is not None and decay < -30, decay

    # A test too short to tell throttling from jitter reports nothing.
    assert _decay_pct([{"t": i, "mbps": v} for i, v in enumerate([100, 100, 50, 50])]) is None

    rising = [{"t": i, "mbps": v} for i, v in enumerate([10, 20, 40, 60, 80, 100, 100])]
    assert _decay_pct(rising) == pytest.approx(0.0)

    # Too few samples to say anything.
    assert _decay_pct([{"t": 0, "mbps": 100.0}]) is None
    assert _decay_pct([]) is None


def test_decay_pct_does_not_cry_wolf_on_a_ramp():
    """A single TCP stream starts slowly; that is not throttling."""
    from droptrace.probes import _decay_pct

    ramp = [{"t": i, "mbps": v} for i, v in enumerate([5, 30, 90, 100, 100, 100, 100, 100])]
    assert _decay_pct(ramp) == pytest.approx(0.0)


# ------------------------------------------------------------ tcp probing
async def test_probe_tcp_timeout_is_a_failure():
    # 192.0.2.0/24 is TEST-NET-1: reserved, never routed.
    settings = Settings(ping_count=1, target_timeout=0.3, ping_gap=0.0)
    sample = await probe_tcp(tcp_target("192.0.2.1", 443), settings, round_id=1, ts=1000.0)
    assert sample["ok"] is False
    assert sample["probe_ms"] is None
    assert sample["recv"] == 0
    assert sample["loss_pct"] == 100.0
    assert sample["round_id"] == 1
    assert sample["role"] == "internet"
    # Which failure you get depends on the network and on the machine, not on
    # this code: a blackholed address times out, a router that answers with ICMP
    # "no route to host" (this connection does, mid-drop) fails just as fast,
    # and a loaded machine can starve the worker thread past the per-target
    # budget. All three are failures; what must never happen is a connect that
    # nobody answered counting as "ok".
    error = (sample["error"] or "").lower()
    assert any(word in error for word in ("timeout", "unreachable", "no route", "budget")), error


async def test_probe_tcp_counts_connection_refused_as_reachable():
    """A RST proves the host answered; routers filter most ports."""
    settings = Settings(ping_count=1, target_timeout=1.0, ping_gap=0.0)
    sample = await probe_tcp(tcp_target("127.0.0.1", 9), settings, round_id=2, ts=1000.0)
    assert sample["ok"] is True
    assert sample["refused"] == 1
    assert sample["probe_ms"] is not None
    assert sample["probe_ms"] >= 0
    assert sample["recv"] == 1


async def test_probe_tcp_falls_back_across_ports():
    """The gateway may only answer on one of several ports."""
    target = Target(
        name="gw", role="local", label="gw", host="127.0.0.1", ports=(1, 9, 65000), guessed=True
    )
    settings = Settings(ping_count=1, target_timeout=0.5, ping_gap=0.0)
    sample = await probe_tcp(target, settings, round_id=3, ts=1000.0)
    # Every port refuses, but the host is answering, so this is "reachable".
    assert sample["ok"] is True
    assert sample["role"] == "local"


async def test_probe_tcp_reports_jitter_across_connects():
    settings = Settings(ping_count=3, target_timeout=1.0, ping_gap=0.0)
    sample = await probe_tcp(tcp_target("127.0.0.1", 9), settings, round_id=4, ts=1000.0)
    assert sample["sent"] == 3
    assert sample["recv"] == 3
    assert sample["tcp_min_ms"] <= sample["tcp_avg_ms"] <= sample["tcp_max_ms"]
    assert sample["jitter_ms"] >= 0


async def test_probe_tcp_tries_ports_concurrently():
    """A black-holed multi-port target must not cost one timeout per port.

    Sequentially this was 3 ports x 0.3s x 2 attempts = 1.8s, which stretched a
    round well past the 2s probe interval.
    """
    target = Target(
        name="gw", role="local", label="gw", host="192.0.2.1", ports=(53, 80, 443)
    )
    settings = Settings(ping_count=2, target_timeout=0.3, ping_gap=0.0)
    sample = await probe_tcp(target, settings, round_id=11, ts=1000.0)
    assert sample["ok"] is False
    assert sample["elapsed_ms"] < 1200, sample["elapsed_ms"]
    # One error per port, de-duplicated across attempts.
    assert len(sample["error"].split(";")) == 3


async def test_measure_round_is_bounded_by_the_slowest_target():
    settings = Settings(ping_count=2, target_timeout=0.3, ping_gap=0.0)
    targets = [
        Target("gw", "local", "gw", host="192.0.2.1", ports=(53, 80, 443)),
        tcp_target("127.0.0.1", 9, "internet", "alive"),
    ]
    started = time.perf_counter()
    samples = await measure_round(settings, targets, round_id=12)
    elapsed = time.perf_counter() - started
    assert len(samples) == 2
    assert elapsed < 1.2, elapsed


async def test_probe_target_enforces_a_budget(monkeypatch):
    """Even a hung probe is converted into a failed sample, never a stall."""
    async def hang(*args, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr("droptrace.probes.probe_tcp", hang)
    settings = Settings(ping_count=1, target_timeout=0.2)
    started = time.perf_counter()
    sample = await probe_target(tcp_target(), settings, round_id=13, ts=1000.0)
    elapsed = time.perf_counter() - started
    assert sample["ok"] is False
    assert "budget" in (sample["error"] or "")
    assert elapsed < 2.0, elapsed


# ------------------------------------------------------------ dns probing
def test_build_dns_query_structure():
    txid, packet = build_dns_query("one.one.one.one")
    assert isinstance(txid, int) and 0 <= txid <= 0xFFFF
    assert struct.unpack(">H", packet[:2])[0] == txid
    assert struct.unpack(">H", packet[4:6])[0] == 1  # one question
    assert b"\x03one\x03one\x03one\x03one\x00" in packet
    assert packet.endswith(struct.pack(">HH", 1, 1))


def _fake_dns_server(response_builder):
    """UDP DNS responder on an ephemeral port, returning (thread, port)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    def serve():
        try:
            for _ in range(4):
                data, addr = sock.recvfrom(2048)
                reply = response_builder(data)
                if reply:
                    sock.sendto(reply, addr)
        except OSError:
            pass
        finally:
            sock.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread, port


def _valid_response(query: bytes) -> bytes:
    header = query[:2] + b"\x81\x80" + struct.pack(">HHHH", 1, 1, 0, 0)
    answer = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + bytes([1, 2, 3, 4])
    return header + query[12:] + answer


async def test_probe_dns_success_against_a_responder():
    _thread, port = _fake_dns_server(_valid_response)
    target = Target("dns", "dns", "dns", kind="dns", host="127.0.0.1", ports=(port,), probe_name="x.com")
    sample = await probe_dns(target, Settings(target_timeout=1.0), round_id=5, ts=1000.0)
    assert sample["ok"] is True
    assert sample["probe_ms"] is not None
    assert sample["dns_ms"] == sample["probe_ms"]
    assert sample["role"] == "dns"


async def test_probe_dns_detects_transaction_id_mismatch():
    def wrong_txid(query: bytes) -> bytes:
        reply = _valid_response(query)
        # Flip the echoed transaction id so the reply cannot be correlated.
        return bytes([query[0] ^ 0xFF, query[1] ^ 0xFF]) + reply[2:]

    _thread, port = _fake_dns_server(wrong_txid)
    target = Target("dns", "dns", "dns", kind="dns", host="127.0.0.1", ports=(port,), probe_name="x.com")
    sample = await probe_dns(target, Settings(target_timeout=1.0), round_id=6, ts=1000.0)
    assert sample["ok"] is False
    assert "transaction id" in (sample["error"] or "")


async def test_probe_dns_detects_error_rcode():
    def nxdomain(query: bytes) -> bytes:
        return query[:2] + b"\x81\x83" + struct.pack(">HHHH", 1, 0, 0, 0) + query[12:]

    _thread, port = _fake_dns_server(nxdomain)
    target = Target("dns", "dns", "dns", kind="dns", host="127.0.0.1", ports=(port,), probe_name="x.com")
    sample = await probe_dns(target, Settings(target_timeout=1.0), round_id=7, ts=1000.0)
    assert sample["ok"] is False
    assert "rcode 3" in (sample["error"] or "")


def _negative_response(rcode: int, authority: int = 1):
    """A Builder for an authoritative negative answer (NXDOMAIN or NODATA)."""

    def build(query: bytes) -> bytes:
        return query[:2] + bytes([0x81, 0x80 | (rcode & 0xF)]) + struct.pack(
            ">HHHH", 1, 0, authority, 0
        ) + query[12:]

    return build


async def test_the_uncached_probe_accepts_an_authoritative_negative_answer():
    """A random name must come back "no such name", and that is a success.

    The resolver cannot invent that answer from its cache -- it had to ask the
    authoritative servers -- which is exactly the half of DNS health a cached
    name can never show. Measured live, both a plain and a public resolver
    answer a random label under one.one.one.one with NOERROR + the zone's SOA.
    """
    for rcode in (3, 0):  # NXDOMAIN, and NODATA carrying the SOA
        _thread, port = _fake_dns_server(_negative_response(rcode))
        target = Target(
            "dns-upstream", "dns-upstream", "dns", kind="dns", host="127.0.0.1",
            ports=(port,), probe_name="probe-{random}.one.one.one.one", expect="negative",
        )
        sample = await probe_dns(target, Settings(target_timeout=1.0), round_id=8, ts=1000.0)
        assert sample["ok"] is True, sample["error"]
        assert sample["target"] == "dns-upstream"


async def test_a_bare_empty_answer_is_not_evidence_of_anything():
    """No records and no SOA: the resolver said nothing, so it proved nothing."""
    _thread, port = _fake_dns_server(_negative_response(0, authority=0))
    target = Target(
        "dns-upstream", "dns-upstream", "dns", kind="dns", host="127.0.0.1",
        ports=(port,), probe_name="probe-{random}.x.com", expect="negative",
    )
    sample = await probe_dns(target, Settings(target_timeout=1.0), round_id=9, ts=1000.0)
    assert sample["ok"] is False
    assert "no answers" in (sample["error"] or "")


async def test_a_servfail_says_so_in_the_uncached_probe():
    _thread, port = _fake_dns_server(_negative_response(2))
    target = Target(
        "dns-upstream", "dns-upstream", "dns", kind="dns", host="127.0.0.1",
        ports=(port,), probe_name="probe-{random}.x.com", expect="negative",
    )
    sample = await probe_dns(target, Settings(target_timeout=1.0), round_id=10, ts=1000.0)
    assert sample["ok"] is False
    assert "SERVFAIL" in (sample["error"] or "")
    assert (sample["error"] or "").startswith("uncached:"), "name the failing query"


async def test_a_random_label_is_asked_for_every_time():
    """Two probes must not share a name: a cached answer would hide the fault."""
    _thread, port = _fake_dns_server(_negative_response(0))
    target = Target(
        "dns-upstream", "dns-upstream", "dns", kind="dns", host="127.0.0.1",
        ports=(port,), probe_name="probe-{random}.one.one.one.one", expect="negative",
    )
    assert fresh_probe_name(target.probe_name) != fresh_probe_name(target.probe_name)
    assert fresh_probe_name("one.one.one.one") == "one.one.one.one"


async def test_probe_dns_does_not_depend_on_loop_socket_ops(monkeypatch):
    """uvloop (auto-selected by uvicorn) raises NotImplementedError for these.

    The probe used to work under `asyncio.run` and fail with an empty
    NotImplementedError once served over HTTP, so this is pinned down.
    """
    def unsupported(*args, **kwargs):
        raise NotImplementedError

    monkeypatch.setattr(asyncio.BaseEventLoop, "sock_sendto", unsupported, raising=False)
    monkeypatch.setattr(asyncio.BaseEventLoop, "sock_recv", unsupported, raising=False)

    _thread, port = _fake_dns_server(_valid_response)
    target = Target("dns", "dns", "dns", kind="dns", host="127.0.0.1", ports=(port,), probe_name="x.com")
    sample = await probe_dns(target, Settings(target_timeout=1.0), round_id=10, ts=1000.0)
    assert sample["ok"] is True, sample["error"]


async def test_probe_dns_timeout_is_a_failure():
    # Bind a port, then close it so nothing answers.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    target = Target("dns", "dns", "dns", kind="dns", host="127.0.0.1", ports=(port,), probe_name="x.com")
    sample = await probe_dns(target, Settings(target_timeout=0.3), round_id=8, ts=1000.0)
    assert sample["ok"] is False
    assert sample["loss_pct"] == 100.0


# ---------------------------------------------------------------- rounds
def sample(target, role, ok, probe_ms=None, error=None, ts=1000.0):
    return {
        "target": target, "role": role, "ok": ok, "probe_ms": probe_ms,
        "error": error, "ts": ts, "kind": "latency",
    }


def test_evaluate_round_roles():
    verdict = evaluate_round([
        sample("resolver", "local", True, 5.0),
        sample("gateway", "lan", False, error="timeout"),
        sample("cloudflare", "internet", True, 12.0),
        sample("google", "internet", True, 19.0),
        sample("dns", "dns", True, 30.0),
    ])
    assert verdict["internet_ok"] is True
    assert verdict["local_ok"] is True   # the resolver answered
    assert verdict["lan_ok"] is False    # ...but the router did not
    assert verdict["dns_ok"] is True
    assert verdict["failed"] == ["gateway"]
    assert verdict["total"] == 5


def test_evaluate_round_missing_role_is_none_not_false():
    verdict = evaluate_round([sample("cloudflare", "internet", True, 12.0)])
    assert verdict["internet_ok"] is True
    assert verdict["lan_ok"] is None
    assert verdict["local_ok"] is None
    assert verdict["dns_ok"] is None


def test_classify_outage_attribution():
    # The router still answers -> the ISP/upstream is at fault.
    assert classify_outage({"internet_ok": False, "lan_ok": True}) == "isp"
    # The router is gone too -> the local network is at fault.
    assert classify_outage({"internet_ok": False, "lan_ok": False}) == "local"
    # No router evidence to judge by.
    assert classify_outage({"internet_ok": False, "lan_ok": None}) == "internet"
    # Everything fine.
    assert classify_outage({"internet_ok": True, "lan_ok": True}) == ""


def test_a_resolver_answer_is_not_evidence_that_the_lan_is_fine():
    """A WSL resolver proxy sits on loopback and answers with the wire cut.

    Crediting that as "the LAN was fine" is how a drop gets blamed on the ISP
    without anything having crossed the cable, so the resolver role must not be
    able to produce an "isp" verdict.
    """
    verdict = evaluate_round([
        sample("resolver", "local", True, 0.3),
        sample("dns", "dns", True, 4.0),
        sample("cloudflare", "internet", False, error="timeout on 443"),
        sample("google", "internet", False, error="timeout on 443"),
    ])
    assert verdict["local_ok"] is True     # the proxy answered
    assert verdict["lan_ok"] is None       # but nothing tested the wire
    assert classify_outage(verdict) == "internet"


async def test_measure_round_probes_every_target_concurrently():
    settings = Settings(ping_count=1, target_timeout=0.2, ping_gap=0.0)
    targets = [
        tcp_target("192.0.2.1", 443, "internet", "dead"),
        tcp_target("127.0.0.1", 9, "local", "alive"),
    ]
    samples = await measure_round(settings, targets, round_id=42)
    assert len(samples) == 2
    by_target = {s["target"]: s for s in samples}
    assert by_target["dead"]["ok"] is False
    assert by_target["alive"]["ok"] is True
    # One shared round identifier ties the samples of a round together.
    assert {s["round_id"] for s in samples} == {42}
    assert len({s["ts"] for s in samples}) == 1


async def test_measure_round_with_no_targets():
    assert await measure_round(Settings(), [], round_id=1) == []


async def test_measure_round_never_raises_on_a_broken_target():
    class Exploding(Target):
        pass

    settings = Settings(ping_count=1, target_timeout=0.2)
    # A host name that cannot resolve still yields a sample rather than raising.
    weird = Target("weird", "internet", "weird", host="", ports=(443,))
    samples = await measure_round(settings, [weird], round_id=9)
    assert len(samples) == 1
    assert samples[0]["ok"] is False


# --------------------------------------------------------- handshake bursts
async def test_a_burst_counts_what_came_back():
    """Twenty handshakes at a port nobody listens on: all refused, none lost.

    A RST is an answer, exactly as in a round probe, so a burst against a closed
    port measures 0% loss rather than 100%. The number has to mean "packets that
    did not arrive", not "connections that did not open".
    """
    sample = await measure_burst(
        "probe", "127.0.0.1", 9, count=6, window=0.2, timeout=0.5,
        max_seconds=2.0, round_id=11, ts=1000.0,
    )
    assert sample["kind"] == "burst"
    assert sample["sent"] == 6 and sample["recv"] == 6
    assert sample["loss_pct"] == 0.0
    assert sample["refused"] == 6
    assert sample["ok"] is True and sample["error"] is None
    assert sample["probe_ms"] is not None


async def test_a_burst_spreads_across_the_window_and_reports_total_loss():
    """Six handshakes over half a second, none of them answered.

    TEST-NET-1 is reserved and never routed: either it is blackholed (timeout)
    or something on the path answers with ICMP, and both are losses.
    """
    started = time.perf_counter()
    sample = await measure_burst(
        "probe", "192.0.2.1", 443, count=6, window=0.4, timeout=0.3,
        max_seconds=2.4, round_id=12, ts=1000.0,
    )
    elapsed = time.perf_counter() - started
    assert sample["sent"] == 6 and sample["recv"] == 0
    assert sample["loss_pct"] == 100.0
    assert sample["ok"] is False
    assert "6/6 handshakes lost" in (sample["error"] or "")
    # The burst is paced across the window instead of fired at once, and it is
    # bounded: six attempts at 0.3s cannot run past its budget by much.
    assert elapsed >= 0.35, f"fired at once instead of paced: {elapsed:.2f}s"
    assert elapsed < 6, f"not bounded: {elapsed:.2f}s"


async def test_a_burst_stops_at_its_budget_and_says_how_many_it_sent():
    """A dead path costs the timeout per handshake, so the burst must stop."""
    sample = await measure_burst(
        "probe", "192.0.2.1", 443, count=20, window=1.0, timeout=0.3,
        max_seconds=0.8, round_id=13, ts=1000.0,
    )
    assert 0 < sample["sent"] < 20, "the budget cut the burst short"
    assert sample["loss_pct"] == 100.0
    assert sample["recv"] == 0
