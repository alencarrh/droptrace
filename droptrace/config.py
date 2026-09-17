"""DropTrace configuration.

Every setting can be supplied three ways, in increasing priority:

1. the dataclass defaults below,
2. ``DROPTRACE_*`` environment variables,
3. command line flags (see ``droptrace.__main__``).

The defaults are tuned for *catching intermittent drops*: a cheap latency round
every 2 seconds against several targets, plus an occasional throughput test.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

ENV_PREFIX = "DROPTRACE_"

MB = 1024 * 1024


def _raw(name: str) -> str | None:
    value = os.environ.get(ENV_PREFIX + name)
    if value is None or value.strip() == "":
        return None
    return value.strip()


def _env_str(name: str, default: str) -> str:
    return _raw(name) or default


def _env_int(name: str, default: int) -> int:
    value = _raw(name)
    if value is None:
        return default
    try:
        return int(float(value))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = _raw(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = _raw(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on", "y"}


_SUFFIXES = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_seconds(text: str | float | int | None) -> float | None:
    """Parse ``90``, ``90s``, ``10m``, ``2h`` or ``1d`` into seconds.

    Returns ``None`` when the value cannot be understood.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    raw = str(text).strip().lower()
    if not raw:
        return None
    if raw in ("all", "forever", "unlimited", "inf", "infinite"):
        return 0.0
    suffix = raw[-1]
    try:
        if suffix in _SUFFIXES:
            return float(raw[:-1]) * _SUFFIXES[suffix]
        return float(raw)
    except ValueError:
        return None


