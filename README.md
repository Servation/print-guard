# print-guard

Watches a Bambu Lab P1S on your LAN while it prints and steps in when something
goes wrong: it **pauses** the print on a confident failure, **stops** it on a
critical one, and always tells you (Windows notification, optional phone push).
A dry-run mode only alerts, so you can trust it before you arm it.

It runs on your PC, talks to the printer directly over the LAN (no cloud), and
needs no changes to Bambu Studio.

```
 P1S  --MQTT (TLS 8883)-->  status, errors, temps, AMS  --> telemetry checks --+
      --camera (TLS 6000)-> JPEG frames --> spaghetti model --(+ Claude)-------+--> guard --> pause / stop / notify
```

## What it catches, and how well

| Problem | How print-guard sees it | Reliability |
|---|---|---|
| Spaghetti, part torn loose into a mess | Obico's open spaghetti model on the chamber camera every 10 s, smoothed over time and compared with this print's own normal look; optional Claude confirmation | Good for visible spaghetti. Small or early messes can be missed; the P1S camera is wide-angle and modest resolution |
| Part knocked over or moved, no mess yet | Optional Claude look every 10 min, comparing now with 10 min ago | Moderate; needs an Anthropic API key |
| Nozzle clog, printing in air | Printer error codes (extruder overload, clog), nozzle-too-cold rule, Claude's "is the part still growing?" comparison, and the spaghetti that usually follows | Partial: the P1S has no extrusion sensor |
| First layer not sticking | Claude first-layer checks at layers 2 and 5, then spaghetti detection | Partial |
| AMS filament runout | The printer pauses itself; print-guard raises a loud alert, warns when the active spool is low, and pauses if the printer somehow keeps going | Reliable. It never stops a print for runout |
| Printer-reported errors (HMS) | Decoded with readable descriptions; serious errors pause if the printer kept printing, fatal heater/sensor errors stop | Reliable |
| Overheating or a cold nozzle | Live temperatures vs targets, with hold times | Reliable (a backup to the firmware's own protection) |
| Stalled print, lost connection, dead camera | Watchdogs | Reliable |

## Printer settings you need to change (your call)

Bambu added "authorization control" to P1-series firmware **01.08.02.00** and
later (current P1S firmware is 01.10.00.00). Since then:

* **Reading status works in any mode.** print-guard can monitor and alert
  without changing anything.
* **Pause and stop from a third-party tool are refused** unless **LAN Only mode
  and Developer Mode** are both on. The printer answers "MQTT command
  verification failed" (HMS 0500_0500_0001_0007). print-guard detects this and
  shows "Control: REFUSED" on the dashboard; it then stays alert-only.

To let print-guard pause and stop, on the printer screen:

1. Settings > WLAN (network page) > **LAN Only Mode**: turn it on, confirm, then
   power-cycle the printer once.
2. On the same page, scroll to **Developer Mode**, read the notice, **Enable**.
3. Note the **Access Code** shown under the IP address on that page (it can
   change when LAN mode is toggled).

What you give up: Bambu Cloud, so the Bambu Handy app and remote viewing stop
working, and firmware updates need the SD card (or switching back temporarily).
Bambu Studio keeps working over the LAN (bind the printer with its access code).
The ntfy phone alerts below replace Handy's notifications.

**Camera:** if `print-guard check` can't get a frame, turn on **LAN Only
Liveview** on the same network page (if your firmware shows it), and keep at
most one other app watching the camera: the P1S serves only two live streams.

print-guard never changes printer settings itself.

## Install

Needs [uv](https://docs.astral.sh/uv/) (already on this PC) and Windows 10/11.

```bash
cd D:\tools\print-guard
uv sync
uv run print-guard download-model
```

`download-model` fetches Obico's ~200 MB model and verifies its SHA-256.

## Set up your printer

```bash
uv run print-guard setup
```

Setup listens for your printer's network announcement (passive, nothing is sent),
then asks for:

* **IP** and **serial**: prefilled from discovery. Otherwise the IP is on the
  printer's network page and the 15-character serial on the sticker at the back
  (P1S serials start with `01P`).
* **Access code** (8 characters, printer network page): typed hidden and stored in
  **Windows Credential Manager**, never in a file. `PRINT_GUARD_ACCESS_CODE`
  overrides it if you prefer an environment variable.
* Optional **ntfy topic** for phone alerts, and an optional **Anthropic API key**
  (also stored in Credential Manager).

It writes `config.toml` (git-ignored) from `config.example.toml`. You can also
copy the example by hand and replace the `XXX` placeholders. Logs, the dashboard
and notifications never show the access code, and show the serial masked.

Then run the read-only check:

```bash
uv run print-guard check
```

It verifies the printer's identity (its Bambu-signed TLS certificate must carry
the configured serial), logs in, reads the status and firmware, reports whether
pause/stop will be accepted (Developer Mode), grabs and scores one camera frame
(saved under `runs/`), and checks the model. It sends no commands.

## Test it safely

Work through these in order. Only the last two touch a real print.

**1. The simulator (no printer involved).** A fake P1S runs on your PC with the
same TLS MQTT and camera protocols; print-guard watches it exactly as it would the
real one, with timescales compressed to a few minutes:

```bash
uv run print-guard simulate spaghetti   # detector sees spaghetti, guard pauses
uv run print-guard simulate runout      # printer pauses itself, guard alerts, sends nothing
uv run print-guard simulate overtemp    # nozzle runs away, guard stops
uv run print-guard simulate hms-fatal   # fatal heatbed error, guard stops
uv run print-guard simulate locked      # Developer Mode off: guard reports the refusal
uv run print-guard simulate stall       # layers stop advancing, guard alerts
uv run print-guard simulate dark        # chamber light off, guard says it's blind
uv run print-guard simulate clean       # a good print: guard does nothing
```

Each ends with PASS or FAIL. Add `--dry-run` to see alert-only behaviour,
`--quiet` to skip desktop notifications, `--ntfy` to test phone pushes.

**2. Notifications.** `uv run print-guard test-notify --level critical` (a
critical alert breaks through Focus Assist and loops the alarm sound while it is
on screen).

**3. Dry run on a scrap print designed to fail.** Slice
`test-prints/spaghetti-bait.stl` (a pillar with a wide roof) with **supports
off**, any PLA, and print it. Around 20 mm the roof prints into thin air and
turns into loose strands, a small, contained failure. While it prints:

```bash
uv run print-guard watch --dry-run
```

Stay near the printer. You should see the vision gauge climb, then a
"[DRY RUN] Would pause the print" alert with a snapshot. Cancel the print on the
printer yourself afterwards. If it never triggers, look at `latest.jpg` in the
run folder and see Tuning below.

**4. Armed on the same scrap print.** Repeat with `--armed` and confirm the
printer really pauses (the dashboard says "pause confirmed by the printer").
Resume or cancel on the printer.

**5. Real prints.** Use dry-run for your first couple of real prints to see how
quiet it is on a good print, then set `mode = "armed"` in `config.toml`.

## Daily use

Double-click **`print-guard.cmd`** when a print starts (or any time before). It
attaches to a print already in progress, follows the next one if you leave it
running, and keeps Windows from sleeping while it watches (the display can still
turn off). Arguments pass through, e.g. `print-guard.cmd --armed` or
`print-guard.cmd --exit-when-done`. From a terminal: `uv run print-guard watch`.

The dashboard shows the link, camera, whether control is available, job progress,
temperatures, AMS spools, the vision gauge (green, warning, failure thirds), the
Claude judge, findings, and a live log of every check. Ctrl+C stops watching.

Each run gets a folder under `runs/`:

* `guard.log`: every check and decision, with timestamps;
* `events.jsonl`: findings, state changes and actions, one JSON object per line;
* `latest.jpg`: the newest scored camera frame with detection boxes;
* `snapshots/`: the frames attached to alerts.

**After the guard pauses a print:** look at the print (snapshot in the alert, or
walk over). Resume on the printer if it's fine: print-guard re-arms after a 5
minute grace period, then spends 5 minutes learning the resumed print's look (so
a dismissed false alarm doesn't fire again, and anything new that goes wrong in
those 10 minutes is only caught if it keeps getting worse). Otherwise stop it on
the printer. Set
`guard.stop_after_paused_minutes` if you want unattended paused prints stopped
(heaters off) after a while.

