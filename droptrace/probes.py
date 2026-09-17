"""The actual measurements.

Latency ("ping")
    ICMP is deliberately *not* used: raw sockets need ``CAP_NET_RAW`` or setuid
    root and are unavailable in most containers and sandboxes. Instead a probe
    is a **TCP handshake** to the target, which is what a browser or ``curl``
    actually experiences. A ``TCP RST`` (connection refused) still proves the
    host answered, so it counts as *reachable* -- important for routers that
    filter most ports.

    Each round probes every target concurrently and writes one sample per
    target, tagged with its ``role`` (local / internet / dns) so a failed round
    can be attributed instead of just recorded.

Throughput
    Measured against a plain HTTPS endpoint that streams a requested number of
    bytes down and accepts a body on upload. The rate is computed over the
    transfer window, so the TLS handshake does not deflate a short test. For
    uploads the server only answers once the body has arrived, so the wall
    clock is the honest denominator there.
"""

from __future__ import annotations

import asyncio
import math
import secrets
import socket
import struct
import time
from typing import Any, Sequence

import httpx

from .config import Settings
from .targets import Target

CHUNK = 64 * 1024
USER_AGENT = "droptrace/0.2 (+https://localhost) httpx"


class RateLimited(Exception):
    """The speed test provider refused the request (HTTP 429).

    This is *not* a connectivity failure: the link is fine, the provider is
    asking us to slow down. It gets its own type so the sampler can back off
    instead of logging a phantom outage.
    """

    def __init__(self, retry_after: str | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(
            "rate limited by the speed test provider (HTTP 429)"
            + (f", retry after {retry_after}s" if retry_after else "")
        )


# --------------------------------------------------------------------- utils
def now() -> float:
    return time.time()


def perf() -> float:
    return time.perf_counter()


def percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile (``pct`` in 0..100)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return float(ordered[int(rank)])
    weight = rank - low
    return float(ordered[low] * (1 - weight) + ordered[high] * weight)


def jitter(values: Sequence[float]) -> float:
    """Mean absolute difference between consecutive samples (ms)."""
    if len(values) < 2:
        return 0.0
    deltas = [abs(b - a) for a, b in zip(values, values[1:])]
    return sum(deltas) / len(deltas)


def mbps(nbytes: int, seconds: float) -> float:
    if seconds <= 0 or nbytes <= 0:
        return 0.0
    return (nbytes * 8) / seconds / 1e6


def make_payload(nbytes: int) -> bytes:
    """Incompressible-ish payload so CDN compression cannot inflate results."""
    nbytes = max(1024, int(nbytes))
    block = secrets.token_bytes(CHUNK)
    repeats = nbytes // CHUNK + 1
    return (block * repeats)[:nbytes]


def _clean(value: Any) -> Any:
    """Round floats and drop NaNs so every sample is JSON/SQLite friendly."""
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, 3)
    return value


def as_sample(data: dict) -> dict:
    return {k: _clean(v) for k, v in data.items()}


def build_client(settings: Settings) -> httpx.AsyncClient:
    timeout = httpx.Timeout(
        connect=min(10.0, settings.request_timeout),
        read=settings.request_timeout,
        write=settings.request_timeout,
        pool=min(10.0, settings.request_timeout),
    )
    limits = httpx.Limits(
        max_connections=max(8, settings.streams * 2),
        max_keepalive_connections=max(4, settings.streams),
    )
    return httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"},
    )


# ------------------------------------------------------------ latency probes
def _tcp_sample(
    target: Target,
    *,
    round_id: int,
    ts: float,
    samples: list[float],
    refused: int,
    sent: int,
    errors: list[str],
    elapsed_ms: float,
) -> dict:
    received = len(samples)
    return as_sample(
        {
            "ts": ts,
            "kind": "latency",
            "target": target.name,
            "role": target.role,
            "round_id": round_id,
            "ok": received > 0,
            "error": None if received else ("; ".join(errors)[:300] or "no response"),
            "probe_ms": (sum(samples) / received) if received else None,
            "refused": refused,
            "tcp_min_ms": min(samples) if samples else None,
            "tcp_avg_ms": (sum(samples) / received) if received else None,
            "tcp_max_ms": max(samples) if samples else None,
            "tcp_p95_ms": percentile(samples, 95) if samples else None,
            "jitter_ms": jitter(samples),
            "loss_pct": (sent - received) / sent * 100.0 if sent else 0.0,
            "sent": sent,
            "recv": received,
            "elapsed_ms": elapsed_ms,
        }
    )


