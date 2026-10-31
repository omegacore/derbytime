# Scout Race Manager

Self-hosted race management for Cub Scout **Pinewood Derby** and **Rain Gutter Regatta** events.
It runs on a laptop now, and later on a Raspberry Pi acting as an offline Wi-Fi access point.
Phones on the same network are the officials' controllers and the families' live scoreboard.

- Lane-balanced heats (every car/boat runs every lane once), raced by den, plus a final and tie-break runoffs
- Several tracks at once, fed from one shared queue: whichever track is free races the next heat, so a slow track never holds the others up. The scheduler never puts a scout on two tracks at the same moment (racers are shared across events, so this covers a derby and a regatta running side by side)
- Timing sources: simulator, phone stopwatches for regatta lane officials, and USB timers (interface only in phase 1)
- Every result is **pending** until the head timekeeper approves, edits (a reason is required) or calls a re-run. Every change is audit-logged.
- Officials join by scanning a QR code. Viewers need no login and can follow racers for in-page alerts.

## Quick start (laptop)

Requires Python 3.11+.

```bash
git clone <this repo> racemgr && cd racemgr
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"            # or: pip install git+<repo-url>
racemgr init-config                # writes ./config.toml (optional; defaults work)
racemgr init-db
racemgr seed-demo                  # demo derby + regatta, 2 dens, 20 scouts
racemgr serve --host 0.0.0.0 --port 8000
```

On first start with no admin device, the console prints a **one-time admin link**:

```
========================================================================
  No admin device yet. Open this one-time admin link:

    http://192.168.1.140:8000/join/4zMUaT5e...
========================================================================
```

Open it, tap **Join on this device**, and that browser becomes the admin. If you get locked out, run `racemgr admin-url` to print a fresh link (it works while the server is running).

### Reaching it from phones

Phones on the same Wi-Fi use `http://<laptop-LAN-IP>:8000`. The app guesses the LAN IP for QR codes. If it guesses wrong (VPNs, multiple adapters), set `public_base_url` in `config.toml`.

- **Windows:** allow Python through Windows Defender Firewall on *Private* networks when prompted, or run `netsh advfirewall firewall add rule name="racemgr" dir=in action=allow protocol=TCP localport=8000`.
- **macOS:** accept the "allow incoming connections" prompt.
- Guest or "client isolation" Wi-Fi networks block phone-to-laptop traffic. Use a normal network or a phone hotspot.

### Optional HTTPS with mkcert (for Wake Lock etc.)

Everything works over plain HTTP. A few browser features need a *secure context*, though: screen **Wake Lock** on the stopwatch page, and later service workers and Web Push. To enable them on the LAN:

```bash
# install mkcert: https://github.com/FiloSottile/mkcert  (winget install FiloSottile.mkcert / brew install mkcert)
mkcert -install
mkcert 192.168.1.140 localhost 127.0.0.1      # use your LAN IP
racemgr serve --ssl-certfile 192.168.1.140+2.pem --ssl-keyfile 192.168.1.140+2-key.pem
```

Each phone must trust mkcert's root CA. Run `mkcert -CAROOT` to find `rootCA.pem`, send it to the phone, and install it:
- **iOS:** Settings › Profile downloaded › Install, then Settings › General › About › Certificate Trust Settings › enable it.
- **Android:** Settings › Security › Encryption & credentials › Install a certificate › CA certificate.

Without HTTPS, the stopwatch page says "Screen may sleep" and still works. Ask lane officials to keep the screen awake.

## Raspberry Pi

Tested target: Raspberry Pi 4/5 with Raspberry Pi OS (Bookworm, 64-bit), which ships Python 3.11.

```bash
sudo apt update && sudo apt install -y python3-venv git
git clone <this repo> ~/racemgr && cd ~/racemgr
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
racemgr init-config && racemgr init-db
sudo usermod -aG dialout $USER     # access to USB serial timers (log out/in after)
```

