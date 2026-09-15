import socket
import threading
import json
import os
import re
import time
import datetime
import requests
from flask import Flask, render_template, request, redirect, url_for, Response

app = Flask(__name__)
CONFIG_FILE = "config.json"
LOG_FILE = "sentinel_events.log"

# Shared dictionary to hold our dynamic zones (zone number -> name/type)
dynamic_zones = {}
# IP the panel reports from, learned from the first CID message.
# Used to fetch sensor names when no PANEL_URL is configured.
panel_source_ip = None

# Bumped on every log write; lets the event stream wake up waiting browsers.
event_counter = 0
event_cond = threading.Condition()

# ==========================================
# 1. CONFIGURATION & LOG READING
# ==========================================
def load_config():
    """Loads config from file, merged over defaults so missing keys are safe."""
    config = {
        "RUN_PORT": 5000, # Web dashboard port (restart required to change)
        "CID_PORT": 5002, # TCP port the panel sends Contact ID reports to (restart required)
        "PANEL_IP": "", # Blank = auto-detect from the panel's reporting IP
        "PANEL_USER": "admin", "PANEL_PASS": "", # Panel web login, only in config.json
        "DISCOVERY_PATH": "/action/sensorListGet", # Panel HTTP API path for the sensor list
        "API_USER": "", "API_PASS": "",
        "TO_NUMBERS": [], # Up to 5 SMS recipients
        "FROM_SENDER": "HomeAlarm",
        "SMS_OVERRIDE": False # True = suppress all SMS (e.g. while replacing batteries)
    }
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            config.update(json.load(f))
    return config

def save_config(data):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(data, f, indent=4)

def read_recent_logs(limit=15):
    """Reads the log file from disk and returns the last 'limit' lines."""
    if not os.path.exists(LOG_FILE):
        return ["No event logs found yet. System is waiting for panel activity."]

    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            lines = [line.strip() for line in f.readlines() if line.strip()]
            return lines[::-1][:limit]
    except Exception as e:
        return [f"Error reading log history: {e}"]

def append_to_log(message):
    """Helper to cleanly append text to our local log file."""
    global event_counter
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(f"{timestamp} {message}\n")
    # Wake up any browsers waiting on the event stream
    with event_cond:
        event_counter += 1
        event_cond.notify_all()

def get_panel_base_url():
    """Configured PANEL_IP wins; otherwise fall back to the IP the panel reports from.
    The panel only speaks plain http, so the scheme is always hardcoded."""
    config = load_config()
    configured = (config.get("PANEL_IP") or "").strip()
    # Tolerate a pasted URL: strip any scheme/trailing slash so only the IP remains
    configured = re.sub(r'^https?://', '', configured).strip('/')
    if configured:
        return f"http://{configured}"
    if panel_source_ip:
        return f"http://{panel_source_ip}"
    return None

def update_dynamic_zones():
    """Fetches the sensor list from the panel to map zone numbers to names.
    If the panel can't be reached, events simply show raw zone numbers."""
    global dynamic_zones
    base_url = get_panel_base_url()
    if not base_url:
        return "No panel address known yet. Waiting for first report or a configured Panel URL."
    try:
        config = load_config()
        path = config.get("DISCOVERY_PATH") or "/action/sensorListGet"
        if not path.startswith('/'):
            path = '/' + path
        url = f'{base_url}{path}'
        response = requests.get(url, auth=(config["PANEL_USER"], config["PANEL_PASS"]), timeout=5)
        raw_text = response.text
        start_idx = raw_text.find('{')
        end_idx = raw_text.rfind('}') + 1
        clean_text = raw_text[start_idx:end_idx]
        clean_json = re.sub(r'([a-zA-Z0-9_]+)\s*:', r'"\1":', clean_text)

        sensor_list = json.loads(clean_json).get('senrows', [])

        new_zones = {}
        for sensor in sensor_list:
            zone_str = str(sensor.get('zone')).zfill(3)
            new_zones[zone_str] = {
                "name": sensor.get('name', f"Zone {zone_str}"),
                "type": sensor.get('type', 'Unknown')
            }

        dynamic_zones = new_zones
        return None
    except Exception as e:
        return f"Could not fetch sensor names from panel: {e}"