async def _connect_once(
    host: str, port: int, timeout: float
) -> tuple[bool, float, bool, str | None]:
    """Try one TCP port.

    Returns ``(reachable, rtt_ms, was_refused, error)``. A ``TCP RST`` counts as
    reachable because it proves the host answered the packet -- routers and
    firewalls refuse most ports while being perfectly healthy.
    """
    attempt = perf()
    writer = None
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout
        )
        return True, (perf() - attempt) * 1000, False, None
    except ConnectionRefusedError:
        return True, (perf() - attempt) * 1000, True, None
    except asyncio.TimeoutError:
        return False, 0.0, False, f"timeout on {port}"
    except OSError as exc:
        return False, 0.0, False, f"{type(exc).__name__} on {port}: {exc.strerror or exc}"
    except Exception as exc:  # noqa: BLE001
        return False, 0.0, False, f"{type(exc).__name__} on {port}"
    finally:
        if writer is not None:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), 1.0)
            except Exception:  # noqa: BLE001
                pass


async def measure_burst(
    target_name: str,
    host: str,
    port: int,
    *,
    count: int,
    window: float,
    timeout: float,
    max_seconds: float,
    round_id: int,
    ts: float,
) -> dict:
    """A burst of handshakes fired at a drop, and counted rather than inferred.

    A round probe answers "is it up". This answers "how much of it is arriving",
    which is the number for a line that is *up* but losing packets -- the five
    seconds when nothing gets through while the router still answers ping. The
    attempts are spread across ``window`` seconds instead of fired at once:
    twenty connects in the same millisecond measure this machine's socket
    backlog, not the link.

    Everything is bounded: a dead path costs ``timeout`` per handshake, so the
    burst stops early at ``max_seconds`` and records how many it actually sent.
    Refused counts as received, exactly as in a round probe -- a RST proves the
    packet arrived.
    """
    started = perf()
    results: list[tuple[bool, float, bool, str | None]] = []
    errors: list[str] = []
    for index in range(max(1, count)):
        if index:
            due = started + window * index / max(1, count - 1)
            delay = due - perf()
            if delay > 0:
                await asyncio.sleep(delay)
        results.append(await _connect_once(host, port, timeout))
        if not results[-1][0] and results[-1][3]:
            errors.append(results[-1][3])
        if perf() - started >= max_seconds:
            break

    rtts = [rtt for ok, rtt, _refused, _error in results if ok]
    received = len(rtts)
    sent = len(results)
    refused = sum(1 for ok, _rtt, was_refused, _error in results if ok and was_refused)
    lost = sent - received
    error = None
    if not received:
        detail = "; ".join(dict.fromkeys(errors))[:200] or "no response"
        error = f"{lost}/{sent} handshakes lost: {detail}"
    return as_sample(
        {
            "ts": ts,
            "kind": "burst",
            "target": target_name,
            "role": "internet",
            "round_id": round_id,
            "ok": received > 0,
            "error": error,
            "probe_ms": (sum(rtts) / received) if received else None,
            "refused": refused,
            "tcp_min_ms": min(rtts) if rtts else None,
            "tcp_avg_ms": (sum(rtts) / received) if received else None,
            "tcp_max_ms": max(rtts) if rtts else None,
            "tcp_p95_ms": percentile(rtts, 95) if rtts else None,
            "jitter_ms": jitter(rtts),
            "loss_pct": (lost / sent * 100.0) if sent else 0.0,
            "sent": sent,
            "recv": received,
            "elapsed_ms": (perf() - started) * 1000,
        }
    )