Run it as a service, saved as `/etc/systemd/system/racemgr.service`:

```ini
[Unit]
Description=Scout Race Manager
After=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/racemgr
ExecStart=/home/pi/racemgr/.venv/bin/racemgr serve --host 0.0.0.0 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now racemgr
journalctl -u racemgr -f          # shows the one-time admin link on first run
```

Set USB timer ports per track on the admin event page, for example `/dev/ttyUSB0`. For stable names, use `/dev/serial/by-id/...`. The Pi's offline **Wi-Fi access point** setup (hostapd/dnsmasq or NetworkManager hotspot, plus a captive DNS name) is a later phase. Until then, run the Pi on a normal Wi-Fi network or a travel router.

All assets are local (htmx is vendored in `static/vendor`), so nothing needs the internet on race day.

## Configuration

`config.toml` (see [config.example.toml](src/racemgr/config.example.toml) for every key) holds:

| Section | What |
| --- | --- |
| `[server]` | host, port, SQLite `db_path`, `data_dir` (serial logs), `public_base_url` |
| `[auth]` | QR code lifetime, admin link lifetime, device cookie lifetime |
| `[defaults.derby]`, `[defaults.regatta]` | lanes, timing source, scoring mode, outlier min/max time and DNF time applied to **new** tracks and events |
| `[timers.<source>]` | per-source settings: simulator delay/mean/spread; serial port and baud rate |

Events, tracks, rosters and schedules live in the database and are edited from `/admin`. Per-track overrides (lane count, timing source, serial port, outlier limits) are saved on the track itself.

Lookup order for the config file: `--config PATH`, then `$RACEMGR_CONFIG`, then `./config.toml`, then built-in defaults.

## Running an event

1. **Admin** (`/admin`): create the event (one default track is added), adjust tracks, and import the pack roster export (CSV or tab-separated, or paste rows copied from the spreadsheet). Only these columns are used:

   | Column | |
   | --- | --- |
   | `Scout First Name` | required |
   | `Scout Last Name` | |
   | `Den` | each den gets its own runs; den names must match exactly |
   | `Status` | only rows marked `Active` are imported (others are skipped and counted). A file without this column imports every row. |

   Every other column (Member ID, School, parent names/emails/phones/addresses, DOB, dues, notes, …) is ignored and never stored. Car/boat numbers are assigned automatically (an optional `number` column overrides). Scouts and dens are pack-wide, so importing the same roster into the regatta reuses them.
2. **Check-in** (`/checkin`): mark scouts checked in, record weight and pass/fail inspection. Only checked-in entries that haven't failed inspection are scheduled. You can also tick "mark checked in" on import.
3. **Schedule**: on the event's admin page, *Generate schedule* gives every den **runs**. A run is one full lane rotation: every car runs every lane once. The number of runs per den is set on that form (default `passes_per_den = 2` in config).
   - **Default order:** dens are sorted by name with numbers compared numerically (Den 2 before Den 10). The order snakes down the list and back up, e.g. Den 1, Den 2, Den 3, Den 3 · run 2, Den 2 · run 2, Den 1 · run 2.
   - **Race order list:** drag ☰ (works on touch) or use ↑ ↓ to rearrange at any time, including mid-event. The shared queue updates immediately.
   - **Add or remove runs:** *Add a run* appends another rotation for a den. ✕ removes a run that hasn't started. *Reverse order* flips the list.
   - **Tracks:** den runs go into one **shared queue** for all of the event's active tracks. When a track's operator presses *Stage next*, that track takes the earliest heat in the queue it can race, so if one track runs slow, the others keep going and take up the slack. A den finishes quickly and the order you set is the order that races. A heat is tied to a track only once it's staged (*Unstage* puts it back in the shared queue); until then schedules show it as "Next free track", and each track's on-deck list is a forecast. A heat is skipped for a track that's missing one of its lanes (e.g. a broken lane), and never staged while one of its scouts is racing on another track.
   - **Standings and finals:** standings combine all of a den's runs. Finals and runoffs stay on one track.
   - **Regenerate any time:** *Regenerate remaining heats* rebuilds every unraced heat from the current check-ins, tracks and working lanes. Heats that have raced, are on the track, or are awaiting approval keep their results. A late arrival gets a full set of runs, and a scratched scout's remaining heats are dropped. A run already under way keeps its length, so every car in it races the same number of times. The admin page warns when check-ins have changed since the schedule was built.
   - **Lanes and tracks:** untick a broken lane (*Lanes in use*), change a lane count, switch a track off, or add a track (another gutter). Unraced heats are rescheduled automatically.