# ==========================================
# 2. DURABLE QUEUES
# ==========================================
# Reports from the panel are persisted to disk BEFORE they are ACKed, and
# SMS stay in a persistent outbox until 46elks accepts them. Both queues
# survive a crash or restart. The listener thread never does anything
# slower than a local file write, so the panel always gets its ACK promptly.
PENDING_EVENTS_FILE = "pending_events.json"
SMS_OUTBOX_FILE = "sms_outbox.json"

state_lock = threading.Lock()                 # guards both queues below
pending_events = []                           # raw reports not yet processed
sms_outbox = []                               # SMS awaiting delivery, in order
events_available = threading.Event()
outbox_cond = threading.Condition(state_lock)

RETRY_BACKOFF = [5, 15, 30, 60, 120, 300]     # seconds between delivery attempts
LATE_DELIVERY_SECONDS = 60                    # after this, the SMS gets the event time appended

def load_json_list(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[!] Could not read {path}: {e}")
        return []

def save_json_list(path, items):
    """Atomic write (temp file + fsync + replace) so a crash never leaves a half-written queue."""
    tmp = path + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(items, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def persist_pending_events():
    try:
        save_json_list(PENDING_EVENTS_FILE, pending_events)
    except Exception as e:
        print(f"[!] Could not persist pending events: {e}")

def persist_outbox():
    try:
        save_json_list(SMS_OUTBOX_FILE, sms_outbox)
    except Exception as e:
        print(f"[!] Could not persist SMS outbox: {e}")

def enqueue_event(raw, source_ip):
    """Called by the listener. Never raises: even if the disk write fails the
    event is kept in memory so the ACK can still go out."""
    entry = {"received": time.time(), "raw": raw, "source_ip": source_ip}
    with state_lock:
        pending_events.append(entry)
        persist_pending_events()
    events_available.set()

def outbox_summary():
    """Small status block for the dashboard."""
    with state_lock:
        pending = len(sms_outbox)
        head = sms_outbox[0] if sms_outbox else None
    if not head:
        return {"pending": 0}
    return {
        "pending": pending,
        "last_error": head.get("last_error"),
        "attempts": head.get("attempts", 0),
        "retry_in": max(0, int(head.get("next_attempt", 0) - time.time()))
    }

# ==========================================
# 3. SMS & ALARM LOGIC (BACKGROUND WORKERS)
# ==========================================
CRITICAL_CODES = ["110", "120", "130", "134", "137"]
MAINTENANCE_CODES = ["302", "384"]

# The two SMS message types and their headers
SMS_HEADERS = {"alarm": "‼️ Alarm", "alert": "⚠️ Alert"}

def format_sms(sms_type, message_text):
    header = SMS_HEADERS.get(sms_type)
    return f"{header}\n{message_text}" if header else message_text

def flatten(text):
    """Multiline SMS bodies on a single line for the event log."""
    return text.replace('\n', ' | ')

def deliver_sms(number, text, config):
    """One 46elks request. Returns (ok, permanent_failure, error)."""
    try:
        response = requests.post(
            'https://api.46elks.com/a1/sms',
            auth=(config["API_USER"], config["API_PASS"]),
            data={'from': config["FROM_SENDER"], 'to': number, 'message': text},
            timeout=(5, 15)
        )
    except requests.RequestException as e:
        return False, False, f"connection error: {e}"
    if response.status_code == 200:
        return True, False, None
    # 400/404 = rejected by 46elks (e.g. invalid number); retrying cannot help.
    # Anything else (auth, rate limit, server error) may clear up, so keep trying.
    permanent = response.status_code in (400, 404)
    return False, permanent, f"HTTP {response.status_code}"

def queue_sms(sms_type, message_text, event_time):
    """Puts one outbox entry per recipient. Entries leave the outbox only on success."""
    config = load_config()
    numbers = [n.strip() for n in config.get("TO_NUMBERS", []) if n and n.strip()]
    full_text = format_sms(sms_type, message_text)
    log_text = flatten(full_text)

    if not all([config["API_USER"], config["API_PASS"]]) or not numbers:
        append_to_log(f"SMS NOT CONFIGURED (no credentials or recipients): {log_text}")
        return
    if config.get("SMS_OVERRIDE"):
        append_to_log(f"SMS OVERRIDE: Suppressed SMS to {len(numbers)} recipient(s): {log_text}")
        return

    with outbox_cond:
        for number in numbers:
            sms_outbox.append({
                "to": number, "text": full_text, "event_time": event_time,
                "attempts": 0, "next_attempt": 0, "last_error": None
            })
        persist_outbox()
        outbox_cond.notify()
    append_to_log(f"SMS QUEUED for {len(numbers)} recipient(s): {log_text}")

def sms_worker():
    """Delivers the outbox head, strictly in order, retrying with backoff forever."""
    while True:
        with outbox_cond:
            while not sms_outbox:
                outbox_cond.wait()
            entry = sms_outbox[0]
            delay = entry.get("next_attempt", 0) - time.time()
            if delay > 0:
                outbox_cond.wait(timeout=delay)
                continue

        text = entry["text"]
        if time.time() - entry.get("event_time", time.time()) > LATE_DELIVERY_SECONDS:
            event_clock = datetime.datetime.fromtimestamp(entry["event_time"]).strftime('%H:%M')
            text += f"\nEvent time {event_clock}"
        log_text = flatten(text)

        ok, permanent, error = deliver_sms(entry["to"], text, load_config())

        with outbox_cond:
            if ok or permanent:
                sms_outbox.pop(0)
            else:
                entry["attempts"] = entry.get("attempts", 0) + 1
                wait = RETRY_BACKOFF[min(entry["attempts"] - 1, len(RETRY_BACKOFF) - 1)]
                entry["next_attempt"] = time.time() + wait
                entry["last_error"] = error
            persist_outbox()
            remaining = len(sms_outbox)

        if ok:
            append_to_log(f"SMS SENT to {entry['to']}: {log_text}")
        elif permanent:
            append_to_log(f"SMS DROPPED to {entry['to']} ({error}, will not retry): {log_text}")
        else:
            append_to_log(f"SMS FAILED to {entry['to']} ({error}), attempt {entry['attempts']}, "
                          f"retry in {wait}s, {remaining} pending")

def process_event(entry):
    """Parses one raw report, logs it, and queues SMS if warranted."""
    raw = entry["raw"]
    remember_panel_ip(entry.get("source_ip"))
    # Every raw report goes in the log as-is, so the ACCT field and
    # message format can be inspected straight from the dashboard
    append_to_log(f"REPORT from {entry.get('source_ip')}: {raw}")

    match = re.search(r'\[(\d+)\s18([13])(\d{3})(\d{2})(\d{3})', raw)
    if not match:
        append_to_log(f"UNPARSED report from panel: {raw}")
        return

    qualifier = match.group(2)
    event_code = match.group(3)
    zone = match.group(5)

    event_names = {
        "110": "Fire Alarm", "120": "Panic Alarm", "130": "Burglary Alarm",
        "134": "Entry/Exit Burglary", "137": "Tamper Alarm",
        "302": "Low System Battery", "384": "Sensor Low Battery",
        "400": "Arm/Disarm", "401": "Armed AWAY / Disarmed", "441": "Armed STAY",
        "602": "Periodic Test", "750": "Sensor Activity"
    }
    event_desc = event_names.get(event_code, f"Unknown Code {event_code}")

    # Look up the friendly name; refresh from the panel once if this zone is new.
    # If the panel is unreachable/unconfigured, fall back to the raw zone number.
    zone_info = dynamic_zones.get(zone)
    if not zone_info:
        update_dynamic_zones()
        zone_info = dynamic_zones.get(zone, {"name": f"Zone {zone}", "type": "Unknown"})
    zone_name = zone_info["name"]
    zone_type = zone_info["type"]

    # --- DYNAMIC STATUS NAMING V2 ---
    if event_code.startswith("4"):
        status = "OPENED/DISARMED" if qualifier == "1" else "CLOSED/ARMED"
    elif event_code == "602":
        status = "SYSTEM CHECK"
    elif event_code.startswith("3"):
        status = "TROUBLE ALERT" if qualifier == "1" else "TROUBLE CLEARED"
    else:
        if zone_type == "Door Contact":
            status = "OPEN" if qualifier == "1" else "CLOSED"
        elif zone_type == "IR Camera":
            status = "MOTION DETECTED" if qualifier == "1" else "CLEAR"
        elif zone_type == "Smoke Sensor":
            status = "SMOKE DETECTED" if qualifier == "1" else "CLEAR"
        else:
            status = "TRIGGERED" if qualifier == "1" else "RESTORED"

    append_to_log(f"{status}: {event_desc} on {zone_name}")

    # --- SMS NOTIFICATIONS ---
    if qualifier == "1":
        if event_code in CRITICAL_CODES:
            queue_sms("alarm", f"{event_desc}\n{zone_name}", entry["received"])
        elif event_code in MAINTENANCE_CODES:
            queue_sms("alert", f"{event_desc}\n{zone_name}", entry["received"])

def event_worker():
    """Processes pending reports in order. Everything slow (panel lookups) happens here."""
    while True:
        events_available.wait()
        while True:
            with state_lock:
                if not pending_events:
                    events_available.clear()
                    break
                entry = pending_events[0]
            try:
                process_event(entry)
            except Exception as e:
                append_to_log(f"Error processing report {entry.get('raw')!r}: {e}")
            with state_lock:
                pending_events.pop(0)
                persist_pending_events()

def run_tcp_server():
    """Receive, persist, ACK. Nothing else — this thread must never block on
    the panel, 46elks, or anything but a local file write."""
    HOST = '0.0.0.0'
    port = int(load_config().get("CID_PORT", 5002))
    print(f"[*] Contact ID listener on port {port}.")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, port))
        s.listen(5)
        while True:
            try:
                conn, addr = s.accept()
                with conn:
                    data = conn.recv(1024)
                    if data:
                        raw = data.decode('utf-8', errors='replace').strip()
                        enqueue_event(raw, addr[0])
                        conn.sendall(b'\x06')
            except Exception as e:
                print(f"TCP Error: {e}")

