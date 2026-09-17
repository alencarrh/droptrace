# Roadmap — what to add, and in what order

Planning only: nothing here is built. Effort is rough and relative to this
project's own scale (**S** ≈ an afternoon, **M** ≈ a day or two, **L** ≈ a week of
careful work with tests and docs).

Every item is judged by one question: **does it make a drop easier to prove, or
easier to explain?** Fixing the connection is explicitly *not* the goal, except
where the tool can show the problem is on our side of the wall (router, cable,
Wi-Fi) — see [Decisions taken](#decisions-taken).

**Delivery is digital**: screenshots (Print Screen / Win+Shift+S) sent through a
support portal, chat or email — never paper. So this is not about print
stylesheets or pagination. It is about producing **a self-contained, legible image
that stands on its own when it arrives without you in the room**. The dark theme is
fine for that; what matters is framing, what is inside the frame, and type size
once the image is scaled down inside someone's ticket viewer.

---

## Decisions taken (answered 2026-09-15)

| Question | Answer | What it changes |
|---|---|---|
| Who reads it? | **You** read it; you send **digital screenshots/evidence** to the ISP, mediating the data yourself | The unit of delivery is an **image**, not a document. A dedicated capture-friendly view matters far more than a print stylesheet |
| Alerts? | Not understood; the goal is evidence to question the ISP | Dropped to optional and explained below |
| Second vantage point? | A **phone on Wi-Fi**; the PC and MacBook are usually on **Ethernet**; the MacBook is always on a **VPN** | The phone becomes the Wi-Fi discriminator, and the PC's evidence has **no Wi-Fi in its path** — which makes it stronger, not weaker |
| Prove it, or fix it? | **Prove it.** A fix only matters if the fault is ours | Evidence first; "improve it" items (bufferbloat, IPv6, MTU) deferred, while the discriminating ones (DNS, loss, tracing) stay high |

Two facts that shape the rest:

1. **Ethernet is the better evidence.** No Wi-Fi signal, roaming or channel
   congestion is in the path, so the ISP cannot answer "your Wi-Fi is bad".
2. **A VPN moves the path**, so the MacBook cannot measure the line while it is up.
   It is still useful for probing the router with the VPN off.

---

## Batch 1 — Evidence you can capture and send

**Delivered so far: the global statistics page and its one-screen summary**
(`/stats`, `?summary=1`). It reads both storage tiers, so a 30-day window is not
blind to the folded part, and its per-target table is the loss metric that 2.2
asked for.

Nothing here needs new measuring; every number is already in the database. What is
missing is a view worth screenshotting.

### 1.1 An evidence view built for the screenshot — **S/M**

A dedicated layout (a mode, not a page you have to find) that contains everything a
reader needs and **nothing of the dashboard**: no toolbar, no range chips, no
settings, no "Pause"/"Reset". Concretely:

- **Designed to fit one screen** at common sizes (1280×800, 1600×1000, 1920×1080),
  so a single screenshot is the whole story with no scrolling and no stitching. A
  "fit to viewport" toggle for the summary.
- **Larger type and fewer panels** than the dashboard: the dashboard's 10px chart
  labels become unreadable the moment the image is scaled into a ticket viewer.
- **Its own header baked into the image**: "DropTrace — connectivity report",
  the window covered, generated-at timestamp, machine name, and the method line.
  An image arriving in a support queue must identify itself.
- **Light or dark, your choice**, default light: a dark image next to white email
  text reads as a screenshot of "some tool", while light reads as a report. It also
  prints fine if a technician does print it.
- The dashboard stays dark; this is a separate mode.

### 1.2 One-screen summary — **M**

The content of that view, computed from the data:

- **The three strongest facts first**, in plain language. For the 19:04 event:
  1. *34 disconnections between 19:04 and 19:07, 53 seconds in total.*
  2. *At each one, five independent networks (Cloudflare, Google, Microsoft,
     Amazon, Wikimedia) stopped answering simultaneously.*
  3. *The router answered throughout, and DNS kept working — the failure was
     upstream of the home network.*
- **The incident list, filtered by minimum duration** (suggestion: 2s, with the
  count of shorter ones stated) so 34 rows of 1-second blips do not shrink the
  three that matter into unreadability.
- The timeline strip and the latency chart with drops shaded.
- **A baseline comparison**: "p95 latency is normally 20ms; during these drops it
  was 400ms" — far more persuasive than an absolute number.
- **A "how this was measured" box**: TCP handshakes to fixed IPs plus five
  independent networks, the router and a DNS query every 5 seconds; failures
  recorded individually with timestamps; hourly statistics with raw probes kept
  seven days. ISPs dismiss consumer tools that cannot say what they measured; one
  paragraph removes that opening.
- **A "what this does not prove" line** (consumer measurement, TCP not ICMP, one
  vantage point) — voluntary honesty makes the rest more credible.

### 1.3 Capture one incident — **S/M**

The same treatment for a single drop: probes second by second, every failed entry
with timestamp and error, boundary uncertainty, which networks were dark, latency
before and after. Reachable by clicking a row in Recorded outages — which today is
a summary with no browsable evidence behind it — and sized so one screenshot covers
it.

### 1.4 Plain-language narration — **S**

Generate the summary sentences from the measured numbers rather than writing them
by hand: *"At 19:04:02 the connection dropped for 4.1s, then flapped for 2m48s.
58% of connection attempts were dropped; the handshakes that succeeded took a
normal 19ms, so packets were being discarded rather than the line being slow. The
router stayed up throughout."* Templates plus measured values, nothing inferred.

### 1.5 Provenance and redaction — **S**

- **Provenance** is baked into the image (1.1), because you will not be there to
  explain it.
- **Redaction**: the current dashboard shows your LAN addresses (`192.168.1.1`,
  `10.255.255.254`) and the machine name. A share mode should be able to mask them
  (`router (masked)`), because a screenshot is the easiest way to leak your own
  network layout to a stranger, and none of it is needed to make the case.

### 1.6 Download the image, or just screenshot it? — **decision, then S or M**

- **S**: the view is exactly sized and the user presses Win+Shift+S. Zero code, and
  it is what you already do.
- **M**: a "Download image" button that renders the view to a PNG in the browser.
  No external library is allowed in this project, so it means composing the image
  ourselves (drawing the already-canvas charts plus text and the table onto a
  canvas, or serialising the view to SVG via `foreignObject`) — real work for a
  convenience the OS already provides.

My recommendation: **S first**, and only build M if taking screenshots turns out to
be the annoying part. Also worth offering a **single-file HTML** export next to the
PNG: support portals that strip images still accept a file, and its text stays
selectable and searchable.

### 1.7 Exports completed — **S**

`export.csv` and `incidents.csv` exist; add `failures.csv` (every failed probe) and
`stats.csv` (the hourly rollups), plus a per-window bundle. Digital delivery means
attachments are cheap, and a CSV next to the screenshot answers "can you send the
raw data?" without another round trip.

**Batch 1 is the whole of "prove it".**

---

## Batch 2 — Is it my side or theirs

Still evidence, but the kind that answers the second question an ISP asks.

### 2.1 DNS depth — **delivered**

Built: every resolver in `--dns-servers` (plus the machine's own) is asked the same
cached name every round *and* a random, never-before-seen name once a minute. The
random label cannot be answered from a cache, so its authoritative negative answer
proves the query reached the servers — the half a cached name never shows, and the
reason "DNS is up" could be true while a browser was dying. The per-target table
puts the resolvers side by side over the same seconds, and an uncached failure is
recorded as `uncached: DNS timeout after 1s` with its own rate. Only the machine's
own resolver decides the verdict; the comparison resolvers are evidence. Original
plan follows.

Compare several resolvers side by side (router, 1.1.1.1, 8.8.8.8), use a **random
subdomain per probe** so a cache cannot mask a dead upstream, separate query setup
from answer, and count SERVFAIL/timeouts. This turns "the internet drops" into "the
resolver stalls for four seconds every ten minutes" — a specific complaint DropTrace
already has partial evidence for.

### 2.2 Loss as a first-class metric — **delivered**

Built: a round that finds nothing answering fires a counted **burst** of
handshakes (`--burst-handshakes`, default 20, spread over `--burst-window`
seconds), so the loss during a drop is a measured rate instead of an inference
from failure counts — "70% of the handshakes did not arrive while the line was
nominally up". Bursts are their own sample kind, so they cannot skew the round
probes' latency or failure counts, and they show up on the statistics page as
the *Handshake loss at the drop* panel plus **Loss %** and **Worst hour** columns
in the per-target table (an average over a week hides the hour that lost a third
of its handshakes). `droptrace burst` runs one on demand, and a burst that loses
at least half its handshakes also earns a path trace, because the partial
blackouts it catches never open an incident. Original plan follows.

The 58% TCP handshake loss measured during your 19:04 event is currently buried in
failure counts. Loss is *the* number for "it stops for five seconds": the line is
up, the packets are not arriving. Show it per target per hour, and add a
**suspicion burst** (20 handshakes in a second) when a drop is detected so the
figure is defensible rather than inferred.

### 2.3 Path tracing at the moment of a drop — **delivered**

Built: `droptrace/trace.py` traces once when an incident opens and once an hour
while healthy, as background tasks with a hard cap, and stores the hop list with
the incident. `tracepath` is used where it exists (unprivileged UDP), `tracert`
under WSL — which traces from the **host**, the machine whose connection is being
complained about — and `traceroute` as the Linux fallback; names are never
resolved, since DNS is often the broken thing. The `/stats` page pairs the newest
drop trace with the newest healthy one, and the hop-by-hop table names where it
stopped. Traces are never pruned: a hop list summarised away is worthless.
Original plan follows.

The strongest single piece of evidence is *where* it dies. `tracepath` (UDP, works
unprivileged) fired once per detected drop, stored with its hop list, compared
against a healthy baseline: "traffic stops after hop 4, at 100.64.0.1, the
provider's edge". Must be time-capped and rate-limited, since tracing during an
outage can hang. This is the item that most changes the conversation, because it
names equipment rather than describing symptoms.

### 2.4 The phone as a second vantage point — **delivered**

Built: `--bind 0.0.0.0` plus a browser agent at `/agent` (no install on the phone
or the laptop) and a Python agent (`droptrace agent`) for a machine that can run
it. Everything is stored centrally, remote probes never affect this machine's
verdict, and `/stats` compares devices hour by hour. The dashboard hands out the
ready-made links and token. Original plan follows.


The phone on Wi-Fi against the PC on Ethernet is exactly the pair needed to split
the path in two. No app: a `/probe` page that runs in the phone's browser, fetches
a tiny resource from two or three endpoints every few seconds with a cache-buster,
and posts results with a device label; the server stores them as a separate source.

The question it settles: *at 19:04, did the phone's Wi-Fi lose connectivity too, or
only the cabled machine?* Both dark = the ISP; phone dark and PC fine = the Wi-Fi
or the phone. Caveats to state honestly: mobile browsers throttle background
timers (foreground tab, screen on) and Wi-Fi power saving inflates latency, so it
is a coarse yes/no signal rather than a measurement.

---

## Batch 3 — Optional, or deferred by your answers

| Item | Status | Why |
|---|---|---|
| **Alerts** | Optional | Explained below. Nothing about proving a drop needs them |
| **Global statistics page** (`/stats`) | **Done** | Answers "is it getting worse?", which strengthens a complaint but is not the complaint. Cheap when it comes: hourly rollups already hold probes/ok/fail, latency min/avg/max, jitter, loss, round counts and attribution |
| **Latency under load / bufferbloat** | Deferred | Fix-oriented. Still worth doing because a 20ms→400ms jump during a call is a defect, not a drop |
| **IPv6 checks** | Deferred | Fix-oriented; cheap to add (`2606:4700:4700::1111`) since "v4 fine, v6 broken" causes stalling |
| **MTU / PMTU black hole** | Deferred | Fix-oriented |
| **Wi-Fi metadata (RSSI, roaming)** | Deferred | There is no Wi-Fi in the measured path. Only relevant if you start measuring over Wi-Fi, and 2.4 gets you most of the way first |
| **Second speed-test provider** | Deferred | Only matters if Cloudflare itself is suspect; the corroboration pool already covers "one provider is the problem" for reachability |
| **Print stylesheet / PDF pagination** | **Not needed** | Delivery is digital: images and files, never paper |

### Alerts, explained

An alert is a message sent the moment a drop is detected, so you do not have to
watch the page to know it happened. Two flavours: **local** (a Windows
notification, using the host path the launcher already uses — no accounts, nothing
leaves the machine) or **remote** (a message to Discord/Telegram/ntfy so it reaches
your phone). Both would have a minimum-duration threshold, a cooldown, and a "back
up" message. Since the evidence does not depend on being told in real time, this is
a convenience rather than a requirement.

---

## Not planned

| Idea | Why not |
|---|---|
| ICMP ping | Needs `CAP_NET_RAW`; a TCP handshake measures the same path and works everywhere |
| Packet capture / PCAP archive | Enormous, and answering "was it the ISP" does not need packet-level detail |
| Cloud sync / accounts | Turns a local evidence tool into a service with a privacy surface. Files are enough |
| Mobile app | A responsive page plus the phone vantage point (2.4) covers it |
| Continuous full-rate speed tests | Costs ~700 MB a test and saturates the link being measured |
| Formal regulator-grade dossier | You mediate the data yourself; a legible image plus CSV attachments is the right size |

---

## Suggested order, in one line

**Evidence view → one-screen summary → per-incident capture → narration →
provenance/redaction → exports** (that is "prove it"), then **DNS depth, loss,
tracing, phone vantage point** (that is "whose fault"), then optionally the stats
page and alerts, and only then the fix-oriented items.

## Open items

- **Screenshot the view, or a "Download image" button?** My recommendation is the
  view first (S); the button is real work for something the OS already does.
- **Minimum incident duration** in the summary by default (suggestion: 2s, with the
  count of shorter ones stated).
- Whether the **phone vantage point** is realistic often enough to build; if not,
  2.1–2.3 matter more.
- **Config persistence** is still missing (live settings revert on restart) — a
  small piece of hygiene that belongs in whichever batch comes next.
