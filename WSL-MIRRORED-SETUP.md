# WSL mirrored networking — step by step

Copy-paste guide for the Windows side. Everything here was checked against **this**
machine on 2026-09-15.

| | |
|---|---|
| Windows | 11 25H2 (build 26200) |
| WSL | 2.3.26.0 (mirrored mode needs 2.0+, Windows 11 22H2+) |
| Distro | Ubuntu (default) |
| Router (LAN gateway) | **192.168.1.1** |
| Active link | **Ethernet 192.168.1.50** (Wi-Fi adapter has 192.168.1.51) |
| Network profile | **Alencar / Ethernet / Public** |
| Dashboard port | 8777 |

## Status on this machine (applied 2026-09-15)

| Check | Result |
|---|---|
| `wslinfo --networking-mode` | **mirrored** |
| `ip route show default` | **`default via 192.168.1.1 dev eth1 metric 25`** (was `172.20.0.1`) |
| Windows → `http://localhost:8777/` | works |
| DropTrace router target | role `lan`, `192.168.1.1:53/80/443`, answering |
| Data integrity after the switch | 0 ongoing incidents, cadence 5s, no double-sampling; the ~2 min gap during the shutdown is recorded honestly as "no probes" |
| Phone / LAN access | **not needed, deliberately not pursued** — see [Step 8](#step-8--verify-from-windows-optional) |

The part that mattered is done: the distro now sees the **real** router, so DropTrace
can tell a router problem from an ISP problem. Everything below the fold about
reaching the dashboard from a phone is optional and can stay undone.

## What this changes, and why it is worth the restart

1. **The dashboard becomes reachable from your phone.** In WSL2's default NAT mode
   the distro sits behind a private `172.20.x.x` address your phone cannot route
   to, so a port opened inside WSL is invisible to the LAN.
2. **DropTrace can prove where a drop happened.** In NAT mode the only "local"
   address it can reach is WSL's own DNS proxy `10.255.255.254`, which sits on
   *loopback* and answers even with the cable pulled — so a drop could not be
   attributed to the ISP with any honesty. Mirrored mode gives the distro the
   real interfaces and routes, so the router is directly probeable.

> **`wsl --shutdown` stops everything in WSL**: containers, dev servers, the
> DropTrace dashboard, and any terminal session. Nothing is lost that matters: the
> database is SQLite in WAL mode with an hourly checkpoint, and there is already a
> copy at `data/droptrace.db.backup-20260915-193327` (that copy predates the
> move of the default database to `~/.local/share/droptrace/`).

**Which steps need Administrator?** Only the firewall step (3) and the verify
step (8). `wsl --shutdown` itself does not. Running everything in one admin window
is fine — that is how the commands below are written.

---

## Step 0 — Confirm you are in an Administrator PowerShell

```powershell
([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
```

Expect `True`. If it says `False`: Start → type `PowerShell` → right-click →
**Run as administrator**.

Confirm the WSL version supports mirrored mode:

```powershell
wsl --version
```

Expect `WSL version: 2.3.26.0` or newer. If it is below `2.0.0.0`, stop here and
use **Option B (port forward)** in `LOCAL-NETWORK.md` instead.

See the current (NAT) state, so you can tell the change worked:

```powershell
wsl -d Ubuntu -e sh -c "ip route show default; echo ---; grep nameserver /etc/resolv.conf"
```

Expect right now — this is what will change:

```
default via 172.20.0.1 dev eth0 proto kernel   <- WSL's virtual gateway (useless for evidence)
---
nameserver 10.255.255.254                        <- a proxy on loopback
```

After the switch it should read `default via 192.168.1.1`.

---

## Step 1 — Verify the `.wslconfig` file

It was already written; this step just confirms it (and recreates it if you ever
lose it):

```powershell
Get-Content "$env:USERPROFILE\.wslconfig"
```

Expect exactly:

```ini
[wsl2]
networkingMode=mirrored
firewall=false
```

If the file is missing or wrong, write it:

```powershell
@"
[wsl2]
networkingMode=mirrored
firewall=false
"@ | Set-Content -Path "$env:USERPROFILE\.wslconfig" -Encoding ascii

Get-Content "$env:USERPROFILE\.wslconfig"
```

`-Encoding ascii` matters: WSL does not accept a UTF-8 BOM. Do not add other
`[wsl2]` keys unless you know what they do — this file replaces the defaults.

---

## Step 2 — Check the network profile (and make it Private)

Windows Firewall treats a **Public** network far more strictly than a **Private**
one, and right now this machine's LAN is classified **Public**:

```powershell
Get-NetConnectionProfile | Format-Table Name,InterfaceAlias,NetworkCategory -AutoSize
```

Expect (before):

```
Name    InterfaceAlias NetworkCategory
----    -------------- ---------------
Alencar Ethernet                Public
```

Tell Windows this is a home network (needs Administrator):

```powershell
Set-NetConnectionProfile -Name "Alencar" -NetworkCategory Private
Get-NetConnectionProfile | Format-Table Name,InterfaceAlias,NetworkCategory -AutoSize
```

Expect `Private` afterwards.

*If you would rather leave it Public*, skip this step and use `-Profile Public`
(or `-Profile Any`) in Step 3 instead of `-Profile Private`.

---

## Step 3 — Allow port 8777 through Windows Firewall

With `firewall=false` in `.wslconfig`, the **Hyper-V** firewall stops filtering
WSL — but **Windows Firewall still filters inbound traffic on your real
interfaces**, and there is currently no rule for 8777:

```powershell
New-NetFirewallRule `
  -DisplayName "DropTrace 8777" `
  -Direction Inbound `
  -Protocol TCP `
  -LocalPort 8777 `
  -Action Allow `
  -Profile Private
```

Verify:

```powershell
Get-NetFirewallRule -DisplayName "DropTrace 8777" |
  Format-Table DisplayName,Enabled,Direction,Action,Profile -AutoSize
```

Expect `True  Inbound  Allow  Private`.

<details>
<summary>Alternative: keep the Hyper-V firewall enabled</summary>

If you would rather not set `firewall=false`, delete that line from `.wslconfig`
and add a rule to the Hyper-V firewall instead (WSL's VM creator ID is fixed):

```powershell
New-NetFirewallHyperVRule `
  -Name "DropTrace 8777" `
  -DisplayName "DropTrace dashboard" `
  -Direction Inbound `
  -VMCreatorId "{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}" `
  -Protocol TCP `
  -LocalPorts 8777
```

This path is not verified on this machine — the tested configuration is
`firewall=false` plus the Windows rule above.

</details>

---

## Step 4 — Stop the dashboard (optional but tidy)

If it is running in a window (`start.bat` or a terminal), press **Ctrl+C** there.

If you have lost the window, find and stop the process holding the port. `fuser`
is the precise tool (no pattern matching, so nothing else on the machine is
touched) — this is verified working on this machine:

```powershell
wsl -d Ubuntu -e fuser 8777/tcp        # prints the pid holding the port
wsl -d Ubuntu -e kill -9 2638          # then use the number it printed
```

Expect the first command to print a pid and `8777/tcp:` (for example
` 26388777/tcp:` — the label is appended right after the pid, so read the digits
before `/tcp:`). Do not use `<(angle brackets)>` placeholders in PowerShell: `<`
is a reserved character there and the line fails to parse.

**SIGTERM does not stop it quickly, and that is deliberate.** DropTrace finishes the
probe in flight before stopping — a sustained speed test needs ~20s, and the wait
is capped at two minutes — so `fuser -k` can look like it did nothing for a while
(observed: still listening after 24s, and `fuser -k -KILL` did not take either).
`kill -9` on the pid is immediate and safe: SQLite is in WAL mode, and an incident
left open is closed on the next start.

You can also skip this: `wsl --shutdown` in the next step stops it anyway, and
DropTrace's single-instance lock stops a second sampler from starting by accident.

---

## Step 5 — Restart WSL

This is the step that applies `.wslconfig`:

```powershell
wsl --shutdown
Start-Sleep -Seconds 10
wsl --list --running
```

Expect:

```
There are no distributions running.
```

If a distro is still listed, wait a few more seconds and run
`wsl --list --running` again. Do not skip the pause — WSL must fully stop before
it re-reads the config.

---

## Step 6 — Start the dashboard again

**Option A — normal start** (dashboard on `127.0.0.1` only, fine if you only ever
look at it from this PC): double-click `start.bat`, or:

```powershell
wsl.exe -d Ubuntu --cd /mnt/f/projetos/stakfin/opencode-harness/contexts/alrohe/droptrace -e python3 -m droptrace serve --web-port 8777
```

If it dies with `[Errno 98] error while attempting to bind on address
('0.0.0.0', 8777): address already in use` **right after the switch**, that is the
mirrored stack still settling — the port is free seconds later, so just run it
again. (Two DropTrace instances are prevented separately: the second one exits with
*"Another DropTrace is already sampling"* rather than fighting over the port.)

**Option B — also reachable from your phone.** The launcher does *not* pass
`--bind`, so a plain `start.bat` stays loopback-only. Run it explicitly:

```powershell
wsl.exe -d Ubuntu --cd /mnt/f/projetos/stakfin/opencode-harness/contexts/alrohe/droptrace -e python3 -m droptrace serve --web-port 8777 --bind 0.0.0.0
```

To make `start.bat` do that permanently, edit this line in
`F:\projetos\stakfin\opencode-harness\contexts\alrohe\droptrace\start.bat`:

```bat
wsl.exe -d %DISTRO% --cd "%WSLDIR%" -e python3 -m droptrace serve --web-port %PORT%
```

and add the flag:

```bat
wsl.exe -d %DISTRO% --cd "%WSLDIR%" -e python3 -m droptrace serve --web-port %PORT% --bind 0.0.0.0
```

> **Read this before binding to the LAN:** the dashboard has **no password**. With
> `--bind 0.0.0.0` anyone on your network can read it and change the cadence,
> start speed tests (your data) and delete history. Keep it off guest networks;
> the firewall rule above is scoped to the Private profile for that reason.

Leave that window open while monitoring — it prints the log, and closing it stops
sampling.

---

## Step 7 — Verify from inside WSL

```powershell
wsl -d Ubuntu -e sh -c "ip -4 route show default; echo ---; grep nameserver /etc/resolv.conf; echo ---; ss -ltnp | grep 8777"
```

Expect:

```
default via 192.168.1.1 dev eth0 proto kernel   <- the real router now
---
nameserver 10.255.255.254                       <- may stay like this: DNS tunnelling, harmless
---
LISTEN 0  2048  0.0.0.0:8777  0.0.0.0:*    users:(("python3",pid=...))   <- 0.0.0.0, not 127.0.0.1
```

If the first line still says `172.20.x.x`, `.wslconfig` did not take effect —
repeat Step 5 and check Step 1 for a BOM.

Now confirm DropTrace found the router and can reach it:

```powershell
Invoke-RestMethod http://127.0.0.1:8777/api/targets |
  Select-Object -ExpandProperty targets |
  Format-Table name,role,address,success,failures,disabled -AutoSize
```

Expect a row with role **`lan`** and address `192.168.1.1:53/80/443`, with
`success` climbing and `disabled = False`:

```
name       role      address            success failures disabled
----       ----      -------            ------- -------- --------
resolver   local     10.255.255.254:53       12        0    False
gateway    lan       192.168.1.1:53/80/443   12        0    False
cloudflare internet  1.1.1.1:443             12        0    False
google     internet  8.8.8.8:443             12        0    False
dns        dns       one.one.one.one...     12        0    False
```

The `lan` row is the point of all this: only the router can say "the LAN was
fine", and only then is a drop attributed to the ISP.

---

## Step 8 — Verify from Windows (optional)

*Only needed if you want the dashboard on your phone. Skip to Step 10 otherwise.*

This checks the LAN path, so it needs **Step 6 Option B** (`--bind 0.0.0.0`).

> **A failure here does not mean phone access is broken.** In mirrored mode both
> Windows and the distro hold the same address (`192.168.1.50`), and this machine
> measured exactly that asymmetry: from Windows, `127.0.0.1:8777` connects and
> `192.168.1.50:8777` does **not**. Traffic the host sends to its own address is
> delivered locally instead of being handed to the mirrored listener, which is the
> mechanism an external device does use. Only the phone can settle it —
> `Test-NetConnection` from the host cannot.

```powershell
Test-NetConnection -ComputerName 192.168.1.50 -Port 8777
```

Expect `TcpTestSucceeded : True`. Then:

```powershell
Invoke-RestMethod http://192.168.1.50:8777/api/health
```

Expect `ok : True`, `running : True`, and a `samples` count.

If `TcpTestSucceeded` is `False`, check in this order:
`ss -ltnp | grep 8777` shows `0.0.0.0:8777` (Step 7) → the firewall rule profile
matches `Get-NetConnectionProfile` (Steps 2–3) → try the other address
(`192.168.1.51`).

---

## Step 9 — Verify from your phone (optional)

On the phone, connected to the same Wi-Fi as this network:

**http://192.168.1.50:8777/**

If it does not load, try **http://192.168.1.51:8777/** (the Wi-Fi address — only
if that adapter is up; right now the active link is Ethernet, `192.168.1.50`).
A guest SSID with client isolation will never work, and neither will a phone on
mobile data.

---

## Step 10 — Confirm the attribution actually improved

Open the dashboard and check two places:

* **Targets** panel: the `gateway` row now shows role **`router`**, ~1ms, 100%.
* **Recorded outages → Verdict**: a drop is only called **"ISP or upstream down
  (router answered)"** when the router answered during it. Otherwise it reads
  **"Internet unreachable (no router evidence)"** — which is the honest answer,
  and the one the tool could not give before.

The 34 rows from 19:04–19:06 were re-derived this way (they were previously —
and wrongly — labelled `isp`), see `scripts/reattribute_incidents.py`.

---

## Rolling it back

```powershell
Remove-Item "$env:USERPROFILE\.wslconfig" -Force
Remove-NetFirewallRule -DisplayName "DropTrace 8777"
wsl --shutdown
Start-Sleep -Seconds 10
Get-Content "$env:USERPROFILE\.wslconfig" -ErrorAction SilentlyContinue   # expect: nothing
```

To go back to NAT but keep the file, edit out the two `[wsl2]` lines instead, and
set the network back if you changed it:

```powershell
Set-NetConnectionProfile -Name "Alencar" -NetworkCategory Public
```

Back in NAT mode the phone port stops working (use
[LOCAL-NETWORK.md](LOCAL-NETWORK.md) Option B). DropTrace keeps working: it reads
the router from the host's own route table, so the `lan` target still answers.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `wsl --version` is below 2.0.0.0, or Windows is older than 11 22H2 | Mirrored networking is unavailable. Use the port-forward route in `LOCAL-NETWORK.md` (Option B). |
| After the restart Docker Desktop or containers behave oddly | Mirroring changes how WSL sees interfaces. Restart Docker Desktop first; if it persists, revert (see *Rolling it back*) and use Option B. |
| A VPN is running | VPNs and mirrored mode frequently conflict. Disconnect the VPN, or revert. |
| `default via 172.20.x.x` still shows after the restart | `.wslconfig` was not read: check the filename is exactly `.wslconfig` in `C:\Users\<you>\`, that it has no BOM (Step 1), then `wsl --shutdown` and wait 10s. |
| `[Errno 98] address already in use` right after the switch | The mirrored stack was still settling. The port frees within seconds — run the start command again. |
| `fuser -k 8777/tcp` seems to do nothing | DropTrace finishes the probe in flight before stopping (up to two minutes). Find the pid with `fuser 8777/tcp` and pass that number to `kill -9`. |
| Host cannot reach the dashboard on its own LAN IP | Expected in mirrored mode: both sides hold the same address and host-to-own-IP traffic is delivered locally. Use `localhost` on this PC, and test the LAN path from the phone. |
| Second start exits with *"Another DropTrace is already sampling"* | The single-instance lock working as intended: an older server still holds the database. Stop it (above) or start with `--db` pointing somewhere else. |
| Phone cannot load the page, Windows can | `--bind 0.0.0.0` is missing (`ss -ltnp` shows `127.0.0.1:8777`), or the firewall rule's profile does not match the active network. |
| Windows browser on `http://localhost:8777/` stops working | It normally keeps working; if not, use `http://192.168.1.50:8777/`. |
| `Another DropTrace is already sampling <database>` | An older instance survived. `wsl --shutdown` clears it, or start with `--db` pointing at another file. Never run two against one database: they halve the round spacing and double every count. |
| `make test` / `make serve` complain about missing dependencies | This Ubuntu has no `python3-venv` (`ensurepip` is missing), so `make install` cannot build a venv. The Makefile falls back to the system interpreter, which already has the dependencies, so `make test` and `make serve` work as-is. To get a venv: `sudo apt install python3-venv && make install`. |
| `/etc/resolv.conf` still says `10.255.255.254` | Expected: WSL's DNS tunnelling. Harmless, and irrelevant to attribution — the router target is what proves the LAN. |
| Phone page loads but charts are empty | The range selector is set to a short window, or sampling is paused. Check the footer says *live* and the pill says *watching*. |

---

## Checklist

```
[ ] 0. Admin PowerShell confirmed, wsl --version >= 2.0
[ ] 1. %USERPROFILE%\.wslconfig has [wsl2] networkingMode=mirrored + firewall=false
[ ] 2. Get-NetConnectionProfile shows Private   (or use -Profile Public in step 3)
[ ] 3. Firewall rule "DropTrace 8777" exists, Enabled=True, Action=Allow
[ ] 4. Old dashboard stopped
[ ] 5. wsl --shutdown, then wsl --list --running is empty
[ ] 6. Dashboard restarted (with --bind 0.0.0.0 if the phone should reach it)
[ ] 7. Inside WSL: default via 192.168.1.1, ss shows 0.0.0.0:8777, /api/targets has role lan
[ ] 8. From Windows: Test-NetConnection 192.168.1.50:8777 -> TcpTestSucceeded True
[ ] 9. From the phone: http://192.168.1.50:8777/ loads
[ ] 10. Targets panel shows the gateway as "router"; verdicts say "router answered"
```