## Phone alerts (recommended with LAN Only mode)

Install the free **ntfy** app, subscribe to an unguessable topic (setup can make
one), and put it in `notify.ntfy_topic`. Warnings and critical alerts arrive on
your phone with the snapshot attached. Topics on ntfy.sh are public to anyone who
knows the name, so keep it random; you can point `ntfy_server` at your own server.

## Optional: the Claude judge

With an Anthropic API key (setup stores it, or set `ANTHROPIC_API_KEY`), Claude
looks at camera frames to:

* confirm or overrule the local detector before the guard acts (fewer false
  pauses), and judge "warning level" detections the detector alone won't act on;
* check every 10 minutes for failures the detector can't see (part knocked off,
  printing in air), comparing with a frame from 10 minutes earlier;
* check first-layer adhesion at layers 2 and 5.

It uses `claude-opus-5-5` at medium effort with structured JSON verdicts. A check
costs roughly $0.02 to $0.05 (two images); a 15-hour print with the defaults is
about $2 to $4, capped by `judge.max_calls_per_hour`. The dashboard shows the
running total. Periodic findings must repeat before the guard acts. Frames are
sent to Anthropic's API; leave the judge off (`enabled = false`) if you'd rather
not. Try it on any image: `uv run print-guard judge-test path\to\frame.jpg`.

