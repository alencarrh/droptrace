"""Path tracing: parsers, command lines, and the caps around a real child."""

from __future__ import annotations

import sys
import time

from droptrace.trace import (
    command_for,
    find_tracer,
    parse_tracepath,
    parse_traceroute,
    parse_tracert,
    summarize,
    trace_path,
)

TRACERT = """
Tracing route to 1.1.1.1 over a maximum of 20 hops

  1    <1 ms    <1 ms    <1 ms  192.168.1.1
  2     5 ms     3 ms     1 ms  100.64.0.1
 16     *        *        *     Request timed out.
 18    10 ms     9 ms     8 ms  1.1.1.1

Trace complete.
"""

TRACEPATH = """
 1?: [LOCALHOST]                      pmtu 1500
 1:  _gateway                          0.512ms
 2:  10.255.255.254                    1.102ms asymm  1
 3:  no reply
 4:  1.1.1.1                           9.876ms reached
     Resume: pmtu 1500 hops 4 back 4
"""

TRACEROUTE = """
traceroute to 1.1.1.1 (1.1.1.1), 20 hops max, 60 byte packets
 1  192.168.1.1 (192.168.1.1)  0.512 ms  0.451 ms  0.400 ms
 2  * * *
 3  1.1.1.1 (1.1.1.1)  9.876 ms  9.800 ms  9.700 ms
"""


def test_tracert_parsing_keeps_the_silent_hop():
    """A router that does not answer is a gap in the path, not a shorter path.

    Measured on this connection: hop 16 of 18 says nothing while 17 and 18
    answer. Dropping that line would make the path look two hops shorter than
    it is.
    """
    hops = parse_tracert(TRACERT)
    assert [h["ttl"] for h in hops] == [1, 2, 16, 18]
    assert hops[0]["host"] == "192.168.1.1"
    assert hops[0]["rtt_ms"] == 1.0  # "<1 ms" is a number, not a hostname
    assert hops[2] == {"ttl": 16, "host": "", "rtt_ms": None, "note": "no reply"}
    assert hops[3]["host"] == "1.1.1.1"


def test_tracepath_parsing():
    hops = parse_tracepath(TRACEPATH)
    assert [h["ttl"] for h in hops] == [1, 2, 3, 4]
    assert hops[1]["host"] == "10.255.255.254" and hops[1]["note"] == "asymm 1"
    assert hops[2]["host"] == "" and hops[2]["note"] == "no reply"
    assert hops[3]["note"] == "reached"
    # The first TTL wins: tracepath repeats a hop when it is still waiting, and
    # the header line ("1?: [LOCALHOST] pmtu 1500") is not a hop answer.
    assert hops[0]["host"] == "localhost"


def test_traceroute_parsing():
    hops = parse_traceroute(TRACEROUTE)
    assert [h["host"] for h in hops] == ["192.168.1.1", "", "1.1.1.1"]
    assert hops[0]["rtt_ms"] == 0.4, "the best of the three probes"


def test_command_lines_never_resolve_names():
    """Names cannot be resolved in the middle of a DNS-shaped outage."""
    assert "-n" in command_for("tracepath", ["tracepath"], "1.1.1.1", 20, 0.7)
    assert "-d" in command_for("tracert", ["tracert"], "1.1.1.1", 20, 0.7)
    assert "-n" in command_for("traceroute", ["traceroute"], "1.1.1.1", 20, 0.7)
    # The per-hop wait is handed over in the unit each tool wants.
    assert "-w" in command_for("tracert", ["tracert"], "1.1.1.1", 20, 0.7)


def test_summary_says_where_it_stopped():
    hops = parse_tracert(TRACERT)
    trace = summarize(hops, "1.1.1.1", 20, "tracert", 17500.0)
    assert trace["reached"] is True and trace["last_hop"] == "1.1.1.1"
    assert trace["hops"] == 4 and trace["answered"] == 3

    cut = summarize(hops[:2], "1.1.1.1", 20, "tracert", 3000.0)
    assert cut["reached"] is False and cut["last_hop"] == "100.64.0.1"
    assert cut["error"] is None, "getting part of the way is a result, not an error"


def test_a_machine_without_a_tracer_says_so(monkeypatch):
    monkeypatch.setattr("droptrace.trace.shutil.which", lambda name: None)
    monkeypatch.setattr("droptrace.trace.Path.exists", lambda self: False)
    assert find_tracer() is None


async def test_trace_path_runs_a_real_child_and_parses_it():
    script = (
        "print('Tracing route to 1.1.1.1 over a maximum of 20 hops');"
        "print('  1    <1 ms    <1 ms    <1 ms  192.168.1.1');"
        "print('  2    10 ms    10 ms    10 ms  1.1.1.1')"
    )
    trace = await trace_path(
        "1.1.1.1", tracer=("tracert", [sys.executable, "-c", script]), timeout=10
    )
    assert trace["reached"] is True
    assert [h["host"] for h in trace["hop_list"]] == ["192.168.1.1", "1.1.1.1"]
    assert trace["tracer"] == "tracert"


async def test_a_trace_that_hangs_is_capped_and_keeps_what_it_printed():
    """Tracing a broken path hangs by nature, so the cap is not optional.

    The hops printed before the cap are the evidence: "it got as far as hop 1"
    still names the last piece of equipment that answered.
    """
    script = (
        "import time;"
        "print('  1    <1 ms    <1 ms    <1 ms  192.168.1.1', flush=True);"
        "time.sleep(30)"
    )
    started = time.perf_counter()
    trace = await trace_path(
        "1.1.1.1", tracer=("tracert", [sys.executable, "-c", script]), timeout=0.6
    )
    elapsed = time.perf_counter() - started
    assert elapsed < 5, "the child must be killed, not waited for"
    assert trace["error"] and "capped" in trace["error"]
    assert [h["host"] for h in trace["hop_list"]] == ["192.168.1.1"]
    assert trace["reached"] is False and trace["last_hop"] == "192.168.1.1"


async def test_a_tracer_that_cannot_start_is_reported_not_raised():
    trace = await trace_path("1.1.1.1", tracer=("tracert", ["/nonexistent/tracer"]), timeout=2)
    assert trace["hops"] == 0 and trace["error"]
    assert trace["reached"] is False