async def probe_tcp(target: Target, settings: Settings, round_id: int, ts: float) -> dict:
    """One round against a TCP target: ``ping_count`` attempts across its ports.

    All of a target's ports are tried *concurrently*. Sequentially, a
    gateway-style target with three black-holed ports cost three timeouts per
    attempt, which stretched a round to twelve seconds and wrecked the cadence.
    """
    started = perf()
    count = max(1, settings.ping_count)
    ports = target.ports or (443,)
    samples: list[float] = []
    errors: list[str] = []
    refused = 0

    for index in range(count):
        results = await asyncio.gather(
            *(_connect_once(target.host, port, settings.target_timeout) for port in ports)
        )
        winner = next((result for result in results if result[0]), None)
        if winner is not None:
            samples.append(winner[1])
            if winner[2]:
                refused += 1
        else:
            for result in results:
                if result[3] and result[3] not in errors:
                    errors.append(result[3])
        if index < count - 1 and settings.ping_gap > 0:
            await asyncio.sleep(settings.ping_gap)

    return _tcp_sample(
        target,
        round_id=round_id,
        ts=ts,
        samples=samples,
        refused=refused,
        sent=count,
        errors=errors,
        elapsed_ms=(perf() - started) * 1000,
    )


# ------------------------------------------------------------------ dns probe
def fresh_probe_name(name: str) -> str:
    """Fill the ``{random}`` slot with a label nobody has asked for before.

    DNS is where a cache turns "the upstream is dead" into "everything is
    fine": the resolver keeps answering the names it already knows. A label that
    has never been queried cannot be cached anywhere, so the answer -- including
    an authoritative NXDOMAIN -- has to have come from the servers themselves.
    """
    if "{random}" not in name:
        return name
    return name.replace("{random}", f"probe-{secrets.token_hex(4)}")


def build_dns_query(name: str, qtype: int = 1) -> tuple[int, bytes]:
    """Minimal DNS A-record query (RFC 1035) for ``name``."""
    txid = secrets.randbits(16)
    flags = 0x0100  # standard query, recursion desired
    header = struct.pack(">HHHHHH", txid, flags, 1, 0, 0, 0)
    labels = b"".join(bytes([len(part)]) + part.encode("idna") for part in name.split(".") if part)
    question = labels + b"\x00" + struct.pack(">HH", qtype, 1)
    return txid, header + question


def _dns_exchange(server: str, port: int, packet: bytes, timeout: float) -> bytes:
    """Blocking DNS exchange. Runs in a worker thread, see ``probe_dns``."""
    family = socket.AF_INET6 if ":" in server else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (server, port))
        data, _ = sock.recvfrom(2048)
        return data
    finally:
        sock.close()


async def probe_dns(target: Target, settings: Settings, round_id: int, ts: float) -> dict:
    """Ask a resolver directly, bypassing the OS cache.

    Resolving through ``getaddrinfo`` would usually be answered from cache, so a
    dead resolver could look healthy for minutes. Sending the query ourselves
    over UDP exposes the real state.

    Two kinds of target use this. The plain ones ask for a name that is already
    in every cache and expect a record back -- that is "the resolver still
    answers". The ``expect="negative"`` ones ask for a name that cannot be
    cached and accept an authoritative *negative* answer instead: NXDOMAIN, or
    NOERROR with the zone's SOA and no records (NODATA). Both prove the query
    left the machine and came back from the servers, which is the half a cached
    name can never show.

    The exchange happens on a blocking socket in a worker thread rather than
    via ``loop.sock_sendto``/``loop.sock_recv``: uvloop -- which uvicorn selects
    automatically when it is installed -- raises ``NotImplementedError`` for
    those, which made this probe fail only when served over HTTP.
    """
    started = perf()
    server = target.host
    port = target.ports[0] if target.ports else 53
    loop = asyncio.get_running_loop()
    error: str | None = None
    elapsed_ms: float | None = None
    name = fresh_probe_name(target.probe_name or "one.one.one.one")
    uncached = target.expect == "negative"

    try:
        txid, packet = build_dns_query(name)
        data = await asyncio.wait_for(
            loop.run_in_executor(
                None, _dns_exchange, server, port, packet, settings.target_timeout
            ),
            settings.target_timeout + 0.5,
        )
        elapsed_ms = (perf() - started) * 1000
        if len(data) < 12:
            error = "short DNS response"
        else:
            reply_id, reply_flags, _, answers, authority, _ = struct.unpack(
                ">HHHHHH", data[:12]
            )
            rcode = reply_flags & 0x000F
            if reply_id != txid:
                error = "DNS transaction id mismatch"
            elif uncached and rcode == 3:
                pass  # NXDOMAIN: authoritative, and it had to be asked for
            elif uncached and rcode == 0 and answers == 0 and authority:
                pass  # NODATA carrying the zone's SOA: same proof, tube empty
            elif rcode != 0:
                error = f"DNS rcode {rcode}" + (" (SERVFAIL)" if rcode == 2 else "")
            elif answers == 0:
                error = "DNS response contained no answers"
    except (asyncio.TimeoutError, socket.timeout):
        error = f"DNS timeout after {settings.target_timeout:g}s"
    except OSError as exc:
        error = f"{type(exc).__name__}: {exc.strerror or exc}"
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"

    if error and uncached:
        # Say which query failed: "the cached name answered but the uncached one
        # did not" is the interesting half of the story, not a bare timeout.
        error = f"uncached: {error}"

    return as_sample(
        {
            "ts": ts,
            "kind": "latency",
            "target": target.name,
            "role": target.role,
            "round_id": round_id,
            "ok": error is None,
            "error": error,
            "probe_ms": elapsed_ms if error is None else None,
            "dns_ms": elapsed_ms if error is None else None,
            "sent": 1,
            "recv": 0 if error else 1,
            "loss_pct": 100.0 if error else 0.0,
            "elapsed_ms": (perf() - started) * 1000,
        }
    )