@dataclass(slots=True)
class Settings:
    """Runtime settings for the sampler, the store and the web UI."""

    # --- targets -----------------------------------------------------------
    # Comma separated ``host`` or ``host:port`` entries probed as "internet".
    public_targets: str = "1.1.1.1:443,8.8.8.8:443"
    # Probe the machine's DNS resolver:53 as a "local hop" reachability check.
    # This is *not* LAN evidence: under WSL it is usually a proxy inside the VM.
    probe_resolver: bool = True
    # Probe the router itself. Only this probe can say "the LAN was fine", so it
    # is what a drop gets attributed with. Auto-detected guesses are validated at
    # runtime and dropped if they never answer (a router may filter TCP entirely).
    probe_gateway: bool = True
    # Set this when auto-detection picks the wrong router (empty = detect).
    lan_gateway: str = ""
    # Shared secret for the agent ingest and for the mutating endpoints. Empty
    # means "generate one on first use and keep it in the database"; it is only
    # used as-is when it is set explicitly.
    agent_token: str = ""
    # Resolve this name against the local resolver to check DNS health.
    dns_probe_name: str = "one.one.one.one"
    # Resolvers to compare with the machine's own, side by side: host[:port],
    # comma separated. The comparison is what answers "is name resolution broken
    # on this line, or only my resolver?" -- an ISP saying "your DNS is fine"
    # needs a second resolver asked in the same second. Empty = no comparison.
    dns_servers: str = "1.1.1.1"
    # Every this many seconds, also ask each resolver for a *random* subdomain of
    # dns_probe_name. A label nobody has asked for cannot be answered from cache,
    # so it is the only query that exposes a resolver whose upstream is dead
    # while its cache keeps answering -- which is exactly how a browser dies
    # while "DNS is up" looks true. Kept slow on purpose: each one is a real
    # query to the authoritative servers. 0 = cached names only.
    dns_cache_bust_interval: float = 60.0
    # Extra targets: ``name=host:port`` entries, comma separated.
    extra_targets: str = ""
    # Corroboration hosts, on separate networks, probed only when a round looks
    # like a drop -- and once every fact_check_interval so we know which of them
    # are reachable at all on this connection. A drop confirmed by five networks
    # is evidence; a drop that only one provider saw is a provider problem.
    fact_check_targets: str = (
        "github.com:443,wikipedia.org:443,twitch.tv:443,youtube.com:443,9gag.com:443"
    )
    fact_check_interval: float = 900.0
    # Path tracing: where the traffic stops, which is the one piece of evidence
    # that names equipment instead of describing symptoms. Traced once when a
    # drop is first seen, and once in a while while healthy so the broken path
    # has something honest to be compared with. Empty host = off.
    trace_host: str = "1.1.1.1"
    # Baseline cadence while the connection is healthy (0 = never).
    trace_interval: float = 3600.0
    # Hard cap on one trace. Tracing a broken path hangs by nature, and the
    # sampler must not wait for it -- this bounds the child process, not the
    # probe loop, which keeps running.
    trace_timeout: float = 20.0
    # Per-hop wait handed to the tracer.
    trace_hop_timeout: float = 0.7
    trace_max_hops: int = 20
    # At most one trace this often, so a flapping link cannot spawn one a second.
    trace_cooldown: float = 120.0
    # A burst that loses at least this share of its handshakes also earns a
    # trace, and at most one per this many seconds. Those partial blackouts
    # (both primary endpoints dark, the corroboration pool still answering)
    # never open an incident, and "where did it stop" is exactly the question
    # they leave open. 0 = only trace at a full drop.
    trace_blackout_interval: float = 900.0
    # Per-connect timeout for a target probe. Deliberately short: a connect that
    # has not completed in a second is indistinguishable from a drop, and a long
    # timeout stretches a failing round past the probe interval.
    target_timeout: float = 1.0
    # TCP connects per target per round (>=2 yields a per-sample jitter value).
    ping_count: int = 2
    # Pause between the connects of one target.
    ping_gap: float = 0.05
    # A guessed target that never answers this many times is stood down.
    target_probation_rounds: int = 3
    # --- loss, measured rather than inferred ---------------------------------
    # When a round fails, fire this many handshakes at burst_host and count what
    # came back. Loss is *the* number for "it stops for five seconds": the line
    # is up, the packets are not arriving, and a 0%/100% round probe cannot say
    # how much of it got through. 0 turns the burst off.
    burst_handshakes: int = 20
    # Spread them across this many seconds rather than firing them at once, so
    # the figure describes the link and not this machine's socket backlog.
    burst_window: float = 1.0
    # Per-handshake timeout during a burst. Shorter than a round probe's: a dead
    # path costs this per attempt, and twenty of them must still be bounded.
    burst_timeout: float = 0.4
    # Host to burst at. Matched against the public targets so the row carries the
    # same name as the round probes; anything else is used as-is on 443.
    burst_host: str = "1.1.1.1"
    # At most one burst this often, so a flapping link cannot burst continuously.
    # Two minutes keeps the record dense enough to be evidence (this connection
    # has ~47 partial blackouts an hour) while capping the extra handshakes at
    # ~14,000/day.
    burst_cooldown: float = 120.0
    # --- scheduling --------------------------------------------------------
    # Normal probe cadence (seconds). This sets *detection*, and detection is
    # the thing that cannot be recovered afterwards: a drop shorter than this
    # can fall entirely between two probes and never be seen at all. At 5s a 5s
    # drop still contains a probe; at 10s about half of them would be missed, so
    # raising this trades away evidence rather than just traffic.
    latency_interval: float = 5.0
    # Adaptive resolution. The moment a round finds no internet target
    # answering, switch to this cadence to pin down the start and the end of the
    # drop to within a second. 0 disables it.
    fast_interval: float = 1.0
    # Timeout per connect while probing fast. A round cannot be faster than the
    # timeout of the target that is failing, so a dead target with the normal
    # 1s x 2 attempts set a ~2s floor on the round and the 1s cadence was never
    # actually achieved. One short attempt is enough to confirm an outage.
    fast_timeout: float = 0.5
    # Stay fast until this many seconds of continuous success confirm recovery,
    # so the tail of a flapping drop is still resolved at the fast cadence.
    fast_hold_seconds: float = 10.0
    # ...but never longer than this, so an outage that lasts all night cannot
    # flood the database with one-second rows.
    fast_max_seconds: float = 300.0
    # Two throughput tiers, because they answer different questions.
    #
    # Quick: a small fixed burst, frequently. Cheap enough to run often (~7 MiB
    # a go) and it answers "what is my peak right now", but on a fast line it is
    # over in a fraction of a second, so it cannot see a link that starts fast
    # and then throttles.
    quick_interval: float = 600.0
    quick_download_bytes: int = 5 * MB
    quick_upload_bytes: int = 2 * MB
    # Sustained: runs for a configured duration and buckets the rate per second,
    # so it exposes throttling. Costs `speed x duration`, so it runs far less
    # often. Set an interval to 0 to disable that tier.
    sustained_interval: float = 3600.0
    # Total run length in seconds; 0 means "run until stopped".
    duration: float = 0.0
    # Consecutive failed rounds before an outage is recorded.
    incident_min_rounds: int = 1

    # --- transfer ----------------------------------------------------------
    host: str = "speed.cloudflare.com"
    port: int = 443
    download_url: str = "https://speed.cloudflare.com/__down"
    upload_url: str = "https://speed.cloudflare.com/__up"
    # How long each direction runs. A fixed byte count measures a burst, not a
    # sustained rate: 5 MiB on a 400 Mbit line is a tenth of a second, so a link
    # that starts fast and then throttles looks perfect. Running for a duration
    # measures the steady state and exposes the decay.
    download_seconds: float = 10.0
    upload_seconds: float = 10.0
    # Bytes per download request. Each request costs one round trip before the
    # next starts, so small chunks badly under-report: measured on a 470 Mbit
    # line, 2 MiB chunks gave 199 Mbps and 48 MiB chunks gave 467 Mbps. The
    # endpoint rejects `bytes` above ~100 MB (64 MiB is a safe margin).
    download_chunk_bytes: int = 64 * MB
    # Block size streamed into a single upload request. Uploads use one
    # long-lived request per connection, so there is no per-block round trip and
    # this can be small, which gives a finer per-second view.
    upload_chunk_bytes: int = 1 * MB
    # Optional safety valve per direction; 0 disables it. At 10s a gigabit link
    # would move ~1.2 GB per test, so set this if you are on a data cap.
    max_test_bytes: int = 0
    # Parallel connections per direction (1 = single stream).
    streams: int = 1
    request_timeout: float = 60.0
    enable_latency: bool = True
    enable_download: bool = True
    enable_upload: bool = True

    # --- storage -----------------------------------------------------------
    db_path: Path = field(default_factory=lambda: Path("data/droptrace.db"))
    # Keep every individual probe for this long, then keep hourly statistics and
    # the failures forever. Raw probes are ~234 bytes each and 86,400 of them
    # arrive per day at the 5s default; an hour of statistics is one row per
    # target, so a year of history costs a few megabytes.
    raw_window_hours: float = 168.0
    retention_days: float = 30.0

    # --- web ---------------------------------------------------------------
    bind: str = "127.0.0.1"
    web_port: int = 8777
    auto_start: bool = True

    # ------------------------------------------------------------------ env
    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings, letting ``DROPTRACE_*`` variables win over defaults."""
        # ``slots=True`` turns class attributes into descriptors, so the
        # defaults have to be read off the dataclass fields themselves.
        default: dict = {}
        for field_info in dataclasses.fields(cls):
            if field_info.default is not dataclasses.MISSING:
                default[field_info.name] = field_info.default
            elif field_info.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
                default[field_info.name] = field_info.default_factory()  # type: ignore[misc]

        return cls(
            public_targets=_env_str("PUBLIC_TARGETS", default["public_targets"]),
            probe_resolver=_env_bool("PROBE_RESOLVER", default["probe_resolver"]),
            probe_gateway=_env_bool("PROBE_GATEWAY", default["probe_gateway"]),
            lan_gateway=_env_str("LAN_GATEWAY", default["lan_gateway"]),
            dns_probe_name=_env_str("DNS_PROBE_NAME", default["dns_probe_name"]),
            dns_servers=_env_str("DNS_SERVERS", default["dns_servers"]),
            dns_cache_bust_interval=_env_float(
                "DNS_CACHE_BUST_INTERVAL", default["dns_cache_bust_interval"]
            ),
            extra_targets=_env_str("EXTRA_TARGETS", default["extra_targets"]),
            fact_check_targets=_env_str("FACT_CHECK_TARGETS", default["fact_check_targets"]),
            fact_check_interval=_env_float("FACT_CHECK_INTERVAL", default["fact_check_interval"]),
            trace_host=_env_str("TRACE_HOST", default["trace_host"]),
            trace_interval=_env_float("TRACE_INTERVAL", default["trace_interval"]),
            trace_timeout=_env_float("TRACE_TIMEOUT", default["trace_timeout"]),
            trace_hop_timeout=_env_float("TRACE_HOP_TIMEOUT", default["trace_hop_timeout"]),
            trace_max_hops=_env_int("TRACE_MAX_HOPS", default["trace_max_hops"]),
            trace_cooldown=_env_float("TRACE_COOLDOWN", default["trace_cooldown"]),
            trace_blackout_interval=_env_float(
                "TRACE_BLACKOUT_INTERVAL", default["trace_blackout_interval"]
            ),
            target_timeout=_env_float("TARGET_TIMEOUT", default["target_timeout"]),
            ping_count=_env_int("PING_COUNT", default["ping_count"]),
            ping_gap=_env_float("PING_GAP", default["ping_gap"]),
            burst_handshakes=_env_int("BURST_HANDSHAKES", default["burst_handshakes"]),
            burst_window=_env_float("BURST_WINDOW", default["burst_window"]),
            burst_timeout=_env_float("BURST_TIMEOUT", default["burst_timeout"]),
            burst_host=_env_str("BURST_HOST", default["burst_host"]),
            burst_cooldown=_env_float("BURST_COOLDOWN", default["burst_cooldown"]),
            target_probation_rounds=_env_int(
                "TARGET_PROBATION_ROUNDS", default["target_probation_rounds"]
            ),
            latency_interval=_env_float("LATENCY_INTERVAL", default["latency_interval"]),
            fast_interval=_env_float("FAST_INTERVAL", default["fast_interval"]),
            fast_timeout=_env_float("FAST_TIMEOUT", default["fast_timeout"]),
            fast_hold_seconds=_env_float("FAST_HOLD_SECONDS", default["fast_hold_seconds"]),
            fast_max_seconds=_env_float("FAST_MAX_SECONDS", default["fast_max_seconds"]),
            quick_interval=_env_float("QUICK_INTERVAL", default["quick_interval"]),
            quick_download_bytes=_env_int("QUICK_DOWNLOAD_BYTES", default["quick_download_bytes"]),
            quick_upload_bytes=_env_int("QUICK_UPLOAD_BYTES", default["quick_upload_bytes"]),
            sustained_interval=_env_float("SUSTAINED_INTERVAL", default["sustained_interval"]),
            duration=_env_float("DURATION", default["duration"]),
            incident_min_rounds=_env_int("INCIDENT_MIN_ROUNDS", default["incident_min_rounds"]),
            host=_env_str("HOST", default["host"]),
            port=_env_int("PORT", default["port"]),
            download_url=_env_str("DOWNLOAD_URL", default["download_url"]),
            upload_url=_env_str("UPLOAD_URL", default["upload_url"]),
            download_seconds=_env_float("DOWNLOAD_SECONDS", default["download_seconds"]),
            upload_seconds=_env_float("UPLOAD_SECONDS", default["upload_seconds"]),
            download_chunk_bytes=_env_int("DOWNLOAD_CHUNK_BYTES", default["download_chunk_bytes"]),
            upload_chunk_bytes=_env_int("UPLOAD_CHUNK_BYTES", default["upload_chunk_bytes"]),
            max_test_bytes=_env_int("MAX_TEST_BYTES", default["max_test_bytes"]),
            streams=max(1, _env_int("STREAMS", default["streams"])),
            request_timeout=_env_float("REQUEST_TIMEOUT", default["request_timeout"]),
            enable_latency=_env_bool("ENABLE_LATENCY", default["enable_latency"]),
            enable_download=_env_bool("ENABLE_DOWNLOAD", default["enable_download"]),
            enable_upload=_env_bool("ENABLE_UPLOAD", default["enable_upload"]),
            db_path=Path(_env_str("DB_PATH", str(default["db_path"]))),
            raw_window_hours=_env_float("RAW_WINDOW_HOURS", default["raw_window_hours"]),
            retention_days=_env_float("RETENTION_DAYS", default["retention_days"]),
            bind=_env_str("BIND", default["bind"]),
            web_port=_env_int("WEB_PORT", default["web_port"]),
            auto_start=_env_bool("AUTO_START", default["auto_start"]),
        )

    # -------------------------------------------------------------- helpers
    def to_dict(self) -> dict:
        data = asdict(self)
        data["db_path"] = str(self.db_path)
        return data

    def describe(self) -> str:
        """One-line human summary used by the CLI banner."""
        mins = self.duration / 60 if self.duration else 0
        span = f"{mins:g} min" if self.duration else "until stopped"
        quick = f"{self.quick_interval:g}s" if self.quick_interval else "off"
        sustained = f"{self.sustained_interval:g}s" if self.sustained_interval else "off"
        return (
            f"latency round every {self.latency_interval:g}s  "
            f"burst test every {quick}  sustained test every {sustained}  "
            f"run={span}  db={self.db_path}"
        )


# Field names that end up as SQLite columns in the ``samples`` table.
SAMPLE_FIELDS: tuple[str, ...] = (
    "ts",
    "kind",
    "target",
    "role",
    "ok",
    "error",
    "probe_ms",
    "refused",
    "throttled",
    "dns_ms",
    "tcp_min_ms",
    "tcp_avg_ms",
    "tcp_max_ms",
    "tcp_p95_ms",
    "jitter_ms",
    "loss_pct",
    "sent",
    "recv",
    "http_ms",
    "tier",
    "trigger",
    "download_mbps",
    "upload_mbps",
    "download_bytes",
    "upload_bytes",
    "download_ttfb_ms",
    "upload_ttfb_ms",
    "download_intervals",
    "upload_intervals",
    "download_decay_pct",
    "upload_decay_pct",
    "capped",
    "elapsed_ms",
    "streams",
    "round_id",
    # Which vantage point produced the sample: "local" for this machine, or the
    # label a remote agent reported (phone-wifi, macbook-eth, ...). Remote probes
    # are stored and compared; they never decide this machine's verdict.
    "source",
)

# Incident kinds tracked independently.
INCIDENT_KINDS = ("internet", "dns")
