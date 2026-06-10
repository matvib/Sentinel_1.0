# Sentinel

A small self-hosted bridge between a home alarm panel and your phone. Sentinel
listens for the panel's Contact ID reports over TCP, writes every event to a
log with human-readable sensor names, shows that log live in a web dashboard,
and sends SMS notifications for the events that matter — alarms and
maintenance issues — via [46elks](https://46elks.com).

Built for Climax-based alarm panels (e.g. Lupus/Lupusec XT series) that can
report Contact ID over TCP/IP and expose the `/action/sensorListGet` HTTP API.

## How it works

```
Alarm panel ──(Contact ID over TCP, port 5002)──> Sentinel
                                                    │
                 ┌──────────────────────────────────┼─────────────────┐
                 │                                  │                 │
          event log on disk                live web dashboard    SMS via 46elks
        (sentinel_events.log)             (push-updated, SSE)    (up to 5 numbers)
```

- The panel pushes Contact ID reports to Sentinel's TCP listener (port 5002).
- Sentinel resolves zone numbers to sensor names by querying the panel's
  sensor list — using the configured panel IP, or, if none is set, the IP
  the reports come from. If the panel can't be reached, events are logged
  with raw zone numbers.
- The dashboard's event log updates only when something happens (Server-Sent
  Events, no polling).
- Critical events (fire, panic, burglary, tamper) send an `‼️ Alarm` SMS;
  maintenance events (low battery) send a `⚠️ Alert` SMS. One message per
  recipient, every send logged.

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
2. Configure the panel to send its Contact ID reports to `<host>:5002`.
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
| `PANEL_IP` | Panel IP; leave blank to auto-detect from incoming reports |
| `PANEL_USER` / `PANEL_PASS` | Panel web login used for the sensor list (config file only, not in the UI) |
| `API_USER` / `API_PASS` | 46elks API credentials |
| `TO_NUMBERS` | List of SMS recipients, max 5 |
| `FROM_SENDER` | SMS sender name |
| `SMS_OVERRIDE` | `true` suppresses event SMS (e.g. while replacing sensor batteries — suppressed alerts are still logged). Manual test buttons always send. |

## Notes

- The panel is reached over plain `http://` only (these panels do not
  support https), so run Sentinel on the same trusted LAN as the panel.
- The TCP listener for Contact ID reports is fixed on port 5002.