async def probe_target(target: Target, settings: Settings, round_id: int, ts: float) -> dict:
    """Probe one target, hard-bounded so it can never stall the whole round."""
    budget = target_budget(settings)
    try:
        if target.kind == "dns":
            coro = probe_dns(target, settings, round_id, ts)
        else:
            coro = probe_tcp(target, settings, round_id, ts)
        return await asyncio.wait_for(coro, budget)
    except asyncio.TimeoutError:
        return as_sample(
            {
                "ts": ts,
                "kind": "latency",
                "target": target.name,
                "role": target.role,
                "round_id": round_id,
                "ok": False,
                "error": f"exceeded the {budget:.1f}s per-target budget",
                "sent": max(1, settings.ping_count),
                "recv": 0,
                "loss_pct": 100.0,
            }
        )
    except Exception as exc:  # noqa: BLE001 - a probe must never raise
        return as_sample(
            {
                "ts": ts,
                "kind": "latency",
                "target": target.name,
                "role": target.role,
                "round_id": round_id,
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "sent": max(1, settings.ping_count),
                "recv": 0,
                "loss_pct": 100.0,
            }
        )


def target_budget(settings: Settings) -> float:
    """Worst-case wall clock allowed for a single target in one round.

    Keeps a pathological target from delaying the round (and therefore from
    hiding the start of a short drop).
    """
    return max(0.5, settings.target_timeout * max(1, settings.ping_count) + 0.5)


async def measure_round(
    settings: Settings, targets: Sequence[Target], round_id: int | None = None
) -> list[dict]:
    """Probe every target concurrently; one sample per target."""
    if not targets:
        return []
    round_id = round_id if round_id is not None else int(now() * 1000)
    ts = now()
    samples = await asyncio.gather(
        *(probe_target(target, settings, round_id, ts) for target in targets)
    )
    return list(samples)


def evaluate_round(samples: Sequence[dict]) -> dict:
    """Turn a round's samples into a per-role verdict.

    ``None`` means "no target of that role exists", which is different from
    "every target of that role failed".
    """
    by_role: dict[str, list[dict]] = {}
    for sample in samples:
        by_role.setdefault(sample.get("role") or "internet", []).append(sample)

    def healthy(role: str) -> bool | None:
        items = by_role.get(role)
        if not items:
            return None
        return any(item.get("ok") for item in items)

    failed = [s["target"] for s in samples if not s.get("ok")]
    return {
        "internet_ok": healthy("internet"),
        # Only the router can say the LAN was fine. The resolver is excluded on
        # purpose: under WSL it is a proxy inside this machine (10.255.255.254
        # sits on loopback), so it answers even when nothing crosses the wire.
        "lan_ok": healthy("lan"),
        "local_ok": healthy("local"),
        "dns_ok": healthy("dns"),
        "failed": failed,
        "total": len(samples),
        "errors": {
            s["target"]: s.get("error") for s in samples if not s.get("ok") and s.get("error")
        },
    }


