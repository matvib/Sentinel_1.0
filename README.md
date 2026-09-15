# Sentinel

Get SMS alerts from an alarm panel's Contact ID messages.

A small self-hosted bridge between a home alarm panel and your phone. Sentinel
listens for the panel's Contact ID reports over TCP, writes every event to a
log with human-readable sensor names, shows that log live in a web dashboard,
and sends SMS notifications for the events that matter — alarms and
maintenance issues — via [46elks](https://46elks.com).

**Scope:** the receiver, parser and SMS logic are generic **Ademco Contact ID
over TCP/IP** and should work with any panel that reports that way. The
sensor-name lookup, however, uses the Climax/Vesta local HTTP API
(`/action/sensorListGet`, configurable) and has only been tested on a
**Climax/Vesta CTC-1852Z**. Other panels will work but show raw zone numbers
unless their API happens to match.

> ⚠️ **Read [Panel side: it must be running 24/7](#panel-side-it-must-be-running-247) before pointing a panel at this.**

## How it works

```
Alarm panel ──(Contact ID over TCP, default port 5002)──> Sentinel
                                                    │
                 ┌──────────────────────────────────┼─────────────────┐
                 │                                  │                 │
          event log on disk                live web dashboard    SMS via 46elks
        (sentinel_events.log)             (push-updated, SSE)    (up to 5 numbers)
```

- The panel pushes Contact ID reports to Sentinel's TCP listener (default
  port 5002, configurable in settings).
- Sentinel resolves zone numbers to sensor names by querying the panel's
  sensor list — using the configured panel IP, or, if none is set, the IP
  the reports come from. If the panel can't be reached, events are logged
  with raw zone numbers.
- The dashboard's event log updates only when something happens (Server-Sent
  Events, no polling).
- Critical events (fire, panic, burglary, tamper) send an `‼️ Alarm` SMS;
  maintenance events (low battery) send a `⚠️ Alert` SMS. One message per
  recipient, every send logged.

## Panel side: it must be running 24/7

On the CTC-1852Z (and likely other Climax panels) an unreachable reporting
receiver is a **communication failure**. The panel's status LED turns red and,
**if the panel is armed, the siren sounds**. Panels on an alarm-company
contract never hit this because they fall back to the built-in GSM modem; a
CTC-1852Z run standalone has a 2G-only Cinterion BGS3 modem, which has no
network left to register on in most countries — so Ethernet to this receiver
is the *only* path.

That means:

- Run Sentinel as an **auto-restarting service on an always-on machine**
  (container next to Home Assistant, systemd unit, Windows service) — not as
  `python app.py` in a terminal on a desktop that sleeps.
- **Start Sentinel before plugging the panel in**, and confirm the first report
  arrives (dashboard, or `sentinel_events.log`).
- If you take Sentinel away for good, **clear the reporting URL on the panel
  first**.
- Think about the panel's backup battery: during a mains outage the panel
  stays up on battery while this receiver goes down — armed panel, siren.
  Either disconnect the battery (the CTC-1852Z boots *disarmed* after a full
  power loss, so it comes back quiet) or put the receiver and network gear on a
  UPS. See the
  [vesta-local-ha README](https://github.com/mphel44/vesta-local-ha#-running-the-panel-standalone-no-alarm-company-no-gsm)
  for the full reasoning.

Panel reporting setting (web UI): `rptn://1234@<sentinel-ip>:5002` — account
number `1234`, port matching `CID_PORT`. Sentinel ACKs every report with
`0x06`.

## Setup

Requires Python 3 with Flask and requests:

```
pip install flask requests
python app.py
```

Then:

1. Open the dashboard at `http://<host>:5000` and fill in the **System
   Settings** tab: panel IP, 46elks API credentials, and up to 5 recipient
   numbers.
2. Configure the panel to send its Contact ID reports to `<host>:5002` (or
   whatever you set as the panel reporting port — it just has to match on
   both sides, and it cannot be the same port as the web dashboard).
3. Use the two Diagnostics buttons to send a clearly marked test alarm/alert
   and confirm the SMS path works.

The settings tab also shows the devices discovered on the panel, so you can
verify the connection at a glance.

## Configuration

Settings are stored in `config.json` (created/updated by the settings page,
never committed to git):

| Key | Description |
| --- | --- |
| `RUN_PORT` | Web dashboard port (default 5000, restart to apply) |
| `CID_PORT` | TCP port for the panel's Contact ID reports (default 5002, restart to apply; must differ from `RUN_PORT`) |
| `PANEL_IP` | Panel IP; leave blank to auto-detect from incoming reports |
| `PANEL_USER` / `PANEL_PASS` | Panel web login used for the sensor list (config file only, not in the UI) |
| `DISCOVERY_PATH` | Panel HTTP API path returning the sensor list (default `/action/sensorListGet`) |
| `API_USER` / `API_PASS` | 46elks API credentials |
| `TO_NUMBERS` | List of SMS recipients, max 5 |
| `FROM_SENDER` | SMS sender name |
| `SMS_OVERRIDE` | `true` suppresses event SMS (e.g. while replacing sensor batteries — suppressed alerts are still logged). Manual test buttons always send. |

## Notes

- The panel is reached over plain `http://` only (these panels do not
  support https), so run Sentinel on the same trusted LAN as the panel.
- Both ports are free to change, but the web dashboard and the Contact ID
  listener cannot share one — the settings page enforces this.