def remember_panel_ip(ip):
    """Learn the panel's address from where its reports originate."""
    global panel_source_ip
    if ip and panel_source_ip != ip:
        panel_source_ip = ip
        # New (or first) panel address: refresh the zone name map from it
        # unless a panel IP is explicitly configured.
        if not (load_config().get("PANEL_IP") or "").strip():
            update_dynamic_zones()

def start_workers():
    """Recover queues from disk and start the background threads."""
    global pending_events, sms_outbox
    with state_lock:
        pending_events = load_json_list(PENDING_EVENTS_FILE)
        sms_outbox = load_json_list(SMS_OUTBOX_FILE)
        # Retry recovered SMS immediately rather than honoring a stale backoff
        for entry in sms_outbox:
            entry["next_attempt"] = 0
    if pending_events:
        print(f"[*] Recovered {len(pending_events)} unprocessed report(s) from disk.")
        events_available.set()
    if sms_outbox:
        print(f"[*] Recovered {len(sms_outbox)} pending SMS from disk.")
    threading.Thread(target=event_worker, daemon=True).start()
    threading.Thread(target=sms_worker, daemon=True).start()

# ==========================================
# 4. WEB DASHBOARD ROUTES
# ==========================================
@app.route('/')
def home():
    # Refresh the device list so the settings tab shows what the panel reports
    discover_error = update_dynamic_zones()
    devices = [
        {"zone": zone, "name": info["name"], "type": info["type"]}
        for zone, info in sorted(dynamic_zones.items())
    ]
    return render_template('index.html', config=load_config(), devices=devices,
                           discover_error=discover_error, panel_addr=get_panel_base_url())

