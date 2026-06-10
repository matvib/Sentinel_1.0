import socket
import threading
import json
import os
import re
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
        "PANEL_IP": "", # Blank = auto-detect from the panel's reporting IP
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
        url = f'{base_url}/action/sensorListGet'
        response = requests.get(url, auth=('admin', 'admin1234'), timeout=5)
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
# 2. SMS & ALARM LOGIC (BACKGROUND TASKS)
# ==========================================
CRITICAL_CODES = ["110", "120", "130", "134", "137"]
MAINTENANCE_CODES = ["302", "384"]

# The two SMS message types and their headers
SMS_HEADERS = {"alarm": "‼️ Alarm", "alert": "⚠️ Alert"}

def send_sms(sms_type, message_text, ignore_override=False):
    """Sends an SMS of type 'alarm' or 'alert'; the header/icon is added here.
    ignore_override is for manual tests, which should send even when the
    override (meant to silence panel events) is active."""
    config = load_config()
    numbers = [n.strip() for n in config.get("TO_NUMBERS", []) if n and n.strip()]
    if not all([config["API_USER"], config["API_PASS"]]) or not numbers:
        print("SMS aborted: Missing credentials or recipient numbers.")
        return False

    header = SMS_HEADERS.get(sms_type)
    full_text = f"{header}\n{message_text}" if header else message_text

    # Keep multiline SMS bodies on a single line in the event log
    log_text = full_text.replace('\n', ' | ')

    if config.get("SMS_OVERRIDE") and not ignore_override:
        append_to_log(f"SMS OVERRIDE: Suppressed SMS to {len(numbers)} recipient(s): {log_text}")
        return False

    # 46elks requires one request per recipient, so loop over all configured numbers
    all_ok = True
    for number in numbers:
        try:
            response = requests.post(
                'https://api.46elks.com/a1/sms',
                auth=(config["API_USER"], config["API_PASS"]),
                data={'from': config["FROM_SENDER"], 'to': number, 'message': full_text}
            )
            if response.status_code == 200:
                append_to_log(f"SMS SENT to {number}: {log_text}")
            else:
                append_to_log(f"SMS FAILED to {number} (HTTP {response.status_code}): {log_text}")
                all_ok = False
        except Exception as e:
            append_to_log(f"SMS FAILED to {number} (connection error): {e}")
            all_ok = False
    return all_ok

def parse_and_handle_event(data_bytes):
    try:
        data_str = data_bytes.decode('utf-8', errors='replace').strip()
        match = re.search(r'\[(\d{4})\s18([13])(\d{3})(\d{2})(\d{3})', data_str)
        if not match: return

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

        # Write to log
        log_msg = f"{status}: {event_desc} on {zone_name}"
        append_to_log(log_msg)

        # --- SMS NOTIFICATIONS ---
        if qualifier == "1":
            if event_code in CRITICAL_CODES:
                send_sms("alarm", f"{event_desc}\n{zone_name}")
            elif event_code in MAINTENANCE_CODES:
                send_sms("alert", f"{event_desc}\n{zone_name}")

    except Exception as e:
        append_to_log(f"Error parsing data: {e}")

def run_tcp_server():
    HOST, PORT = '0.0.0.0', 5002
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, PORT))
        s.listen(5)
        while True:
            try:
                conn, addr = s.accept()
                with conn:
                    data = conn.recv(1024)
                    if data:
                        remember_panel_ip(addr[0])
                        parse_and_handle_event(data)
                        conn.sendall(b'\x06')
            except Exception as e:
                print(f"TCP Error: {e}")

def remember_panel_ip(ip):
    """Learn the panel's address from where its reports originate."""
    global panel_source_ip
    if panel_source_ip != ip:
        panel_source_ip = ip
        # New (or first) panel address: refresh the zone name map from it
        # unless a panel IP is explicitly configured.
        if not (load_config().get("PANEL_IP") or "").strip():
            update_dynamic_zones()

# ==========================================
# 3. WEB DASHBOARD ROUTES
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
    return render_template('dashboard_partial.html', history=history)

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

    updated_config = {
        "RUN_PORT": run_port,
        "PANEL_IP": (request.form.get('panel_ip') or '').strip(),
        "API_USER": request.form.get('api_user'),
        "API_PASS": request.form.get('api_pass'),
        "TO_NUMBERS": numbers,
        "FROM_SENDER": request.form.get('from_sender'),
        "SMS_OVERRIDE": request.form.get('sms_override') == 'on'
    }
    save_config(updated_config)
    return redirect(url_for('home', tab='settings', success='true'))

@app.route('/test-sms', methods=['POST'])
def test_sms():
    sms_type = request.form.get('sms_type', 'alert')
    if sms_type == 'alarm':
        message = "Burglary Alarm\nTest Zone\nTHIS IS A TEST"
    else:
        message = "Sensor Low Battery\nTest Zone\nTHIS IS A TEST"

    # Tests bypass the override: that switch only silences panel events
    success = send_sms(sms_type, message, ignore_override=True)
    if success:
        return redirect(url_for('home', tab='settings', sms_status='sent'))
    else:
        return redirect(url_for('home', tab='settings', sms_status='failed'))

if __name__ == '__main__':
    print("[*] Performing initial zone name sync with panel...")
    error = update_dynamic_zones()
    if error:
        print(f"[*] {error} Events will show raw zone numbers until names are available.")
    else:
        print(f"[*] Loaded {len(dynamic_zones)} zone names into memory.")

    tcp_thread = threading.Thread(target=run_tcp_server, daemon=True)
    tcp_thread.start()

    run_port = int(load_config().get("RUN_PORT", 5000))
    print(f"[*] Web dashboard starting on port {run_port}.")
    app.run(debug=True, host='0.0.0.0', port=run_port, use_reloader=False)