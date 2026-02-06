#!/usr/bin/env python3
"""
M365 Router - Routes requests to per-connection containers.

Each connection gets its own isolated container. This router dispatches
requests to the correct container based on connection name.
"""

import json
import os
import time
from datetime import datetime
from flask import Flask, jsonify, request
import httpx

# Configuration
HOST = os.getenv("ROUTER_HOST", "0.0.0.0")
PORT = int(os.getenv("ROUTER_PORT", "5200"))
REGISTRY_PATH = os.path.expanduser("~/.m365-connections.json")
BASE_PORT = 5210  # Containers start at this port

app = Flask(__name__)

# Cache connection -> port mapping
_port_map = {}
_start_time = datetime.now()
_request_count = 0
_error_count = 0


def load_port_map():
    """Build connection name -> port mapping from registry."""
    global _port_map
    try:
        with open(REGISTRY_PATH) as f:
            registry = json.load(f)
        connections = list(registry.get("connections", {}).keys())
        _port_map = {name: BASE_PORT + i for i, name in enumerate(sorted(connections))}
    except Exception as e:
        print(f"Failed to load registry: {e}")
        _port_map = {}
    return _port_map


def get_container_url(connection: str) -> str:
    """Get the container URL for a connection."""
    if not _port_map:
        load_port_map()
    if connection not in _port_map:
        return None
    # Use container name for Docker network resolution
    # ForIT-GA -> m365-forit-ga:5200
    container_name = f"m365-{connection.lower()}"
    return f"http://{container_name}:5200"


def proxy_request(connection: str, path: str, method: str = "GET", data: dict = None) -> dict:
    """Proxy request to the correct container."""
    url = get_container_url(connection)
    if not url:
        return {"status": "error", "error": f"Unknown connection: {connection}"}

    try:
        full_url = f"{url}{path}"
        if method == "GET":
            resp = httpx.get(full_url, timeout=120)
        else:
            resp = httpx.post(full_url, json=data, timeout=120)
        return resp.json()
    except httpx.TimeoutException:
        return {"status": "error", "error": f"Container timeout for {connection}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})


@app.route("/status", methods=["GET"])
def status():
    """Aggregate status from all containers."""
    if not _port_map:
        load_port_map()

    all_sessions = []
    container_status = {}

    for connection in _port_map.keys():
        container_name = f"m365-{connection.lower()}"
        try:
            resp = httpx.get(f"http://{container_name}:5200/status", timeout=5)
            data = resp.json()
            container_status[connection] = "healthy"
            for session in data.get("sessions", []):
                all_sessions.append(session)
        except:
            container_status[connection] = "unreachable"

    return jsonify({
        "sessions": all_sessions,
        "containers": container_status,
    })


@app.route("/metrics", methods=["GET"])
def metrics():
    """Aggregate metrics from all containers."""
    if not _port_map:
        load_port_map()

    uptime = (datetime.now() - _start_time).total_seconds()
    container_metrics = {}
    total_requests = 0
    total_errors = 0
    active_sessions = 0

    for connection in _port_map.keys():
        container_name = f"m365-{connection.lower()}"
        try:
            resp = httpx.get(f"http://{container_name}:5200/metrics", timeout=5)
            data = resp.json()
            container_metrics[connection] = {
                "uptime": data.get("uptime_human"),
                "requests": data.get("total_requests", 0),
                "active_sessions": data.get("active_sessions", 0),
            }
            total_requests += data.get("total_requests", 0)
            total_errors += data.get("total_errors", 0)
            active_sessions += data.get("active_sessions", 0)
        except:
            container_metrics[connection] = {"status": "unreachable"}

    return jsonify({
        "router_uptime_seconds": round(uptime, 1),
        "router_uptime_human": f"{int(uptime // 3600)}h {int((uptime % 3600) // 60)}m {int(uptime % 60)}s",
        "total_requests": total_requests,
        "total_errors": total_errors,
        "active_sessions": active_sessions,
        "containers": container_metrics,
        "port_map": _port_map,
    })


@app.route("/connections", methods=["GET"])
def list_connections():
    """List all connections and their container ports."""
    if not _port_map:
        load_port_map()

    try:
        with open(REGISTRY_PATH) as f:
            registry = json.load(f)
        connections = registry.get("connections", {})
        # Add port info
        for name in connections:
            connections[name]["_container_port"] = _port_map.get(name)
        return jsonify({"connections": connections})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)})


@app.route("/run", methods=["POST"])
def run_command():
    """Route command to correct container."""
    global _request_count, _error_count
    _request_count += 1

    data = request.get_json()
    connection = data.get("connection")
    module = data.get("module")
    command = data.get("command")
    caller_id = data.get("caller_id", "anonymous")

    if not all([connection, module, command]):
        _error_count += 1
        return jsonify({"status": "error", "error": "Missing connection, module, or command"}), 400

    # Validate connection exists in registry
    if not _port_map:
        load_port_map()
    if connection not in _port_map:
        _error_count += 1
        return jsonify({
            "status": "error",
            "error": f"Connection '{connection}' not found in registry. Available: {list(_port_map.keys())}",
        }), 404

    # Route to correct container
    result = proxy_request(connection, "/run", "POST", {
        "module": module,
        "command": command,
        "caller_id": caller_id,
    })

    if result.get("status") == "error":
        _error_count += 1

    return jsonify(result)



if __name__ == "__main__":
    load_port_map()
    print(f"Port mapping: {_port_map}")
    app.run(host=HOST, port=PORT, threaded=True)