@app.route('/api/live-dashboard')
def live_dashboard():
    history = read_recent_logs(limit=30)
    return render_template('dashboard_partial.html', history=history, outbox=outbox_summary())

@app.route('/api/event-stream')
def event_stream():
    """Server-Sent Events: notifies the browser only when a new log entry lands."""
    def generate():
        last_seen = event_counter
        # Send a first byte immediately so the browser's onopen fires right away
        # (otherwise headers wait for the first keepalive, up to 30s)
        yield "retry: 3000\n\n"
        while True:
            with event_cond:
                event_cond.wait(timeout=30)
                current = event_counter
            if current != last_seen:
                last_seen = current
                yield "data: update\n\n"
            else:
                # Periodic comment keeps the connection from timing out
                yield ": keepalive\n\n"
    return Response(generate(), mimetype='text/event-stream')

@app.route('/settings', methods=['POST'])
def save_settings():
    # Collect up to 5 recipient numbers, skipping empty fields
    numbers = []
    for i in range(1, 6):
        n = (request.form.get(f'to_number_{i}') or '').strip()
        if n:
            numbers.append(n)

    try:
        run_port = int(request.form.get('run_port') or 5000)
    except ValueError:
        run_port = 5000
    if not 1 <= run_port <= 65535:
        run_port = 5000

    try:
        cid_port = int(request.form.get('cid_port') or 5002)
    except ValueError:
        cid_port = 5002
    if not 1 <= cid_port <= 65535:
        cid_port = 5002

    # The web dashboard and the Contact ID listener cannot share a port
    if cid_port == run_port:
        return redirect(url_for('home', tab='settings', error='port_conflict'))

    # Start from the existing config so keys without a form field
    # (e.g. PANEL_USER/PANEL_PASS) survive a save from the UI
    discovery_path = (request.form.get('discovery_path') or '').strip() or "/action/sensorListGet"
    if not discovery_path.startswith('/'):
        discovery_path = '/' + discovery_path

    updated_config = load_config()
    updated_config.update({
        "RUN_PORT": run_port,
        "CID_PORT": cid_port,
        "PANEL_IP": (request.form.get('panel_ip') or '').strip(),
        "DISCOVERY_PATH": discovery_path,
        "API_USER": request.form.get('api_user'),
        "API_PASS": request.form.get('api_pass'),
        "TO_NUMBERS": numbers,
        "FROM_SENDER": request.form.get('from_sender'),
        "SMS_OVERRIDE": request.form.get('sms_override') == 'on'
    })
    save_config(updated_config)
    return redirect(url_for('home', tab='settings', success='true'))

