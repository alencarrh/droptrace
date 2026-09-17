"""Throughput tests against a local HTTP server (no internet required).

These pin down the transfer-window semantics, which are easy to get wrong:

* download subtracts TTFB (handshake + first byte) from the wall clock,
* upload does not, because the server only answers once the whole body arrived.
"""

from __future__ import annotations

import http.server
import threading
import time
from urllib.parse import parse_qs, urlparse

import pytest

from droptrace.config import MB, Settings
from droptrace.probes import RateLimited, measure_speed, _run_direction

FIRST_BYTE_DELAY = 0.20   # download: pretend the server is slow to start
ACK_DELAY = 0.25          # upload: pretend the server is slow to acknowledge
THROTTLE_AFTER = 1.5      # /__down-throttle: fast until here, then slow
THROTTLE_SLEEP = 0.15     # ...sleeping this long per 64 KiB chunk
STATE = {"throttle_started": None}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 - http.server API
        path = urlparse(self.path).path
        if path == "/__down-throttle":
            self._throttled_download()
            return
        if path == "/__down-429":
            self.send_response(429)
            self.send_header("Retry-After", "120")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path != "/__down":
            self._not_found()
            return
        params = parse_qs(urlparse(self.path).query)
        size = int(params.get("bytes", ["0"])[0])
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        time.sleep(FIRST_BYTE_DELAY)
        block = b"n" * 65536
        remaining = size
        while remaining > 0:
            piece = block[:remaining]
            self.wfile.write(piece)
            remaining -= len(piece)

    def _throttled_download(self):
        """Full speed for a while, then crawl.

        This is the shape of a cheap "burstable" package: perfect for the first
        seconds, then a fraction of the advertised rate. A fixed-byte test never
        sees it, because it has already finished.
        """
        params = parse_qs(urlparse(self.path).query)
        size = int(params.get("bytes", ["0"])[0])
        if STATE["throttle_started"] is None:
            STATE["throttle_started"] = time.time()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        block = b"t" * 65536
        remaining = size
        while remaining > 0:
            piece = block[:remaining]
            self.wfile.write(piece)
            remaining -= len(piece)
            if time.time() - STATE["throttle_started"] > THROTTLE_AFTER:
                time.sleep(THROTTLE_SLEEP)

    def do_POST(self):  # noqa: N802 - http.server API
        if urlparse(self.path).path != "/__up":
            self._not_found()
            return
        self._read_body()
        time.sleep(ACK_DELAY)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_body(self) -> int:
        """Read a request body, chunked or with Content-Length.

        Uploads stream their body for the whole test, so httpx sends
        ``Transfer-Encoding: chunked`` rather than a Content-Length.
        """
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            total = 0
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    break
                size = int(line.split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()   # trailer CRLF
                    break
                remaining = size
                while remaining > 0:
                    data = self.rfile.read(min(65536, remaining))
                    if not data:
                        return total
                    remaining -= len(data)
                    total += len(data)
                self.rfile.readline()       # CRLF terminating the chunk
            return total
        length = int(self.headers.get("Content-Length", 0))
        consumed = 0
        while consumed < length:
            data = self.rfile.read(min(65536, length - consumed))
            if not data:
                break
            consumed += len(data)
        return consumed

    def _not_found(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # silence the test output
        return


@pytest.fixture(scope="module")
def server():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    yield f"http://{host}:{port}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def local_settings(server: str, **overrides) -> Settings:
    base = dict(
        download_url=f"{server}/__down",
        upload_url=f"{server}/__up",
        download_seconds=1.0,
        upload_seconds=1.0,
        download_chunk_bytes=2 * MB,
        upload_chunk_bytes=MB,
        streams=1,
        request_timeout=30.0,
    )
    base.update(overrides)
    return Settings(**base)


async def test_download_window_excludes_ttfb(server):
    result = await _run_direction(local_settings(server), "download", 1)
    assert result["bytes"] > 0
    assert result["errors"] == []
    assert result["ttfb_ms"] >= FIRST_BYTE_DELAY * 1000 * 0.8
    # The slow first byte is excluded, so the transfer window is shorter than
    # the wall clock and the steady-state rate beats the wall-clock rate.
    assert result["window_ms"] < result["elapsed_ms"]
    assert result["mbps"] > 0 and result["mbps"] >= result["mbps_wall"]


async def test_upload_window_uses_wall_clock(server):
    """Regression: subtracting TTFB made uploads report absurd multi-Gbps."""
    result = await _run_direction(local_settings(server), "upload", 1)
    assert result["bytes"] > 0
    assert result["errors"] == []
    assert result["ttfb_ms"] >= ACK_DELAY * 1000 * 0.8
    # The ack only arrives after the body, so window == wall clock.
    assert result["window_ms"] == pytest.approx(result["elapsed_ms"], rel=0.10)
    assert result["mbps"] == pytest.approx(result["mbps_wall"], rel=0.10)
    # Anything far above loopback-plausible means the window collapsed again.
    assert 0 < result["mbps"] < 100_000, result["mbps"]


async def test_measure_speed_reports_both_directions(server):
    sample = await measure_speed(local_settings(server))
    assert sample["kind"] == "speed"
    assert sample["ok"] is True
    assert sample["download_bytes"] > 0
    assert sample["upload_bytes"] > 0
    assert sample["download_mbps"] > 0
    assert sample["upload_mbps"] > 0
    assert 0 < sample["upload_mbps"] < 100_000, sample["upload_mbps"]
    assert sample["download_ttfb_ms"] > 0
    assert sample["upload_ttfb_ms"] > 0
    assert sample["streams"] == 1
    assert sample["elapsed_ms"] > 0


async def test_measure_speed_honours_direction_toggles(server):
    sample = await measure_speed(
        local_settings(server, enable_upload=False, enable_download=False)
    )
    assert sample["download_mbps"] is None
    assert sample["upload_mbps"] is None
    assert sample["ok"] is True  # nothing failed, nothing ran

    only_down = await measure_speed(local_settings(server, enable_upload=False))
    assert only_down["download_mbps"] > 0
    assert only_down["upload_mbps"] is None


async def test_measure_speed_with_parallel_streams(server):
    sample = await measure_speed(local_settings(server, streams=3))
    assert sample["streams"] == 3
    assert sample["download_bytes"] > 0
    assert sample["download_mbps"] > 0


async def test_duration_controls_how_long_the_test_runs(server):
    """The whole point: a test runs for the configured time, not a byte count."""
    settings = local_settings(server, download_seconds=2.0, upload_seconds=0)
    started = time.perf_counter()
    sample = await measure_speed(settings, tier="sustained")
    elapsed = time.perf_counter() - started
    assert 1.8 <= elapsed <= 4.0, elapsed
    assert sample["download_bytes"] > 0
    assert sample["upload_mbps"] is None
    # One bucket per completed second of transfer. The trailing partial second
    # is dropped, so a 2s test yields one or two.
    assert 1 <= len(sample["download_intervals"]) <= 2, sample["download_intervals"]


async def test_zero_seconds_disables_that_direction(server):
    sample = await measure_speed(local_settings(server, download_seconds=0, upload_seconds=0))
    assert sample["download_mbps"] is None
    assert sample["upload_mbps"] is None
    assert sample["ok"] is True


async def test_a_throttling_link_is_detected_within_one_test(server):
    """Fast for the first seconds, then a fraction of the speed.

    A fixed-byte test finished before the throttle started and reported the fast
    phase as the answer; a duration-based one sees the tail.
    """
    STATE["throttle_started"] = None
    settings = local_settings(
        server,
        download_url=f"{server}/__down-throttle",
        download_seconds=6.5,
        upload_seconds=0,
        download_chunk_bytes=4 * MB,
    )
    sample = await measure_speed(settings, tier="sustained")

    intervals = sample["download_intervals"]
    assert len(intervals) >= 5, intervals
    rates = [point["mbps"] for point in intervals]
    assert max(rates) > 10 * max(1.0, rates[-1]), rates
    assert sample["download_decay_pct"] is not None
    assert sample["download_decay_pct"] < -50, sample["download_decay_pct"]


async def test_byte_cap_stops_a_test_early(server):
    STATE["throttle_started"] = None
    settings = local_settings(
        server,
        download_url=f"{server}/__down-throttle",
        download_seconds=30.0,
        upload_seconds=0,
        download_chunk_bytes=512 * 1024,
        max_test_bytes=2 * MB,
    )
    started = time.perf_counter()
    sample = await measure_speed(settings, tier="sustained")
    elapsed = time.perf_counter() - started
    assert elapsed < 10, elapsed
    assert sample["capped"] is True
    assert "cap" in (sample["error"] or "")
    assert sample["download_bytes"] <= 3 * MB


async def test_measure_speed_survives_server_errors(server):
    settings = local_settings(server, download_url=f"{server}/does-not-exist")
    sample = await measure_speed(settings, tier="sustained")
    assert sample["ok"] is False
    assert sample["error"] is not None
    assert sample["download_mbps"] == 0
    assert sample["throttled"] is False


async def test_rate_limit_is_reported_as_throttling_not_a_fault(server):
    """A 429 from the provider must not look like a connectivity problem."""
    settings = local_settings(server, download_url=f"{server}/__down-429")
    result = await _run_direction(settings, "download", 1)
    assert result["throttled"] == "120"          # Retry-After is captured
    assert result["errors"] == []                # not an error
    assert result["bytes"] == 0

    sample = await measure_speed(settings, tier="sustained")
    assert sample["throttled"] is True
    assert "429" in (sample["error"] or "")
    assert sample["ok"] is False                 # the measurement did fail
    # ...but it is explicitly flagged, so the UI can say "rate limited".
    assert sample["throttled"] is True


async def test_rate_limit_exception_carries_retry_after():
    limited = RateLimited("42")
    assert limited.retry_after == "42"
    assert "429" in str(limited)
    assert "42" in str(limited)
    assert RateLimited().retry_after is None