def classify_outage(verdict: dict) -> str:
    """Human-facing attribution for a round with no internet connectivity."""
    if verdict.get("internet_ok"):
        return ""
    lan = verdict.get("lan_ok")
    if lan is False:
        return "local"
    if lan is True:
        return "isp"
    return "internet"


OUTAGE_LABELS = {
    "local": "Local network down (router / Wi-Fi / host)",
    "isp": "ISP or upstream down (router answered)",
    "internet": "Internet unreachable (no router evidence)",
    "dns": "DNS resolution failing",
}


# ---------------------------------------------------------------- throughput
def _report_progress(
    clock: dict, report, phase: str, expected: float, extra: dict | None = None,
    force: bool = False,
) -> None:
    """Tell the caller how a transfer is going, at most a few times a second.

    Only the headline numbers and the per-second buckets so far: enough for a
    progress bar and a live rate, cheap enough to send while the transfer is
    still running.
    """
    if report is None:
        return
    now = perf()
    if not force and now - clock.get("reported_at", 0.0) < 0.25:
        return
    clock["reported_at"] = now
    elapsed = now - clock["t0"]
    moved = int(clock.get("bytes", 0))
    ttfb_s = (clock.get("ttfb_ms") or 0.0) / 1000.0
    # The same window the final number uses, so the live rate does not disagree
    # with the result when the test ends.
    window = max(elapsed - ttfb_s, 1e-6) if phase == "download" and ttfb_s else elapsed
    report({
        "phase": phase,
        "elapsed_s": round(elapsed, 2),
        "expected_s": round(expected, 1),
        "bytes": moved,
        "mbps": round(mbps(moved, window), 1),
        "intervals": _merge_buckets(clock.get("buckets") or {}, elapsed),
        **(extra or {}),
    })


async def _download_worker(
    client: httpx.AsyncClient, settings: Settings, index: int, clock: dict, buckets: dict,
    report=None, extra: dict | None = None,
) -> tuple[int, bool]:
    """Stream until the time budget runs out, bucketing bytes per second.

    Requests are repeated rather than asking for one huge body, because the
    endpoint rejects ``bytes`` above ~100 MB. Each request costs one round trip
    before the next begins, so ``download_chunk_bytes`` wants to be large: at
    2 MiB the round trips dominated and the same link measured 199 Mbps instead
    of 467 Mbps. The connection is kept alive throughout so TCP does not keep
    re-entering slow start.
    """
    deadline = clock["t0"] + settings.download_seconds
    chunk_bytes = max(64 * 1024, int(settings.download_chunk_bytes))
    cap = int(settings.max_test_bytes)
    total = 0
    capped = False
    request = 0

    while perf() < deadline:
        if cap and total >= cap:
            capped = True
            break
        request += 1
        params = {
            "bytes": chunk_bytes,
            "measId": f"{secrets.token_hex(6)}-{index}-{request}",
        }
        async with client.stream("GET", settings.download_url, params=params) as response:
            if response.status_code == 429:
                raise RateLimited(response.headers.get("Retry-After"))
            response.raise_for_status()
            if index == 0:
                clock.setdefault("headers_ms", (perf() - clock["t0"]) * 1000)
            async for chunk in response.aiter_bytes(CHUNK):
                offset = perf() - clock["t0"]
                if clock.get("ttfb_ms") is None:
                    clock["ttfb_ms"] = offset * 1000
                total += len(chunk)
                clock["bytes"] = clock.get("bytes", 0) + len(chunk)
                slots = buckets.setdefault(index, {})
                second = int(offset)
                slots[second] = slots.get(second, 0) + len(chunk)
                _report_progress(clock, report, "download", settings.download_seconds, extra)
                if perf() >= deadline:
                    break
    return total, capped


