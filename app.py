import socket
import threading
import json
import os
import re
import requests
from flask import Flask, render_template, request, redirect, url_for

app = Flask(__name__)
CONFIG_FILE = "config.json"
LOG_FILE = "sentinel_events.log"

# Shared dictionary to hold our dynamic zones
dynamic_zones = {}
latest_sensors = []

# ==========================================
# 1. CONFIGURATION & LOG READING
# ==========================================
def load_config():
    """Loads config from file, or falls back to defaults."""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {
        "PANEL_URL": "http://192.168.1.193", # Default fallback
        "API_USER": "", "API_PASS": "", 
        "TO_NUMBER": "", "FROM_SENDER": "HomeAlarm"
    }

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
    import datetime
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(f"{timestamp} {message}\n")

def update_dynamic_zones():
    """Fetches sensor data and updates globals."""
    global dynamic_zones, latest_sensors
    try:
        # --- FIXED FOR DYNAMIC CONFIG ---
        config = load_config()
        base_url = config.get("PANEL_URL", "http://192.168.1.193").rstrip('/')
        url = f'{base_url}/action/sensorListGet'
        
        response = requests.get(url, auth=('admin', 'admin1234'), timeout=5)
        raw_text = response.text
        start_idx = raw_text.find('{')
        end_idx = raw_text.rfind('}') + 1
        clean_text = raw_text[start_idx:end_idx]
        clean_json = re.sub(r'([a-zA-Z0-9_]+)\s*:', r'"\1":', clean_text)
        
        sensor_list = json.loads(clean_json).get('senrows', [])
        latest_sensors = sensor_list # Save for the web UI!
        
        # Build a smarter dictionary saving both name AND type
        new_zones = {}
        for sensor in sensor_list:
            zone_str = str(sensor.get('zone')).zfill(3)
            new_zones[zone_str] = {
                "name": sensor.get('name', f"Zone {zone_str}"),
                "type": sensor.get('type', 'Unknown')
            }
            
        dynamic_zones = new_zones
        return sensor_list, None
    except Exception as e:
        return [], f"Could not connect to panel status stream: {e}"
    
# ==========================================
# 2. SMS & ALARM LOGIC (BACKGROUND TASKS)
# ==========================================
def send_sms(message_text):
    config = load_config()
    if not all([config["API_USER"], config["API_PASS"], config["TO_NUMBER"]]):
        print("SMS aborted: Missing credentials.")
        return False

    try:
        response = requests.post(
            'https://api.46elks.com/a1/sms',
            auth=(config["API_USER"], config["API_PASS"]),
            data={'from': config["FROM_SENDER"], 'to': config["TO_NUMBER"], 'message': message_text}
        )
        return response.status_code == 200
    except Exception as e:
        print(f"Error connecting to 46elks: {e}")
        return False

def parse_and_handle_event(data_bytes):
    global dynamic_zones, latest_sensors
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
        
        zone_info = dynamic_zones.get(zone)
        if not zone_info:
            update_dynamic_zones()
            zone_info = dynamic_zones.get(zone, {"name": f"System/Zone {zone}", "type": "Unknown"})
            
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

        # --- INSTANT MEMORY UPDATE ---
        for s in latest_sensors:
            if str(s.get('zone')).zfill(3) == zone:
                if qualifier == "1":
                    if event_code == "384":
                        s['battery'] = "Low"
                    elif zone_type == "Door Contact":
                        s['cond'] = "Open"
                    elif zone_type == "IR Camera":
                        s['cond'] = "Motion"
                    elif zone_type == "Smoke Sensor":
                        s['cond'] = "Smoke"
                    else:
                        s['cond'] = "Alert"
                else:
                    if event_code == "384":
                        s['battery'] = ""
                    else:
                        s['cond'] = ""
                break 

        # --- SMS NOTIFICATIONS ---
        if qualifier == "1":
            if event_code in ["110", "120", "130", "134", "137"]:
                send_sms(f"CRITICAL ALARM: {event_desc} on {zone_name}!")
            elif event_code in ["302", "384"]:
                send_sms(f"MAINTENANCE ALERT: {event_desc} on {zone_name}. Please replace soon.")

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
                        parse_and_handle_event(data)
                        conn.sendall(b'\x06')
            except Exception as e:
                print(f"TCP Error: {e}")

# ==========================================
# 3. WEB DASHBOARD ROUTES
# ==========================================
@app.route('/')
def home():
    sensor_list, error_msg = update_dynamic_zones()
    history = read_recent_logs(limit=15)
    return render_template('index.html', sensors=sensor_list, config=load_config(), history=history, error=error_msg)

@app.route('/api/live-dashboard')
def live_dashboard():
    history = read_recent_logs(limit=15)
    return render_template('dashboard_partial.html', sensors=latest_sensors, history=history)

@app.route('/settings', methods=['POST'])
def save_settings():
    # --- FIXED TO CAPTURE PANEL URL ---
    updated_config = {
        "PANEL_URL": request.form.get('panel_url'),
        "API_USER": request.form.get('api_user'),
        "API_PASS": request.form.get('api_pass'),
        "TO_NUMBER": request.form.get('to_number'),
        "FROM_SENDER": request.form.get('from_sender')
    }
    save_config(updated_config)
    return redirect(url_for('home', tab='settings', success='true'))

@app.route('/test-sms', methods=['POST'])
def test_sms():
    success = send_sms("Sentinel Dashboard: This is a manual alert test transmission.")
    if success:
        return redirect(url_for('home', tab='settings', sms_status='sent'))
    else:
        return redirect(url_for('home', tab='settings', sms_status='failed'))

if __name__ == '__main__':
    print("[*] Performing initial zone sync with panel...")
    update_dynamic_zones()
    print(f"[*] Loaded {len(dynamic_zones)} zones into memory.")

    tcp_thread = threading.Thread(target=run_tcp_server, daemon=True)
    tcp_thread.start()
    app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)