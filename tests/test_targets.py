"""Target discovery and parsing."""

from __future__ import annotations

import pytest

import droptrace.targets as targets_module
from droptrace.config import Settings
from droptrace.targets import (
    Target,
    build_targets,
    nameservers,
    parse_host_port,
)


def test_parse_host_port_variants():
    assert parse_host_port("1.1.1.1") == ("1.1.1.1", 443)
    assert parse_host_port("1.1.1.1:853") == ("1.1.1.1", 853)
    assert parse_host_port("dns.example.com:53") == ("dns.example.com", 53)
    assert parse_host_port("[::1]:443") == ("::1", 443)
    assert parse_host_port(" 8.8.8.8 ") == ("8.8.8.8", 443)


def test_nameservers_parses_resolv_conf(tmp_path):
    path = tmp_path / "resolv.conf"
    path.write_text(
        "# comment\n"
        "nameserver 10.255.255.254\n"
        "nameserver 1.1.1.1\n"
        "nameserver 10.255.255.254\n"  # duplicate
        "search lan\n"
        "options ndots:1\n"
    )
    assert nameservers(str(path)) == ["10.255.255.254", "1.1.1.1"]
    assert nameservers(str(tmp_path / "missing.conf")) == []


# The corroboration pool from the default settings, in build order. They are
# probed only when a round looks like a drop (plus a slow baseline pass), but
# they are still targets and appear in the table.
CHECKS = ["github-com", "wikipedia-org", "twitch-tv", "youtube-com", "9gag-com"]

# The DNS probes: the machine's own resolver (cached name + uncached name) and
# the same pair against the comparison resolver from the default settings.
DNS_TARGETS = ["dns", "dns-upstream", "dns-cloudflare", "dns-cloudflare-upstream"]


@pytest.fixture
def pinned(monkeypatch):
    """Pin discovery so the tests do not depend on the host's network."""
    monkeypatch.setattr(targets_module, "default_gateway", lambda: "192.168.1.1")
    monkeypatch.setattr(targets_module, "nameservers", lambda *a, **k: ["192.168.1.1"])
    # This suite may well be running under WSL: never read the host's routes.
    monkeypatch.setattr(targets_module, "_windows_default_gateway", lambda: None)


def test_build_targets_defaults(pinned):
    settings = Settings()  # 1.1.1.1:443, 8.8.8.8:443
    built = build_targets(settings)
    names = [t.name for t in built]
    assert names == ["resolver", "gateway", "cloudflare", "google", *CHECKS, *DNS_TARGETS]
    # Only the primary pair decides a round; the rest are corroboration.
    by_name = {t.name: t for t in built}
    assert by_name["cloudflare"].fact_check is False
    assert by_name["google"].fact_check is False
    assert all(by_name[name].fact_check for name in CHECKS)
    assert all(by_name[name].role == "internet" for name in CHECKS)

    roles = {t.name: t.role for t in built}
    assert roles["resolver"] == "local"
    assert roles["gateway"] == "lan"  # the router: LAN evidence
    assert roles["cloudflare"] == "internet"
    assert roles["google"] == "internet"
    assert roles["dns"] == "dns"

    # Auto-detected hops are guesses: they may be filtered entirely.
    guessed = {t.name: t.guessed for t in built}
    assert guessed["gateway"] is True
    assert guessed["resolver"] is True
    assert guessed["cloudflare"] is False

    # The gateway is tried on several ports because routers rarely answer 443.
    gateway = next(t for t in built if t.name == "gateway")
    assert gateway.ports == (53, 80, 443)
    assert gateway.host == "192.168.1.1"


def test_build_targets_can_disable_local_hops(pinned):
    settings = Settings(probe_gateway=False, probe_resolver=False)
    names = [t.name for t in build_targets(settings)]
    # The local-hop probes are off, but the DNS health check still runs against
    # the system resolver (it is a different feature from the resolver probe).
    assert names == ["cloudflare", "google", *CHECKS, *DNS_TARGETS]


def test_build_targets_can_disable_dns_check(pinned):
    settings = Settings(dns_probe_name="")
    names = [t.name for t in build_targets(settings)]
    assert "dns" not in names
    assert names == ["resolver", "gateway", "cloudflare", "google", *CHECKS]