async def _upload_worker(
    client: httpx.AsyncClient, settings: Settings, index: int, clock: dict, buckets: dict,
    payload: bytes, report=None, extra: dict | None = None,
) -> tuple[int, bool]:
    """One long-lived POST whose body is generated until the time budget is up.

    Streaming the body avoids the per-request acknowledgement round trip that
    made chunked uploads drift (a 5s test took 6.3s and under-reported), and it
    means the deadline is honoured mid-body instead of between requests.
    """
    deadline = clock["t0"] + settings.upload_seconds
    block = max(64 * 1024, int(settings.upload_chunk_bytes))
    block = min(block, len(payload))
    cap = int(settings.max_test_bytes)
    sent = {"total": 0, "capped": False}

    async def body():
        while perf() < deadline:
            if cap and sent["total"] >= cap:
                sent["capped"] = True
                return
            yield payload[:block]
            sent["total"] += block
            clock["bytes"] = clock.get("bytes", 0) + block
            slots = buckets.setdefault(index, {})
            second = max(0, int(perf() - clock["t0"]))
            slots[second] = slots.get(second, 0) + block
            _report_progress(clock, report, "upload", settings.upload_seconds, extra)

    params = {"measId": f"{secrets.token_hex(6)}-{index}"}
    async with client.stream(
        "POST", settings.upload_url, params=params, content=body()
    ) as response:
        if response.status_code == 429:
            raise RateLimited(response.headers.get("Retry-After"))
        response.raise_for_status()
        # The endpoint answers only once the body has arrived, so this is the
        # acknowledgement time rather than a time to first byte.
        ack = perf() - clock["t0"]
        known = clock.get("ttfb_ms")
        clock["ttfb_ms"] = ack * 1000 if known is None else min(known, ack * 1000)
        await response.aread()
    return sent["total"], sent["capped"]


def _merge_buckets(buckets: dict, elapsed: float | None = None) -> list[dict]:
    """Collapse per-worker buckets into one per-second series (Mbps)."""
    merged: dict[int, int] = {}
    for slots in buckets.values():
        for second, count in slots.items():
            merged[second] = merged.get(second, 0) + count
    items = sorted(merged.items())
    # The test stops mid-second, so the final bucket holds a fraction of a
    # second of traffic. Reporting it as a rate invents a collapse: a healthy
    # 400 Mbps link produced a final "0.5 Mbps" bucket and a -99.9% decay.
    if elapsed is not None and items and elapsed < items[-1][0] + 0.9:
        items = items[:-1]
    return [
        {"t": second, "mbps": round(count * 8 / 1e6, 2)}
        for second, count in items
    ]


def _decay_pct(intervals: list[dict]) -> float | None:
    """Whether the rate held up across a single test: first half vs second half.

    Compares the average of the first half against the second half, ignoring the
    first bucket (a partial second that also contains the connection's ramp-up).
    Halves rather than peak-versus-end, because one TCP stream varies by 10-20%
    second to second and peak-versus-end would cry wolf on a healthy link.

    Needs at least four usable seconds: a shorter test cannot distinguish
    throttling from ordinary jitter, so it reports nothing instead of guessing.
    """
    rates = [point["mbps"] for point in intervals]
    if len(rates) < 5:
        return None
    usable = rates[1:]
    half = len(usable) // 2
    if half < 2:
        return None
    early = sum(usable[:half]) / half
    late = sum(usable[half:]) / (len(usable) - half)
    if early <= 0:
        return None
    return round(min(0.0, (late - early) / early * 100), 1)


async def _burst_worker(
    client: httpx.AsyncClient, settings: Settings, kind: str, size: int, index: int, clock: dict,
    payload: bytes | None,
) -> int:
    """Move exactly ``size`` bytes and return how many arrived."""
    if kind == "download":
        params = {"bytes": size, "measId": f"{secrets.token_hex(6)}-{index}"}
        received = 0
        async with client.stream("GET", settings.download_url, params=params) as response:
            if response.status_code == 429:
                raise RateLimited(response.headers.get("Retry-After"))
            response.raise_for_status()
            async for chunk in response.aiter_bytes(CHUNK):
                if clock.get("ttfb_ms") is None:
                    clock["ttfb_ms"] = (perf() - clock["t0"]) * 1000
                received += len(chunk)
        return received

    assert payload is not None
    params = {"measId": f"{secrets.token_hex(6)}-{index}"}
    async with client.stream(
        "POST", settings.upload_url, params=params, content=payload
    ) as response:
        if response.status_code == 429:
            raise RateLimited(response.headers.get("Retry-After"))
        response.raise_for_status()
        ack = (perf() - clock["t0"]) * 1000
        known = clock.get("ttfb_ms")
        clock["ttfb_ms"] = ack if known is None else min(known, ack)
        await response.aread()
    return len(payload)