@app.route('/test-sms', methods=['POST'])
def test_sms():
    """Manual tests deliver directly (a human is waiting for the result) and
    bypass the override, which only silences panel events."""
    sms_type = request.form.get('sms_type', 'alert')
    if sms_type == 'alarm':
        message = "Burglary Alarm\nTest Zone\nTHIS IS A TEST"
    else:
        message = "Sensor Low Battery\nTest Zone\nTHIS IS A TEST"

    config = load_config()
    numbers = [n.strip() for n in config.get("TO_NUMBERS", []) if n and n.strip()]
    if not all([config["API_USER"], config["API_PASS"]]) or not numbers:
        return redirect(url_for('home', tab='settings', sms_status='failed'))

    text = format_sms(sms_type, message)
    all_ok = True
    for number in numbers:
        ok, _, error = deliver_sms(number, text, config)
        if ok:
            append_to_log(f"SMS SENT to {number}: {flatten(text)}")
        else:
            append_to_log(f"SMS FAILED to {number} ({error}): {flatten(text)}")
            all_ok = False
    return redirect(url_for('home', tab='settings', sms_status='sent' if all_ok else 'failed'))

if __name__ == '__main__':
    print("[*] Performing initial zone name sync with panel...")
    error = update_dynamic_zones()
    if error:
        print(f"[*] {error} Events will show raw zone numbers until names are available.")
    else:
        print(f"[*] Loaded {len(dynamic_zones)} zone names into memory.")

    start_workers()
    threading.Thread(target=run_tcp_server, daemon=True).start()

    run_port = int(load_config().get("RUN_PORT", 5000))
    print(f"[*] Web dashboard starting on port {run_port}.")
    app.run(debug=True, host='0.0.0.0', port=run_port, use_reloader=False)