4. **Officials** (`/admin/officials`): create QR codes per role. Use *one code per lane* for regatta lane officials. Each code is short-lived, single-use by default, and sets a device cookie. Revoke any device from the same page.
5. **Race**:
   - Track operator (`/track/{id}/operate`): *Stage next*, then *Arm*. The timer reports results as pending. If the timer fails, use *Manual entry*.
   - **Regatta shared start:** once a heat is armed, the operator's page shows a big **START** button. Press it the moment the boats are released.
     - **Relayed start:** the start goes out live to every lane official. Their stopwatch starts running by itself and the big button becomes **STOP**.
     - **Earliest press wins:** a lane official may press START instead, e.g. with no operator, or offline. The earliest START press, from anyone, is the heat's start. A press that arrives late but was earlier corrects the times automatically, even after all lanes have stopped.
     - **Times:** each lane's time is its STOP press minus the shared start, computed on the server.
     - **Clock sync:** phones sync their clocks to the server, typically within a few hundredths of a second on local Wi-Fi.
   - **Starting a heat:** any of the three can press START: the operator or either lane official. Everyone's screen shows who started it ("Started by Lane 2") with the clock running. Later presses are discarded.
   - **Disarm** (bad start, boats not ready): the heat goes back to *staged* and every lane phone resets. **Unstage** sends a staged heat back to the queue.
   - **Re-run** only appears once the race has started: a regatta heat after START, a derby heat once armed. It voids the attempt and puts the same lineup straight back on the track, staged and disarmed, and every lane phone resets. From the timekeeper page, if the track has already moved on, the re-run goes to the front of the shared queue instead.
   - Lane officials (`/track/{id}/lane/{n}`): big START/STOP button with Resume (undo an accidental STOP)/Submit/DNF. If a submission can't reach the server, it's kept on the phone and retried on reconnect. The official's own START press goes with it.
   - Head timekeeper (`/timekeeper`): approve, edit (reason required) or re-run. Times outside the track's min/max are flagged. A re-run goes to the front of the shared queue.
   - **Re-run reasons:** pick one from a dropdown (or *Other…* to type one). The lists are per event type in `config.toml` (`rerun_reasons`).
6. **Ties and finals**: tied scores at a den's finalist cutoff (or within the top 3 of the final) show an *Add runoff* button. The runoff's result breaks the tie. *Create final* takes the top N per den (set per event).

Displays for a projector or TV: `/display/ondeck/{track}` and `/display/standings` (add `?event=ID` to limit it to one event). Families use `/`: search, schedule and results, and **☆ Follow**. Follows are stored on the phone. Alerts fire at "2 heats away", "on deck", "racing now" and "results posted" as an in-page banner with a beep and vibration (sound needs one tap on the page first, a browser rule).

### Scoring modes

| Mode | Rank by | Notes |
| --- | --- | --- |
| `average_time` | mean time, lower first | a DNF counts as the event's DNF time |
| `best_time` | fastest single run | |
| `points` | points per heat, higher first | 1st = number of racers in the heat, down to 1 for last; DNF = 0 |

Ties are scores equal to the thousandth. They share a rank until a runoff separates them.

## Architecture