## Tuning

All settings, with defaults and comments, are in `config.example.toml`.

| If... | Change |
|---|---|
| It pauses good prints | Lower `vision.sensitivity` (0.7), enable the Claude judge, or set `vision.roi` to exclude a busy area (purge chute, tubes) |
| It reacts too late | Raise `vision.sensitivity` (1.3 to 1.5) |
| You want alerts, never pauses, from the camera | `vision.action = "notify"` |
| An HMS code is noise for you | Add it to `hms.ignore`, or map it in `hms.overrides` |
| Thermal rules are too strict | `thermal.*_c` and `*_seconds` |

`uv run print-guard snapshot` grabs a live frame, scores it and saves it with the
detection boxes and ROI drawn, which is the quickest way to tune `vision.roi`.
`snapshot --image file.jpg` scores any picture.

**Busy prints (dense tree supports).** A healthy print on a clear plate scores
under 0.3, but the detector also reacts to rows of thin support trunks: a healthy
P1S print with organic tree supports measured 2.9 +/- 0.7, where spaghetti drawn
over the same frames added about +1.5 to +3.5. So the guard judges each print
against its own normal look (`vision.baseline_minutes`) with a margin for how
flickery the scene is (`vision.noise_margin_sigmas`):

* attaching to a print already under way, or resuming one, calibrates on the
  current look for 5 minutes (a failure already in progress would look normal);
* a fresh print starts from the camera's usual level and learns as the supports
  grow. If the supports go from nothing to dense in under ~20 minutes, the
  detector alone may raise a false alarm early in the print. With the Claude
  judge on, Claude reviews it first, and a "looks fine" verdict teaches the
  baseline the new look. Without it, run dry-run on such prints first.

Replaying the measured scores over 20 simulated 15-hour prints gave no false
alarms once calibrated, and caught every +2 jump, typically within 2 minutes.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Not authorized" / no status | Wrong access code (it changes when LAN mode is toggled). Re-run `setup`. |
| "a different serial" at startup | `printer.host` points at another printer, or the serial is mistyped. |
| "certificate not signed by Bambu Lab" | The IP isn't your printer. Only if you're sure, set `printer.tls_verify = false`. |
| Control: REFUSED | Developer Mode is off; see Printer settings above. Alerts still work. |
| Camera closes right after login | Enable LAN Only Liveview; close other viewers (two streams max). |
| "frame too dark" | Turn the chamber light on. |
| `discover` finds nothing | Same network/VLAN as the printer? Bambu Studio can share the port, but a firewall may block UDP 2021. |

## Commands

| Command | What it does |
|---|---|
| `watch [--armed/--dry-run] [--exit-when-done] [--no-camera] [--no-judge] [--plain]` | Watch the printer |
| `setup` | First-time setup (discovery, credentials, config) |
| `check` | Read-only connection and camera test |
| `discover` | List printers announcing themselves on the LAN |
| `snapshot [--image f.jpg] [--out f.jpg] [--judge]` | Score one frame |
| `simulate <scenario>` | Run against the built-in fake printer |
| `test-notify --level info/warning/critical` | Send a test notification |
| `judge-test frame.jpg [current.jpg]` | Ask Claude about one or two images |
| `download-model` | Fetch the detection model |

## Development

```bash
uv run pytest            # unit tests plus end-to-end runs against the fake printer
uv run ruff check src tests
```

The end-to-end tests drive real TLS sockets, the real MQTT client, the camera
reader and the detector against the simulator in `src/print_guard/sim/`.

## Other options considered

* **Bambuddy** (AGPL, self-hosted web app): a full print-farm manager that can
  also use a self-hosted Obico ML server to pause failing prints. Great if you
  want the whole platform; heavier (Docker, separate ML service) for one P1S.
* **Home Assistant + ha-bambulab + Obico ML**: works, but needs both running.
* **OctoEverywhere for Bambu**: hosted AI detection, free tier limited, cloud.
* **Bambu's own detection**: the P1S has none (the X1 and H2 series do).

print-guard reuses the proven parts of these (Obico's model and smoothing,
ha-bambulab's protocol knowledge, CA certificates and error texts) in one small
Windows program. See `THIRD_PARTY_NOTICES.md`.
