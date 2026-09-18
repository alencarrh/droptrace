"""Command line interface.

    python -m droptrace serve     # dashboard + continuous sampling (default)
    python -m droptrace run       # headless continuous sampling
    python -m droptrace probe     # one round now, prints a report
    python -m droptrace outages   # list recorded outages (the evidence)
    python -m droptrace report    # aggregates from the stored samples
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import time
from pathlib import Path

from .config import MB, Settings, parse_seconds
from .guard import InstanceLock
from .probes import OUTAGE_LABELS
from .sampler import Sampler
from .storage import Store
from .targets import build_targets


# ------------------------------------------------------------------ formatting
def fmt_ms(value) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}ms" if value < 100 else f"{value:.0f}ms"


def fmt_mbps(value) -> str:
    if value is None:
        return "—"
    return f"{value:.0f} Mbps" if value >= 100 else f"{value:.2f} Mbps"


def fmt_bytes(value) -> str:
    if not value:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return f"{size:.0f} {units[index]}" if index == 0 else f"{size:.1f} {units[index]}"


def fmt_clock(ts) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "—"


def fmt_duration(seconds) -> str:
    if seconds is None:
        return "—"
    if 0 < seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    seconds = int(max(0, seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


# ---------------------------------------------------------------- cli parsing
def add_common(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("targets")
    group.add_argument(
        "--public-targets",
        metavar="LIST",
        help="comma separated host[:port] internet targets (default 1.1.1.1:443,8.8.8.8:443)",
    )
    group.add_argument("--extra-targets", metavar="LIST", help="extra targets as name=host:port,...")
    group.add_argument("--no-gateway", action="store_true", help="do not probe the router")
    group.add_argument(
        "--raw-window-hours", type=float, metavar="H",
        help="keep individual probes this long, then hourly stats (default 168, 0 = never fold)",
    )
    group.add_argument(
        "--lan-gateway", metavar="IP",
        help="router to probe when auto-detection picks the wrong one",
    )
    group.add_argument("--no-resolver", action="store_true", help="do not probe the DNS resolver")
    group.add_argument(
        "--fact-check-targets", metavar="LIST",
        help="hosts on other networks used to confirm a drop (host:port,..., empty = off)",
    )
    group.add_argument(
        "--fact-check-interval", type=float, metavar="SEC",
        help="how often to probe them while healthy (default 900, 0 = only when a drop is suspected)",
    )
    group.add_argument("--dns-probe-name", metavar="NAME", help="name resolved to test DNS health")
    group.add_argument(
        "--dns-servers", metavar="LIST",
        help="resolvers to compare with this machine's own (host[:port],..., empty = none)",
    )
    group.add_argument(
        "--dns-cache-bust-interval", type=float, metavar="SEC",
        help="ask each resolver for an uncached random name this often (default 60, 0 = off)",
    )
    group.add_argument("--target-timeout", type=float, metavar="SEC", help="per-connect timeout (default 1)")
    group.add_argument("--ping-count", type=int, metavar="N", help="connects per target per round (default 2)")
    group.add_argument("--ping-gap", type=float, metavar="SEC", help="pause between connects (default 0.05)")
    group.add_argument(
        "--burst-handshakes", type=int, metavar="N",
        help="handshakes fired at a drop to measure loss (default 20, 0 = off)",
    )
    group.add_argument(
        "--burst-window", type=float, metavar="SEC",
        help="spread the burst across this many seconds (default 1)",
    )
    group.add_argument("--burst-host", metavar="HOST", help="host to burst at (default 1.1.1.1)")
    group.add_argument(
        "--burst-cooldown", type=float, metavar="SEC",
        help="at most one burst this often (default 120)",
    )
    group.add_argument(
        "--trace-host", metavar="HOST",
        help="trace the path to this host when a drop starts and hourly while healthy (empty = off)",
    )
    group.add_argument(
        "--trace-interval", type=float, metavar="SEC",
        help="seconds between healthy baseline traces (default 3600, 0 = only at a drop)",
    )
    group.add_argument(
        "--trace-timeout", type=float, metavar="SEC",
        help="hard cap on one trace (default 20); 0 turns tracing off",
    )
    group.add_argument("--trace-max-hops", type=int, metavar="N", help="hops to try (default 20)")
    group.add_argument(
        "--trace-blackout-interval", type=float, metavar="SEC",
        help="also trace after a heavy-loss burst, at most this often (default 900, 0 = off)",
    )

    sched = parser.add_argument_group("scheduling")
    sched.add_argument("--latency-interval", type=float, metavar="SEC", help="seconds between rounds (default 2)")
    sched.add_argument("--quick-interval", type=float, metavar="SEC",
                       help="seconds between cheap burst tests (default 600, 0 = off)")
    sched.add_argument("--sustained-interval", type=float, metavar="SEC",
                       help="seconds between sustained duration tests (default 3600, 0 = off)")
    sched.add_argument("--duration", metavar="SEC", help="run time, 0 = forever (default forever). Accepts 8h.")
    sched.add_argument("--incident-min-rounds", type=int, metavar="N", help="failed rounds before an outage is logged")
    sched.add_argument("--fast-interval", type=float, metavar="SEC",
                       help="probe this often while an outage is in progress (default 1, 0 = off)")
    sched.add_argument("--fast-timeout", type=float, metavar="SEC",
                       help="connect timeout while probing fast (default 0.5)")
    sched.add_argument("--fast-hold-seconds", type=float, metavar="SEC",
                       help="stay fast until this long recovered (default 10)")
    sched.add_argument("--fast-max-seconds", type=float, metavar="SEC",
                       help="never stay fast longer than this (default 300)")
    sched.add_argument("--no-latency", action="store_true", help="disable latency rounds")
    sched.add_argument("--no-autostart", action="store_true", help="start with sampling paused")
    sched.add_argument(
        "--ssl-certfile", metavar="PEM",
        help="serve HTTPS with this certificate (needed for the screen Wake Lock API)",
    )
    sched.add_argument("--ssl-keyfile", metavar="PEM", help="private key for --ssl-certfile")

    xfer = parser.add_argument_group("throughput")
    xfer.add_argument("--download-url", help="URL that streams bytes down")
    xfer.add_argument("--upload-url", help="URL that accepts an upload body")
    xfer.add_argument("--download-seconds", type=float, metavar="SEC",
                      help="how long the download test runs (default 10)")
    xfer.add_argument("--upload-seconds", type=float, metavar="SEC",
                      help="how long the upload test runs (default 10)")
    xfer.add_argument("--download-chunk-bytes", type=int, metavar="N",
                      help="bytes per download request (default 64 MiB, endpoint caps ~100 MB)")
    xfer.add_argument("--upload-chunk-bytes", type=int, metavar="N",
                      help="block size streamed into an upload (default 1 MiB)")
    xfer.add_argument("--max-test-bytes", type=int, metavar="N",
                      help="stop a direction after N bytes; 0 = no cap (default 0)")
    xfer.add_argument("--streams", type=int, metavar="N", help="parallel connections per direction")
    xfer.add_argument("--request-timeout", type=float, metavar="SEC", help="HTTP timeout (default 60)")
    xfer.add_argument("--no-download", action="store_true", help="disable download tests")
    xfer.add_argument("--no-upload", action="store_true", help="disable upload tests")

    store_group = parser.add_argument_group("storage")
    store_group.add_argument("--db", dest="db_path", metavar="PATH", help="SQLite file (default ~/.local/share/droptrace/droptrace.db)")
    store_group.add_argument("--retention-days", type=float, metavar="N", help="prune data older than N days")


def parse_duration(text: str | None) -> float | None:
    """Accept bare seconds or a ``10m`` / ``2h`` / ``90s`` suffix."""
    if text is None:
        return None
    seconds = parse_seconds(text)
    if seconds is None:
        raise argparse.ArgumentTypeError(f"invalid duration: {text}")
    return seconds


def build_settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    mapping = {
        "public_targets": "public_targets",
        "extra_targets": "extra_targets",
        "dns_probe_name": "dns_probe_name",
        "dns_servers": "dns_servers",
        "dns_cache_bust_interval": "dns_cache_bust_interval",
        "burst_handshakes": "burst_handshakes",
        "burst_window": "burst_window",
        "burst_host": "burst_host",
        "burst_cooldown": "burst_cooldown",
        "trace_host": "trace_host",
        "trace_interval": "trace_interval",
        "trace_timeout": "trace_timeout",
        "trace_max_hops": "trace_max_hops",
        "trace_blackout_interval": "trace_blackout_interval",
        "target_timeout": "target_timeout",
        "ping_count": "ping_count",
        "ping_gap": "ping_gap",
        "latency_interval": "latency_interval",
        "quick_interval": "quick_interval",
        "quick_download_bytes": "quick_download_bytes",
        "quick_upload_bytes": "quick_upload_bytes",
        "sustained_interval": "sustained_interval",
        "incident_min_rounds": "incident_min_rounds",
        "fast_interval": "fast_interval",
        "fast_timeout": "fast_timeout",
        "fast_hold_seconds": "fast_hold_seconds",
        "fast_max_seconds": "fast_max_seconds",
        "download_url": "download_url",
        "upload_url": "upload_url",
        "download_seconds": "download_seconds",
        "upload_seconds": "upload_seconds",
        "download_chunk_bytes": "download_chunk_bytes",
        "upload_chunk_bytes": "upload_chunk_bytes",
        "max_test_bytes": "max_test_bytes",
        "streams": "streams",
        "request_timeout": "request_timeout",
        "db_path": "db_path",
        "retention_days": "retention_days",
    }
    for attr, field in mapping.items():
        value = getattr(args, attr, None)
        if value is not None:
            if field == "db_path":
                value = Path(value)
            setattr(settings, field, value)

    duration = parse_duration(getattr(args, "duration", None))
    if duration is not None:
        settings.duration = duration
    for flag, field in (
        ("no_latency", "enable_latency"),
        ("no_download", "enable_download"),
        ("no_upload", "enable_upload"),
        ("no_gateway", "probe_gateway"),
        ("no_resolver", "probe_resolver"),
    ):
        if getattr(args, flag, False):
            setattr(settings, field, False)
    if getattr(args, "no_autostart", False):
        settings.auto_start = False
    if getattr(args, "fact_check_targets", None) is not None:
        settings.fact_check_targets = args.fact_check_targets
    if getattr(args, "fact_check_interval", None) is not None:
        settings.fact_check_interval = max(0.0, args.fact_check_interval)
    if getattr(args, "raw_window_hours", None) is not None:
        settings.raw_window_hours = max(0.0, args.raw_window_hours)
    if getattr(args, "lan_gateway", None):
        settings.lan_gateway = args.lan_gateway
    if getattr(args, "bind", None):
        settings.bind = args.bind
    if getattr(args, "web_port", None):
        settings.web_port = args.web_port
    settings.streams = max(1, settings.streams)
    settings.ping_count = max(1, settings.ping_count)
    settings.latency_interval = max(0.2, settings.latency_interval)
    return settings


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="droptrace",
        description="Continuous connectivity monitor that records outages as evidence.",
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the dashboard and the sampler (default)")
    add_common(serve)
    serve.add_argument("--bind", default=None, help="interface to bind (default 127.0.0.1)")
    serve.add_argument("--web-port", type=int, default=None, help="HTTP port (default 8777)")
    serve.add_argument("--log-level", default="info", help="uvicorn log level")
    serve.add_argument(
        "--open", dest="open_browser", action="store_true",
        help="open the dashboard in your browser once the server is up",
    )

    run = sub.add_parser("run", help="headless continuous sampling, prints live lines")
    add_common(run)
    run.add_argument("--quiet", action="store_true", help="only print outages and speed tests")

    probe = sub.add_parser("probe", help="take a single round now and exit")
    add_common(probe)
    probe.add_argument("--kind", choices=("all", "latency", "speed"), default="all")
    probe.add_argument("--json", action="store_true", help="emit raw JSON")
    probe.add_argument("--no-save", action="store_true", help="do not write to the database")

    outages = sub.add_parser("outages", help="list recorded outages")
    outages.add_argument("--window", default="24h", help="1h, 24h, 7d, all or seconds")
    outages.add_argument("--limit", type=int, default=50, help="how many rows")
    outages.add_argument("--db", dest="db_path", metavar="PATH", help="SQLite file")

    agent = sub.add_parser(
        "agent", help="measure from this machine and report to a DropTrace server"
    )
    agent.add_argument("--server", required=True, help="e.g. http://192.168.1.50:8777")
    agent.add_argument("--token", required=True, help="the token shown on the dashboard")
    agent.add_argument(
        "--source", default="",
        help="optional: check that the token belongs to this device before measuring",
    )
    agent.add_argument("--interval", type=float, default=5.0, help="seconds between rounds")
    agent.add_argument("--cycles", type=int, default=0, help="stop after N rounds (0 = forever)")
    agent.add_argument("--quiet", action="store_true", help="only print failures")

    burst = sub.add_parser("burst", help="measure loss now with a counted handshake burst")
    burst.add_argument("--host", default=None, help="host to burst at (default: the settings)")
    burst.add_argument("--count", type=int, default=None, help="handshakes to send (default 20)")
    burst.add_argument("--window", type=float, default=None, help="seconds to spread them over")
    burst.add_argument("--db", dest="db_path", metavar="PATH", help="SQLite file")
    burst.add_argument("--no-save", action="store_true", help="print it, do not store it")

    trace = sub.add_parser("trace", help="trace the path now, and keep the hop list")
    trace.add_argument("--host", default=None, help="target to trace (default: the settings)")
    trace.add_argument("--timeout", type=float, default=None, help="hard cap in seconds (default 20)")
    trace.add_argument("--max-hops", type=int, default=None, help="hops to try (default 20)")
    trace.add_argument("--db", dest="db_path", metavar="PATH", help="SQLite file")
    trace.add_argument("--no-save", action="store_true", help="print it, do not store it")

    report = sub.add_parser("report", help="print aggregates from stored samples")
    report.add_argument("--window", default="all", help="1h, 24h, all or seconds")
    report.add_argument("--db", dest="db_path", metavar="PATH", help="SQLite file")
    return parser


# ------------------------------------------------------------------ commands
class _NullStore:
    """Used by ``probe --no-save``: keeps samples in memory, writes nothing."""

    def __init__(self) -> None:
        self.saved: list[dict] = []

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def close_dangling(self, ts: float) -> int:
        return 0

    async def add(self, sample: dict) -> dict:
        self.saved.append(sample)
        return sample

    async def add_many(self, samples) -> int:
        self.saved.extend(samples)
        return len(samples)

    async def open_incident(self, **kwargs) -> int:
        return 0

    async def extend_incident(self, *args, **kwargs) -> None:
        return None

    async def close_incident(self, *args, **kwargs) -> None:
        return None


async def cmd_probe(settings: Settings, kind: str, as_json: bool, save: bool) -> int:
    store = Store(settings.db_path) if save else _NullStore()
    sampler = Sampler(settings, store)  # type: ignore[arg-type]
    if save:
        await store.connect()
    try:
        started = time.perf_counter()
        await sampler.run_once(kind)
        elapsed = time.perf_counter() - started
    finally:
        if save:
            await store.close()

    round_data = sampler.last_round
    speed = sampler.last_speed
    if as_json:
        import json

        print(json.dumps({"round": round_data, "speed": speed}, indent=2, default=str))
        return 0

    print()
    print("  DropTrace — single round")
    print("  " + "─" * 66)
    if round_data:
        print(f"  {'target':<12}{'role':<10}{'address':<26}{'result':>16}")
        for sample in round_data["samples"]:
            target = next((t for t in sampler.targets if t.name == sample["target"]), None)
            address = target.describe() if target else "?"
            result = fmt_ms(sample.get("probe_ms")) if sample.get("ok") else "FAILED"
            print(f"  {sample['target']:<12}{sample.get('role',''):<10}{address:<26}{result:>16}")
            if not sample.get("ok") and sample.get("error"):
                print(f"  {'':<12}! {sample['error'][:70]}")
        verdict = round_data["verdict"]
        state = "UP" if verdict.get("internet_ok") else "DOWN"
        print("  " + "─" * 66)
        print(f"  internet: {state}")
        if round_data.get("scope"):
            print(f"  verdict : {round_data['scope_label']}")
    if speed:
        print(f"  download: {fmt_mbps(speed.get('download_mbps'))}   "
              f"upload: {fmt_mbps(speed.get('upload_mbps'))}")
        if speed.get("error"):
            print(f"  ! {speed['error'][:80]}")
    print(f"  finished in {elapsed:.1f}s")
    print()
    return 0


async def cmd_run(settings: Settings, quiet: bool) -> int:
    lock = _claim_database(settings, "run")
    if lock is None:
        return 1
    store = Store(settings.db_path)
    await store.connect()
    sampler = Sampler(settings, store)
    queue = sampler.subscribe()

    targets = build_targets(settings)
    print()
    print("  DropTrace — continuous drop monitor")
    print(f"  {settings.describe()}")
    print(f"  targets: {', '.join(t.name for t in targets) or 'none'}")
    print("  " + "─" * 78)

    await sampler.start()
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if not sampler.running:
                    break
                continue
            kind = event.get("type")
            if kind == "round" and not quiet:
                _print_round(event["round"], sampler)
            elif kind == "sample" and event["sample"].get("kind") == "speed":
                sample = event["sample"]
                print(
                    f"  {fmt_clock(sample['ts'])}  speed test   "
                    f"down {fmt_mbps(sample.get('download_mbps')):>12}   "
                    f"up {fmt_mbps(sample.get('upload_mbps')):>12}"
                )
                if sample.get("error"):
                    print(f"  {'':<10}! {sample['error'][:90]}")
            elif kind == "outage_start":
                incident = event["incident"]
                print(
                    f"  {fmt_clock(incident['started_at'])}  ⚠ OUTAGE STARTED — "
                    f"{incident.get('label') or incident.get('scope')}   "
                    f"failed: {', '.join(incident.get('failed_targets') or []) or 'all'}"
                )
            elif kind == "outage_end":
                incident = event["incident"] or {}
                print(
                    f"  {fmt_clock(incident.get('ended_at'))}  ✔ RECOVERED after "
                    f"{fmt_duration(incident.get('duration_s'))} "
                    f"({incident.get('rounds')} rounds)"
                )
            elif kind == "target":
                print(f"  {'':<10}  note: {event.get('reason')}")
            if not sampler.running:
                break
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if sampler.running:
            await sampler.stop("interrupted")
        sampler.unsubscribe(queue)
        await _print_run_summary(sampler, store)
        await store.close()
        lock.release()
    return 0


def _print_round(round_data: dict, sampler: Sampler) -> None:
    verdict = round_data["verdict"]
    up = verdict.get("internet_ok")
    status = "up  " if up else "DOWN"
    latencies = [
        f"{s['target']}={fmt_ms(s.get('probe_ms'))}"
        for s in round_data["samples"]
        if s.get("ok")
    ]
    failures = [s["target"] for s in round_data["samples"] if not s.get("ok")]
    detail = " ".join(latencies)
    if failures:
        detail += f"   failed: {','.join(failures)}"
    print(f"  {fmt_clock(round_data['ts'])}  {status}  {detail}")


async def _print_run_summary(sampler: Sampler, store: Store) -> None:
    summary = await store.summary(0.0)
    uptime = summary.get("uptime") or {}
    incidents = summary.get("incidents") or {}
    print("  " + "─" * 78)
    print(f"  run finished ({sampler.stop_reason or 'stopped'})")
    print(
        f"  uptime {uptime.get('up_pct')}% over {uptime.get('rounds', 0)} rounds "
        f"({uptime.get('rounds_down', 0)} down)"
    )
    print(
        f"  outages: {incidents.get('count', 0)} total, "
        f"{fmt_duration(incidents.get('downtime_s', 0))} of downtime, "
        f"longest {fmt_duration(incidents.get('longest_s', 0))}"
    )
    down = summary.get("download_mbps") or {}
    up = summary.get("upload_mbps") or {}
    print(
        f"  throughput: down avg {fmt_mbps(down.get('avg'))} (peak {fmt_mbps(down.get('max'))})"
        f"   up avg {fmt_mbps(up.get('avg'))} (peak {fmt_mbps(up.get('max'))})"
    )
    print(
        f"  data used {fmt_bytes(summary['bytes']['downloaded'] + summary['bytes']['uploaded'])}"
        f"   samples {summary['counts']['total']}"
    )
    print()


async def cmd_outages(settings: Settings, window: str, limit: int) -> int:
    from .api import _resolve_since

    store = Store(settings.db_path)
    await store.connect()
    try:
        since = _resolve_since(window, None)
        rows = await store.incidents(since, limit)
        ongoing = await store.current_incidents()
        stats = await store.incident_stats(since)
    finally:
        await store.close()

    print()
    print(f"  DropTrace — recorded outages ({window})")
    print("  " + "─" * 88)
    if not rows and not ongoing:
        print("  no outages recorded in this window — nothing to report")
        print()
        return 0
    print(f"  {'started':<10}{'ended':<10}{'duration':>10}{'kind':<10}{'scope':<10}{'rounds':>7}  failed targets")
    for row in [*ongoing, *rows]:
        ended = fmt_clock(row.get("ended_at")) if row.get("ended_at") else "ONGOING"
        scope = OUTAGE_LABELS.get(row.get("scope") or "", row.get("scope") or "")
        print(
            f"  {fmt_clock(row['started_at']):<10}{ended:<10}"
            f"{fmt_duration(row.get('duration_s')):>10}"
            f"{(row.get('kind') or ''):<10}{(row.get('scope') or ''):<10}"
            f"{row.get('rounds') or 0:>7}  {row.get('failed_targets') or ''}"
        )
    print("  " + "─" * 88)
    print(
        f"  {stats['count']} completed, {stats['ongoing']} ongoing, "
        f"{fmt_duration(stats['downtime_s'])} total downtime, "
        f"longest {fmt_duration(stats['longest_s'])}, mean {fmt_duration(stats['average_s'])}"
    )
    if stats["by_scope"]:
        print(f"  attribution: {stats['by_scope']}")
    if stats["by_kind"]:
        print(f"  by kind: {stats['by_kind']}")
    print()
    return 0


async def cmd_burst(settings: Settings, host: str, count: int, window: float,
                    save: bool) -> int:
    from .probes import measure_burst

    if count <= 0:
        print("\n  a burst of zero handshakes measures nothing; pass --count N\n")
        return 2
    sample = await measure_burst(
        host, host, 443, count=count, window=window,
        timeout=settings.burst_timeout,
        max_seconds=window + count * settings.burst_timeout,
        round_id=0, ts=time.time(),
    )
    if save:
        store = Store(settings.db_path)
        await store.connect()
        try:
            await store.add_many([sample])
        finally:
            await store.close()

    sent, recv = sample["sent"], sample["recv"]
    print()
    print(f"  DropTrace — handshake burst at {host}:443 over {window:g}s"
          f"{'' if save else ' (not stored)'}")
    print("  " + "─" * 62)
    print(f"  sent {sent}  received {recv}  lost {sent - recv}  "
          f"loss {sample['loss_pct']:.1f}%")
    if sample.get("probe_ms") is not None:
        print(f"  rtt min {sample['tcp_min_ms']:.1f} ms  avg {sample['tcp_avg_ms']:.1f} ms  "
              f"max {sample['tcp_max_ms']:.1f} ms  refused {sample['refused']}")
    if sample.get("error"):
        print(f"  {sample['error']}")
    print()
    return 0


async def cmd_trace(settings: Settings, host: str, timeout: float, max_hops: int,
                    save: bool) -> int:
    from .trace import trace_path

    result = await trace_path(host, timeout=timeout, max_hops=max_hops)
    result["trigger"] = "manual"

    if save:
        store = Store(settings.db_path)
        await store.connect()
        try:
            result["id"] = await store.add_trace(result)
        finally:
            await store.close()

    print()
    print(f"  DropTrace — path to {host} via {result['tracer']}"
          f"{'' if save else ' (not stored)'}")
    print("  " + "─" * 62)
    for hop in result["hop_list"]:
        rtt = f"{hop['rtt_ms']:.1f} ms" if hop.get("rtt_ms") is not None else "—"
        print(f"  {hop['ttl']:>3}  {(hop['host'] or 'no reply'):<20}{rtt:>10}  {hop.get('note') or ''}")
    print("  " + "─" * 62)
    verdict = "reached" if result["reached"] else (
        f"stopped at {result['last_hop'] or 'nowhere'} after hop {result['hops']}"
    )
    print(f"  {verdict} · {result['answered']}/{result['hops']} hops answered · "
          f"{result['duration_ms']:.0f} ms")
    if result.get("error"):
        print(f"  {result['error']}")
    print()
    return 0


async def cmd_report(settings: Settings, window: str) -> int:
    from .api import _resolve_since

    store = Store(settings.db_path)
    await store.connect()
    try:
        since = _resolve_since(window, None)
        summary = await store.summary(since)
        targets = await store.target_names("latency", since)
    finally:
        await store.close()

    print()
    print(f"  DropTrace — report ({window})")
    print("  " + "─" * 70)
    if not summary["counts"]["total"]:
        print("  no samples in this window")
        print()
        return 0
    uptime = summary["uptime"]
    print(
        f"  uptime {uptime['up_pct']}%  rounds {uptime['rounds']} "
        f"(down {uptime['rounds_down']})  probes {uptime['probes']}"
    )
    for label, key, fmt in (
        ("ping", "probe_ms", fmt_ms),
        ("jitter", "jitter_ms", fmt_ms),
        ("loss", "loss_pct", lambda v: f"{v:.2f} %"),
        ("download", "download_mbps", fmt_mbps),
        ("upload", "upload_mbps", fmt_mbps),
    ):
        block = summary.get(key)
        if not block:
            continue
        print(
            f"  {label:<10} avg {fmt(block['avg']):>12}   min {fmt(block['min']):>12}"
            f"   max {fmt(block['max']):>12}   p95 {fmt(block['p95']):>12}"
        )
    if targets:
        print("  " + "─" * 70)
        print(f"  {'target':<14}{'avg':>12}{'max':>12}{'n':>8}")
        for name in targets:
            block = summary["by_target"].get(name)
            if block:
                print(f"  {name:<14}{fmt_ms(block['avg']):>12}{fmt_ms(block['max']):>12}{block['n']:>8}")
    incidents = summary["incidents"]
    print("  " + "─" * 70)
    print(
        f"  outages {incidents['count']}  downtime {fmt_duration(incidents['downtime_s'])}"
        f"  longest {fmt_duration(incidents['longest_s'])}"
        f"  (internet {incidents['by_kind'].get('internet', {}).get('count', 0)},"
        f" dns {incidents['by_kind'].get('dns', {}).get('count', 0)})"
    )
    print(
        f"  data down {fmt_bytes(summary['bytes']['downloaded'])}"
        f"  up {fmt_bytes(summary['bytes']['uploaded'])}"
    )
    print()
    return 0


def _claim_database(settings: Settings, command: str) -> InstanceLock | None:
    """Take the per-database lock, or explain who has it and give up."""
    lock = InstanceLock(settings.db_path)
    holder = lock.acquire()
    if holder is None:
        return lock
    print()
    print(f"  Another DropTrace is already sampling {settings.db_path} (pid {holder}).")
    print("  Two samplers would halve the round spacing and double every count, so")
    print(f"  this '{command}' will not start.")
    print()
    print("  Stop the other instance, or use a different database with --db PATH.")
    print()
    return None


def cmd_serve(
    settings: Settings, log_level: str, open_dashboard: bool = False, args=None
) -> int:
    import uvicorn

    from .api import create_app
    from .launcher import local_urls, wait_and_open

    lock = _claim_database(settings, "serve")
    if lock is None:
        return 1

    store = Store(settings.db_path)
    sampler = Sampler(settings, store)
    app = create_app(settings, store, sampler)

    # A wildcard bind is not a usable address in a browser, so work out the
    # real addresses instead of printing something unopenable.
    host = "127.0.0.1" if settings.bind in ("0.0.0.0", "::", "") else settings.bind
    url = f"http://{host}:{settings.web_port}/"
    exposed = settings.bind not in ("127.0.0.1", "::1", "localhost")

    print()
    print("  DropTrace dashboard")
    print(f"  {settings.describe()}")
    for label, address in local_urls(settings.web_port, settings.bind):
        hint = "   <- from another device on this network" if label == "network" else ""
        print(f"  → {label:<8} {address}{hint}")
    print(f"  → API docs {url}docs")
    if exposed:
        print()
        print("  ! Listening on a network interface, and there is no authentication:")
        print("    anyone who can reach this port can read your data, pause sampling,")
        print("    run speed tests (which cost data) and delete stored results.")
    print("  Ctrl+C to stop")
    print()

    try:
        if open_dashboard:
            wait_and_open(url, f"http://{host}:{settings.web_port}/api/health")
        tls = {}
        if getattr(args, "ssl_certfile", None):
            tls = {"ssl_certfile": args.ssl_certfile, "ssl_keyfile": args.ssl_keyfile}
        if tls:
            print("  ! serving HTTPS: the phone must trust this certificate, or it will warn")
        uvicorn.run(
            app, host=settings.bind, port=settings.web_port, log_level=log_level, **tls
        )
    finally:
        lock.release()
    return 0


def _db_only_settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    if getattr(args, "db_path", None):
        settings.db_path = Path(args.db_path)
    return settings


async def cmd_agent(settings: Settings, args) -> int:
    """Measure from this machine and post the probes to a DropTrace server.

    Deliberately the same probe code as the server: a comparison between vantage
    points is only worth anything if both measured the same way. Nothing is
    written locally -- there is no database here at all.
    """
    import platform as host_platform

    import httpx

    from .probes import measure_round
    from .targets import build_targets

    server = args.server.rstrip("/")
    targets = build_targets(settings)
    headers = {"X-Agent-Token": args.token}

    # The token decides the label; --source, when given, is an assertion. Refusing
    # a mismatch is the double-check: a command copied from the wrong row of the
    # dashboard stops here instead of filing the laptop's drops as the phone's.
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            response = await client.get(f"{server}/api/agent/whoami", params={"token": args.token})
        except Exception as exc:  # noqa: BLE001
            print(f"  cannot reach {server}: {type(exc).__name__}: {exc}")
            return 1
        if response.status_code == 401:
            print("  this token is not registered (or was revoked). Add the device in the dashboard.")
            return 1
        who = response.json()
        if args.source and args.source != who["source"]:
            print(f"  token belongs to '{who['source']}', not '{args.source}' -- "
                  "refusing to measure, so nothing is filed under the wrong device.")
            return 1

    print(f"  DropTrace agent → {server}  as '{who['source']}'")
    print(f"  probing {len(targets)} targets every {args.interval:g}s"
          + (f", {args.cycles} rounds" if args.cycles else ", until stopped"))

    cycles = 0
    async with httpx.AsyncClient(timeout=20.0) as client:
        while not args.cycles or cycles < args.cycles:
            cycles += 1
            round_id = int(time.time() * 1000)
            probes = await measure_round(settings, targets, round_id)
            payload = {
                "platform": f"{host_platform.system()} {host_platform.release()}".strip()[:40],
                "agent": "python",
                "probes": [
                    {
                        "ts": p.get("ts"), "target": p.get("target"), "role": p.get("role"),
                        "ok": bool(p.get("ok")), "probe_ms": p.get("probe_ms"),
                        "error": p.get("error"), "round_id": round_id,
                    }
                    for p in probes
                ],
            }
            failed = sum(1 for p in probes if not p.get("ok"))
            try:
                response = await client.post(
                    f"{server}/api/agent", json=payload, headers=headers
                )
                if response.status_code == 401:
                    print("  rejected: wrong token (copy it from the dashboard panel)")
                    return 1
                response.raise_for_status()
                stored = response.json().get("stored", 0)
            except Exception as exc:  # noqa: BLE001 - a flaky link must not stop the loop
                print(f"  {time.strftime('%H:%M:%S')}  could not report to {server}: "
                      f"{type(exc).__name__}: {exc}")
                await asyncio.sleep(args.interval)
                continue
            if failed or not args.quiet:
                stamp = time.strftime("%H:%M:%S")
                worst = max((p.get("probe_ms") or 0) for p in probes) or 0
                print(f"  {stamp}  stored {stored} probes, {failed} failed, worst {worst:.0f}ms")
            if not args.cycles or cycles < args.cycles:
                await asyncio.sleep(args.interval)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    command = args.command or "serve"

    if command in ("report", "outages"):
        settings = _db_only_settings(args)
        if command == "report":
            return asyncio.run(cmd_report(settings, args.window))
        return asyncio.run(cmd_outages(settings, args.window, args.limit))

    settings = build_settings(args)

    if command == "serve":
        return cmd_serve(settings, args.log_level, args.open_browser, args)
    if command == "run":
        try:
            return asyncio.run(cmd_run(settings, args.quiet))
        except KeyboardInterrupt:
            return 130
    if command == "probe":
        try:
            return asyncio.run(cmd_probe(settings, args.kind, args.json, not args.no_save))
        except KeyboardInterrupt:
            return 130
    if command == "burst":
        try:
            return asyncio.run(cmd_burst(
                settings,
                args.host or settings.burst_host,
                args.count if args.count is not None else settings.burst_handshakes,
                args.window if args.window is not None else settings.burst_window,
                not args.no_save,
            ))
        except KeyboardInterrupt:
            return 130
    if command == "trace":
        try:
            return asyncio.run(cmd_trace(
                settings,
                args.host or settings.trace_host,
                args.timeout if args.timeout is not None else settings.trace_timeout,
                args.max_hops if args.max_hops is not None else settings.trace_max_hops,
                not args.no_save,
            ))
        except KeyboardInterrupt:
            return 130
    if command == "agent":
        try:
            return asyncio.run(cmd_agent(settings, args))
        except KeyboardInterrupt:
            print("\n  agent stopped")
            return 130
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