```
src/racemgr/
  core/            pure Python: lane rotation, multi-track planner, placing/standings/ties  (no web/DB imports, enforced by a test)
  models.py        SQLModel tables
  services/        heat lifecycle (race.py), schedule & standings, alerts, roster, audit
  timers/          TimerSource interface; simulator, phone stopwatch, USB serial base + stubs
  bus.py           in-process async event bus (thread-safe publish)
  ws.py            WebSocket hub: forwards bus events to browsers
  routes/          pages (+ HTMX partials), JSON/HTMX actions, admin
  templates/ static/
```

- **Event flow:** every state change goes through `RaceService`, which writes the DB and audit log, then publishes events such as `heat.pending`, `track.state` and `alert`. Each event carries routing keys like `track-3` or `racer-12`. Pages mark panels with `data-live="track-3"`, and `static/live.js` re-fetches a panel's HTMX partial when a matching key arrives. It waits while someone is typing in that panel.
- **Many viewers, one render:** public partials (`@public_cached`) and shared computations such as standings come from `cache.py`. Every bus event and every non-GET request bumps a data version. Anonymous phones asking for the same URL at the same version share one render, and the first request renders while the rest wait for it. Officials (any device cookie) always get a fresh render. Public panels add `data-jitter` so phones don't all re-fetch in the same instant.
- **Timing sources** emit `PendingResult`s. A track's `timing_source` picks the class from `timers.REGISTRY`. To add a USB timer, implement `parse_line()` (and optionally `arm_bytes()`) in `timers/usb.py`. `SerialTimerSource` already handles hotplug reconnects, connected/disconnected status, and logging every raw line with a timestamp to `data/serial/track<id>-YYYYMMDD.log`.
- **Web Push later:** alerts are computed on the server (`services/alerts.py`) and published as bus events keyed by racer. A push sender is one more bus subscriber plus a table of push subscriptions. Push needs HTTPS (see mkcert above).
- **Auth:** a `JoinToken` (role + optional track/lane, expiry, max uses) is redeemed for an `Official` with a random device cookie. Only its SHA-256 hash is stored. Permissions are in `auth.py`.

## Development

```bash
pip install -e ".[dev]"
pytest                      # core, services, HTTP/WebSocket and CLI tests
```

Tests use a temporary SQLite file and the simulator with a near-zero delay. `core/` is covered exhaustively (lane balance for 1–25 entries × 2–8 lanes, ties, runoffs, finalist cutoffs). The service and HTTP tests run the full stage → arm → pending → approve/edit/re-run lifecycle, phone submissions (idempotent retries, resuming after a restart), cross-track conflicts, QR join and permission scoping, and WebSocket delivery.

### Load testing

`tools/loadtest.py` simulates race day. It races heats on every track: the operator stages, arms and starts, every lane official hits STOP within milliseconds of the others, and the timekeeper approves. Meanwhile 100 simulated family phones keep racer, home and event pages open and live-refresh them exactly like the browser does. It prints latencies and PASS/FAIL (by default: official actions p95 ≤ 1 s, viewer pages p95 ≤ 3 s, no errors). It works on a copy of the database, never the real one, and races the copy's scheduled heats, so set up the event first.

```bash
# server on this machine
python tools/loadtest.py local --db racemgr.db

# another machine as the server
python tools/loadtest.py prepare --db racemgr.db --out lt          # on the server
racemgr --config lt/config.toml serve --port 8765                  # on the server
python tools/loadtest.py run --url http://SERVER-IP:8765 --plan lt/plan.json   # from a laptop
```

`--viewers`, `--heats` and `--gap` (seconds per official step; 8 is about one real heat a minute) scale it up. `--gap 1` is a useful stress test. Run it from a laptop on the event Wi-Fi to test the network as well as the server.

The database schema is created with `create_all`. Until migrations (Alembic) arrive, delete the `.db` file after schema changes during development.

## Roadmap

- Phase 2: MicroWizard / Champ / Arduino gate parsers
- Later: awards and voting, Pi access-point setup, Web Push, Alembic migrations