def test_build_targets_custom_public_and_extras(pinned):
    settings = Settings(
        public_targets="9.9.9.9:853,example.com",
        extra_targets="myrouter=10.0.0.1:80, 10.0.0.2:443",
        probe_gateway=False,
    )
    built = build_targets(settings)
    by_name = {t.name: t for t in built}
    assert by_name["quad9"].ports == (853,)
    assert by_name["example-com"].host == "example.com"
    assert by_name["myrouter"].host == "10.0.0.1"
    assert by_name["10-0-0-2"].ports == (443,)
    assert all(
        t.role == "internet"
        for t in built
        if t.name not in ("resolver", "dns", "dns-upstream", "dns-cloudflare",
                          "dns-cloudflare-upstream")
    )


def test_build_targets_deduplicates(pinned):
    settings = Settings(public_targets="1.1.1.1:443,1.1.1.1:443", extra_targets="1.1.1.1:443")
    built = build_targets(settings)
    assert [t.name for t in built].count("cloudflare") == 1


def test_build_targets_no_targets_is_empty():
    settings = Settings(
        public_targets="", extra_targets="", probe_gateway=False,
        probe_resolver=False, dns_probe_name="", fact_check_targets="",
    )
    assert build_targets(settings) == []


def test_the_corroboration_pool_can_be_replaced(pinned):
    settings = Settings(fact_check_targets="example.net:8443")
    checks = [t for t in build_targets(settings) if t.fact_check]
    assert [t.name for t in checks] == ["example-net"]
    assert checks[0].ports == (8443,)


def test_dns_target_queries_the_resolver(pinned):
    settings = Settings(dns_probe_name="example.org")
    dns = next(t for t in build_targets(settings) if t.name == "dns")
    assert dns.kind == "dns"
    assert dns.host == "192.168.1.1"
    assert dns.probe_name == "example.org"
    assert dns.ports == (53,)
    assert "example.org" in dns.describe()


def test_dns_depth_asks_several_resolvers_the_same_question(pinned):
    """The comparison is the point: my resolver vs a public one, same second."""
    settings = Settings(dns_servers="1.1.1.1,9.9.9.9:5353")
    built = {t.name: t for t in build_targets(settings)}
    assert built["dns"].role == "dns" and built["dns"].host == "192.168.1.1"
    assert built["dns-cloudflare"].role == "dns-public"
    assert built["dns-cloudflare"].host == "1.1.1.1"
    assert built["dns-quad9"].ports == (5353,)
    # Only the machine's own resolver feeds the dns verdict.
    assert set(built["dns-cloudflare"].probe_name.split()) == {"one.one.one.one"}
    assert built["dns-cloudflare"].guessed is False


def test_the_uncached_query_is_a_random_name_on_its_own_cadence(pinned):
    """A name nobody has asked for cannot be answered from a cache, so it is
    the only query that shows a resolver whose upstream died. It is also a real
    question to the authoritative servers, so it must not run every round."""
    settings = Settings(dns_cache_bust_interval=90)
    built = {t.name: t for t in build_targets(settings)}
    upstream = built["dns-upstream"]
    assert upstream.expect == "negative"
    assert "{random}" in upstream.probe_name
    assert upstream.probe_name.endswith(".one.one.one.one")
    assert upstream.cadence == 90
    assert built["dns"].cadence == 0, "the cached name still runs every round"
    assert "<random>" in upstream.describe()


def test_dns_depth_can_be_switched_off(pinned):
    assert "dns-cloudflare" not in [t.name for t in build_targets(Settings(dns_servers=""))]
    off = build_targets(Settings(dns_cache_bust_interval=0))
    assert not [t for t in off if t.name.endswith("-upstream")]


def test_a_resolver_is_not_compared_with_itself(pinned):
    """The system resolver is often the very address someone would list."""
    built = [t.name for t in build_targets(Settings(dns_servers="192.168.1.1,192.168.1.1"))]
    assert built.count("dns") == 1
    assert not [name for name in built if name.startswith("dns-192-168-1-1")]


def test_target_describe():
    assert Target("a", "internet", "a", host="1.1.1.1", ports=(443,)).describe() == "1.1.1.1:443"
    assert (
        Target("d", "dns", "d", kind="dns", host="1.1.1.1", probe_name="x.com").describe()
        == "x.com via 1.1.1.1"
    )
