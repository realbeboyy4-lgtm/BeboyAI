from flask import Flask, render_template_string
from flask_socketio import SocketIO
import requests
import json
import websocket
import threading
import time

app = Flask(__name__)
app.config['SECRET_KEY'] = 'beboy-secret-key!'
# Setup SocketIO to handle the real-time communication between browser and Flask
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Store session states (handles multiple users if needed)
sessions = {}

BASE_URL = "https://dockerlabs.tutorialsdojo.com"
WS_BASE_URL = "wss://dockerlabs.tutorialsdojo.com"

# ==========================================
# 1. FRONTEND: HTML / JS / CSS (Embedded)
# ==========================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Beboy Alpine Linux session</title>
    
    <!-- Xterm.js for the Terminal UI -->
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.1.0/css/xterm.css" />
    <script src="https://cdn.jsdelivr.net/npm/xterm@5.1.0/lib/xterm.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.7.0/lib/xterm-addon-fit.js"></script>
    
    <!-- Socket.IO Client -->
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
    
    <style>
        body { 
            margin: 0; 
            background: #0f172a; 
            color: #fff; 
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            display: flex;
            flex-direction: column;
            height: 100vh;
            overflow: hidden;
        }
        .header { 
            background: #1e293b; 
            padding: 15px 25px; 
            display: flex; 
            justify-content: space-between; 
            align-items: center;
            border-bottom: 2px solid #334155;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
        }
        .title {
            font-size: 1.2rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .timer-container {
            background: rgba(255,255,255,0.1);
            padding: 8px 16px;
            border-radius: 8px;
            font-size: 0.95rem;
            font-weight: bold;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        #timer { color: #10b981; font-family: monospace; font-size: 1.1rem; }
        #timer.warning { color: #f59e0b; }
        #timer.danger { color: #ef4444; animation: pulse 1s infinite; }
        
        #terminal-container { 
            flex: 1; 
            padding: 10px; 
            background: #000;
        }
        
        @keyframes pulse {
            0% { opacity: 1; }
            50% { opacity: 0.5; }
            100% { opacity: 1; }
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="title">🐧 Beboy Alpine Linux Session</div>
        <div class="timer-container">
            ⏱️ Session expires in: <span id="timer">Loading...</span>
        </div>
    </div>
    
    <div id="terminal-container"></div>

    <script>
        // 1. Initialize Terminal
        const term = new Terminal({
            cursorBlink: true,
            theme: { background: '#000000', foreground: '#f8fafc' },
            fontFamily: 'Menlo, Monaco, "Courier New", monospace'
        });
        
        const fitAddon = new FitAddon.FitAddon();
        term.loadAddon(fitAddon);
        term.open(document.getElementById('terminal-container'));
        fitAddon.fit();

        window.onresize = () => fitAddon.fit();

        // 2. Connect to Flask Backend
        const socket = io();

        socket.on('connect', () => {
            term.write('\\x1b[1;36m[System]\\x1b[0m Connected to Beboy server. Booting remote sandbox...\\r\\n');
            socket.emit('start_session');
        });

        // 3. Handle Output from Server
        socket.on('terminal_output', (msg) => {
            term.write(msg.data);
        });

        // 4. Handle Input from User (Real-time keystrokes)
        term.onData((char) => {
            socket.emit('terminal_input', { char: char });
        });

        // 5. Timer Logic
        let timerInterval;
        socket.on('session_started', (data) => {
            let timeLeft = data.duration;
            clearInterval(timerInterval);
            const timerEl = document.getElementById('timer');
            
            timerEl.style.color = '#10b981';
            timerEl.classList.remove('warning', 'danger');

            timerInterval = setInterval(() => {
                timeLeft--;
                const m = Math.floor(timeLeft / 60).toString().padStart(2, '0');
                const s = (timeLeft % 60).toString().padStart(2, '0');
                timerEl.innerText = `${m}:${s}`;

                if (timeLeft <= 300 && timeLeft > 60) {
                    timerEl.style.color = '#f59e0b'; // Warning at 5 mins
                } else if (timeLeft <= 60) {
                    timerEl.style.color = '';
                    timerEl.className = 'danger'; // Danger at 1 min
                }
                
                if (timeLeft <= 0) {
                    clearInterval(timerInterval);
                    timerEl.innerText = 'EXPIRED';
                    term.write('\\r\\n\\x1b[1;31m[System]\\x1b[0m Session Expired. Please refresh the page.\\r\\n');
                    socket.disconnect();
                }
            }, 1000);
        });
    </script>
</body>
</html>
"""

# ==========================================
# 2. BACKEND: Flask Routes & Logic
# ==========================================
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

def create_environment():
    """Create the session via Playclouds API."""
    response = requests.post(
        f"{BASE_URL}/", 
        data={"stack": "", "stack_name": "", "image_name": ""},
        allow_redirects=False
    )
    if response.status_code == 302:
        return response.headers.get("Location").split("/")[-1]
    raise Exception("Failed to acquire session redirect.")

def get_instance_details(session_id):
    """Fetch instance ID for the WebSocket."""
    response = requests.get(f"{BASE_URL}/sessions/{session_id}")
    instances = response.json().get("instances", {})
    if not instances:
        raise Exception("Environment provisioning failed or timeout.")
    return list(instances.keys())[0]

def playclouds_websocket_thread(sid, session_id, instance_name):
    """Background thread that constantly reads from Docker WS and sends to Browser."""
    ws_url = f"{WS_BASE_URL}/sessions/{session_id}/ws/"
    
    def on_open(ws):
        sessions[sid]['ws'] = ws
        socketio.emit("terminal_output", {"data": "\r\n\x1b[1;32m[System]\x1b[0m Alpine Node Ready. Dropping you into shell...\r\n\r\n"}, to=sid)
        # Send initial enter to prompt the terminal
        ws.send(json.dumps({"name": "instance terminal in", "args": [instance_name, "\r"]}))

    def on_message(ws, message):
        try:
            data = json.loads(message)
            if data.get("name") == "instance terminal out":
                # Forward the raw terminal output straight to the browser
                socketio.emit("terminal_output", {"data": data["args"][1]}, to=sid)
        except:
            pass

    def on_error(ws, error):
        socketio.emit("terminal_output", {"data": f"\r\n\x1b[1;31m[Error]\x1b[0m {error}\r\n"}, to=sid)

    def on_close(ws, close_status_code, close_msg):
        socketio.emit("terminal_output", {"data": "\r\n\x1b[1;33m[System]\x1b[0m Connection to node closed.\r\n"}, to=sid)

    ws = websocket.WebSocketApp(ws_url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
    ws.run_forever()

# ==========================================
# 3. BACKEND: Socket.IO Event Handlers
# ==========================================
@socketio.on("start_session")
def handle_start_session():
    sid = request.sid
    sessions[sid] = {}
    
    def setup_task():
        try:
            session_id = create_environment()
            time.sleep(2) # Give their backend a moment to spin up the container
            instance_name = get_instance_details(session_id)
            
            sessions[sid]['instance_name'] = instance_name
            sessions[sid]['session_id'] = session_id
            
            # Start timer on frontend (15 minutes)
            socketio.emit("session_started", {"duration": 15 * 60}, to=sid)
            
            # Connect the bridge WebSocket in the background
            playclouds_websocket_thread(sid, session_id, instance_name)
            
        except Exception as e:
            socketio.emit("terminal_output", {"data": f"\r\n\x1b[1;31m[System Error]\x1b[0m {str(e)}\r\n"}, to=sid)

    threading.Thread(target=setup_task, daemon=True).start()

@socketio.on("terminal_input")
def handle_input(data):
    """Receive a keystroke from browser and forward to Docker WS."""
    sid = request.sid
    session = sessions.get(sid, {})
    
    ws = session.get('ws')
    instance_name = session.get('instance_name')
    
    if ws and instance_name:
        payload = {
            "name": "instance terminal in",
            "args": [instance_name, data["char"]]
        }
        ws.send(json.dumps(payload))

@socketio.on("disconnect")
def handle_disconnect():
    """Cleanup when a user closes the browser tab."""
    sid = request.sid
    if sid in sessions and sessions[sid].get('ws'):
        sessions[sid]['ws'].close()
        del sessions[sid]

if __name__ == "__main__":
    from flask import request
    print("Starting Beboy Web Terminal on http://127.0.0.1:5000")
    socketio.run(app, host="0.0.0.0", port=5000)