async def _measure_burst(settings: Settings, kind: str, total_bytes: int) -> dict:
    """The quick tier: move a fixed number of bytes as fast as the link allows.

    This is the original measurement and it is still the right tool for a
    frequent reading -- cheap, and it reports peak throughput. It is a burst
    though: on a fast line it is over in a fraction of a second, so it cannot
    see a link that starts fast and then throttles. That is the sustained
    tier's job.
    """
    clock: dict = {"t0": perf(), "ttfb_ms": None}
    errors: list[str] = []
    throttled: str | None = None
    workers = max(1, settings.streams)
    per_worker = max(64 * 1024, total_bytes // workers)
    sizes = [per_worker] * workers
    sizes[-1] = max(64 * 1024, total_bytes - per_worker * (workers - 1))

    payload = make_payload(per_worker) if kind == "upload" else None
    client = build_client(settings)
    async with client:
        tasks = [
            asyncio.create_task(
                _burst_worker(client, settings, kind, size, i, clock, payload)
            )
            for i, size in enumerate(sizes)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    moved = 0
    for item in results:
        if isinstance(item, RateLimited):
            throttled = item.retry_after or "?"
            continue
        if isinstance(item, BaseException):
            errors.append(f"{type(item).__name__}: {item}")
            continue
        moved += int(item)

    elapsed = perf() - clock["t0"]
    window = (
        max(elapsed - clock["ttfb_ms"] / 1000.0, 1e-6)
        if kind == "download" and clock["ttfb_ms"]
        else elapsed
    )
    return {
        "kind": kind,
        "bytes": moved,
        "elapsed_ms": elapsed * 1000,
        "window_ms": window * 1000,
        "ttfb_ms": clock["ttfb_ms"],
        "mbps": mbps(moved, window),
        "mbps_wall": mbps(moved, elapsed),
        "errors": errors,
        "throttled": throttled,
        "intervals": [],
        "decay_pct": None,
        "capped": False,
    }


async def _run_direction(
    settings: Settings, kind: str, workers: int, report=None, extra: dict | None = None
) -> dict:
    """Measure one direction for a fixed duration over ``workers`` connections."""
    seconds = settings.download_seconds if kind == "download" else settings.upload_seconds
    buckets: dict = {}
    clock: dict = {"t0": perf(), "ttfb_ms": None, "headers_ms": None, "buckets": buckets}
    _report_progress(clock, report, kind, seconds, extra, force=True)
    bytes_total = 0
    errors: list[str] = []
    throttled: str | None = None
    capped = False

    payload = make_payload(settings.upload_chunk_bytes) if kind == "upload" else None
    client = build_client(settings)
    async with client:
        if kind == "download":
            tasks = [
                asyncio.create_task(
                    _download_worker(client, settings, i, clock, buckets, report, extra)
                )
                for i in range(workers)
            ]
        else:
            assert payload is not None
            tasks = [
                asyncio.create_task(
                    _upload_worker(client, settings, i, clock, buckets, payload, report, extra)
                )
                for i in range(workers)
            ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    for item in results:
        if isinstance(item, RateLimited):
            throttled = item.retry_after or "?"
            continue
        if isinstance(item, BaseException):
            errors.append(f"{type(item).__name__}: {item}")
            continue
        bytes_total += int(item[0])
        capped = capped or bool(item[1])

    # Transfer window.
    #
    # Download: subtract the time to the first byte, so the handshake and the
    # wait for the CDN do not deflate the sustained rate.
    #
    # Upload: the endpoint only answers once the whole body has arrived, so the
    # wall clock is the honest denominator (subtracting TTFB here once produced
    # nonsense like "24 Gbps").
    elapsed = perf() - clock["t0"]
    if kind == "download" and clock["ttfb_ms"]:
        window = max(elapsed - clock["ttfb_ms"] / 1000.0, 1e-6)
    else:
        window = elapsed

    _report_progress(clock, report, kind, seconds, extra, force=True)
    intervals = _merge_buckets(buckets, elapsed)
    return {
        "kind": kind,
        "bytes": bytes_total,
        "elapsed_ms": elapsed * 1000,
        "window_ms": window * 1000,
        "ttfb_ms": clock["ttfb_ms"],
        "mbps": mbps(bytes_total, window),
        "mbps_wall": mbps(bytes_total, elapsed),
        "errors": errors,
        "throttled": throttled,
        "intervals": intervals,
        "decay_pct": _decay_pct(intervals),
        "capped": capped,
    }


async def measure_speed(
    settings: Settings,
    round_id: int | None = None,
    tier: str = "sustained",
    trigger: str = "scheduled",
    progress=None,
) -> dict:
    """Measure throughput in both directions.

    ``tier='quick'`` moves a fixed small payload: a burst, cheap and frequent.
    ``tier='sustained'`` runs each direction for its configured duration and
    buckets the rate per second: expensive, but it exposes throttling.
    """
    started = perf()
    quick = tier == "quick"
    # Which phases this run will actually perform, so the UI can show both.
    phases = []
    if settings.enable_download and (settings.quick_download_bytes > 0 if quick else settings.download_seconds > 0):
        phases.append("download")
    if settings.enable_upload and (settings.quick_upload_bytes > 0 if quick else settings.upload_seconds > 0):
        phases.append("upload")
    extra = {"tier": tier, "trigger": trigger, "phases": phases, "results": {}}
    report = progress
    sample: dict = {
        "ts": now(),
        "kind": "speed",
        "tier": tier,
        "trigger": trigger,
        "target": "speedtest",
        "role": "internet",
        "round_id": round_id if round_id is not None else int(now() * 1000),
        "ok": True,
        "throttled": False,
        "capped": False,
        "error": None,
        "download_mbps": None,
        "upload_mbps": None,
        "download_bytes": None,
        "upload_bytes": None,
        "download_ttfb_ms": None,
        "upload_ttfb_ms": None,
        "download_intervals": None,
        "upload_intervals": None,
        "download_decay_pct": None,
        "upload_decay_pct": None,
        "streams": max(1, settings.streams),
        "elapsed_ms": None,
    }
    problems: list[str] = []

    download_wanted = (
        settings.quick_download_bytes > 0 if quick else settings.download_seconds > 0
    )
    upload_wanted = (
        settings.quick_upload_bytes > 0 if quick else settings.upload_seconds > 0
    )

    if settings.enable_download and download_wanted:
        result = (
            await _measure_burst(settings, "download", settings.quick_download_bytes)
            if quick
            else await _run_direction(
                settings, "download", max(1, settings.streams), report, extra
            )
        )
        extra["results"]["download_mbps"] = round(result["mbps"], 1)
        sample["download_mbps"] = result["mbps"]
        sample["download_bytes"] = result["bytes"]
        sample["download_ttfb_ms"] = result["ttfb_ms"]
        sample["download_intervals"] = result["intervals"]
        sample["download_decay_pct"] = result["decay_pct"]
        sample["capped"] = sample["capped"] or result["capped"]
        if result["throttled"]:
            sample["throttled"] = True
            problems.append("down: rate limited (HTTP 429)")
        elif result["errors"]:
            problems.append("down: " + "; ".join(result["errors"])[:200])
        if result["bytes"] <= 0:
            sample["ok"] = False

    if settings.enable_upload and upload_wanted:
        result = (
            await _measure_burst(settings, "upload", settings.quick_upload_bytes)
            if quick
            else await _run_direction(
                settings, "upload", max(1, settings.streams), report, extra
            )
        )
        extra["results"]["upload_mbps"] = round(result["mbps"], 1)
        sample["upload_mbps"] = result["mbps"]
        sample["upload_bytes"] = result["bytes"]
        sample["upload_ttfb_ms"] = result["ttfb_ms"]
        sample["upload_intervals"] = result["intervals"]
        sample["upload_decay_pct"] = result["decay_pct"]
        sample["capped"] = sample["capped"] or result["capped"]
        if result["throttled"]:
            sample["throttled"] = True
            problems.append("up: rate limited (HTTP 429)")
        elif result["errors"]:
            problems.append("up: " + "; ".join(result["errors"])[:200])
        if result["bytes"] <= 0:
            sample["ok"] = False

    sample["elapsed_ms"] = (perf() - started) * 1000
    if sample["capped"]:
        problems.append(f"stopped at the {settings.max_test_bytes / 1e6:.0f} MB cap")
    if problems:
        sample["error"] = " | ".join(problems)[:500]
        if (
            not sample.get("throttled")
            and sample["download_mbps"] in (None, 0)
            and sample["upload_mbps"] in (None, 0)
        ):
            sample["ok"] = False
    return as_sample(sample)
