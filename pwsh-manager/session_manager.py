#!/usr/bin/env python3
"""
PowerShell Session Manager - Persistent M365/Azure PowerShell Sessions

Maintains long-lived PowerShell sessions with authenticated connections to:
- Exchange Online (EXO)
- PnP PowerShell (SharePoint/Teams)
- Azure PowerShell (Az)
- Power Platform

Exposes HTTP API for MCP servers to execute commands.
"""

import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from flask import Flask, jsonify, request

# Configuration from environment
HOST = os.getenv("PWSH_MANAGER_HOST", "0.0.0.0")
PORT = int(os.getenv("PWSH_MANAGER_PORT", "5100"))
LOG_LEVEL = os.getenv("PWSH_MANAGER_LOG_LEVEL", "INFO")
TOKEN_DIR = os.getenv("PWSH_MANAGER_TOKEN_DIR", "/data/tokens")

# Watchdog configuration
WATCHDOG_INTERVAL = int(os.getenv("PWSH_WATCHDOG_INTERVAL", "30"))  # Check every 30s
AUTH_PENDING_TIMEOUT = int(os.getenv("PWSH_AUTH_TIMEOUT", "300"))  # 5 min to complete device code
IDLE_SESSION_TIMEOUT = int(os.getenv("PWSH_IDLE_TIMEOUT", "3600"))  # 1 hour idle = cleanup
STUCK_PROCESS_TIMEOUT = int(os.getenv("PWSH_STUCK_TIMEOUT", "120"))  # 2 min unresponsive = kill

# Setup logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Marker for command completion
MARKER = "___PWSH_DONE___"

# Module configurations
MODULES = {
    "exo": {
        "name": "ExchangeOnlineManagement",
        "connect_cmd": 'Connect-ExchangeOnline -Organization "{tenant}" -Device -ShowBanner:$false',
        "disconnect_cmd": "Disconnect-ExchangeOnline -Confirm:$false",
        "check_cmd": "Get-ConnectionInformation | Select-Object -First 1",
        "check_pattern": r"(Organization|TenantId)",
    },
    "pnp": {
        "name": "PnP.PowerShell",
        # PnP v3.x requires ClientId for DeviceLogin - using ForIT's registered PnP PowerShell App
        "connect_cmd": 'Connect-PnPOnline -Url "https://{tenant}.sharepoint.com" -DeviceLogin -ClientId "9f457af8-dd93-4311-aedc-4ab5c663c493"',
        "disconnect_cmd": "Disconnect-PnPOnline",
        "check_cmd": "Get-PnPConnection",
        "check_pattern": r"(Url|ConnectionType)",
    },
    "azure": {
        "name": "Az.Accounts",
        "connect_cmd": 'Connect-AzAccount -Tenant "{tenant}" -DeviceCode',
        "disconnect_cmd": "Disconnect-AzAccount -Scope CurrentUser",
        "check_cmd": "(Get-AzContext).Account.Id",
        "check_pattern": r"@",
    },
    "teams": {
        "name": "MicrosoftTeams",
        "connect_cmd": 'Connect-MicrosoftTeams -TenantId "{tenant}" -UseDeviceAuthentication',
        "disconnect_cmd": "Disconnect-MicrosoftTeams",
        "check_cmd": "Get-CsTenant | Select-Object -First 1",
        "check_pattern": r"(TenantId|DisplayName)",
    },
    "powerplatform": {
        "name": "PAC CLI",  # PowerShell module doesn't work on Linux, use PAC CLI
        "use_pac_cli": True,  # Flag to indicate PAC CLI instead of PowerShell
        "connect_cmd": 'pac auth create --deviceCode',
        "disconnect_cmd": "pac auth clear",
        "check_cmd": "pac auth list",
        "check_pattern": r"(UNIVERSAL|Public)",  # PAC shows "UNIVERSAL" or cloud type when authenticated
    },
}


@dataclass
class Session:
    """Manages a single PowerShell session."""

    tenant: str
    module: str
    sharepoint_tenant: Optional[str] = None  # SharePoint tenant prefix (e.g., "foritllc" for foritllc.sharepoint.com)
    conversation_id: Optional[str] = None  # MCP conversation that created this session
    process: Optional[subprocess.Popen] = None
    authenticated: bool = False
    auth_pending: bool = False
    auth_pending_since: Optional[datetime] = None  # When auth_pending started
    auth_completed_at: Optional[datetime] = None  # When auth completed
    created_at: datetime = field(default_factory=datetime.now)
    last_used: datetime = field(default_factory=datetime.now)
    last_health_check: Optional[datetime] = None  # Last successful health check
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _output_queue: queue.Queue = field(default_factory=queue.Queue)
    _reader_thread: Optional[threading.Thread] = None
    _connect_marker_seen: bool = False  # Flag set by reader thread when MARKER detected during auth_pending
    _azure_account_choice: str = "1"  # Which account to select when Azure prompts (default: first)
    _was_pending: bool = False  # Track if auth just completed from pending state

    def start(self) -> bool:
        """Start pwsh or bash process and load module."""
        if self.process and self.process.poll() is None:
            return True

        config = MODULES[self.module]
        use_pac_cli = config.get("use_pac_cli", False)
        start_time = time.time()

        try:
            if use_pac_cli:
                # PAC CLI uses bash
                self.process = subprocess.Popen(
                    ["bash"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env={**os.environ, "DOTNET_ROOT": "/usr/local/dotnet",
                         "PATH": f"/usr/local/dotnet:/root/.dotnet/tools:{os.environ.get('PATH', '')}"}
                )
                spawn_ms = int((time.time() - start_time) * 1000)
                logger.info(f"[{self.tenant}:{self.module}] TIMING: Process spawn took {spawn_ms}ms")
                self._reader_thread = threading.Thread(target=self._read_output, daemon=True)
                self._reader_thread.start()
                # Test PAC CLI is available
                import_start = time.time()
                result = self._send("pac help > /dev/null 2>&1 && echo 'MODULE_OK'")
                import_ms = int((time.time() - import_start) * 1000)
                logger.info(f"[{self.tenant}:{self.module}] TIMING: PAC CLI check took {import_ms}ms")
                logger.info(f"[{self.tenant}:{self.module}] PAC CLI check: {result[:100]}")
                return "MODULE_OK" in result
            else:
                # PowerShell modules
                env = os.environ.copy()
                # Disable Azure WAM and new login experience via environment
                if self.module == "azure":
                    env["AZURE_POWERSHELL_PREFERENCES_ENABLELOGINBYWAM"] = "false"
                self.process = subprocess.Popen(
                    ["pwsh", "-NoProfile", "-NoLogo", "-NoExit", "-Command", "-"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=env
                )
                spawn_ms = int((time.time() - start_time) * 1000)
                logger.info(f"[{self.tenant}:{self.module}] TIMING: pwsh spawn took {spawn_ms}ms")
                self._reader_thread = threading.Thread(target=self._read_output, daemon=True)
                self._reader_thread.start()
                module_name = config["name"]
                # For Azure, disable the new login experience and WAM before import
                if self.module == "azure":
                    az_config_start = time.time()
                    self._send("$env:AZURE_POWERSHELL_PREFERENCES_ENABLELOGINBYWAM = 'false'; Update-AzConfig -LoginExperienceV2 Off -EnableLoginByWam $false 2>$null", timeout=10)
                    az_config_ms = int((time.time() - az_config_start) * 1000)
                    logger.info(f"[{self.tenant}:{self.module}] TIMING: Azure config took {az_config_ms}ms")
                import_start = time.time()
                result = self._send(f"Import-Module {module_name} -ErrorAction Stop; 'MODULE_OK'")
                import_ms = int((time.time() - import_start) * 1000)
                total_ms = int((time.time() - start_time) * 1000)
                logger.info(f"[{self.tenant}:{self.module}] TIMING: Module import took {import_ms}ms (total start: {total_ms}ms)")
                logger.info(f"[{self.tenant}:{self.module}] Module import: {result[:100]}")
                return "MODULE_OK" in result

        except Exception as e:
            logger.error(f"[{self.tenant}:{self.module}] Failed to start: {e}")
            return False

    def _read_output(self):
        """Background reader thread."""
        try:
            while self.process and self.process.poll() is None:
                line = self.process.stdout.readline()
                if line:
                    self._output_queue.put(line)
                    # For Azure: detect account selection prompt and send configured choice
                    # Check for prompt regardless of auth_pending since it appears before device code
                    if self.module == "azure" and "Please select" in line:
                        try:
                            if self.process and self.process.stdin:
                                choice = self._azure_account_choice
                                self.process.stdin.write(f"{choice}\n")
                                self.process.stdin.flush()
                                logger.info(f"[{self.tenant}:{self.module}] Detected account prompt, sent '{choice}' to stdin")
                        except Exception as e:
                            logger.warning(f"[{self.tenant}:{self.module}] Failed to send stdin: {e}")
                    # If auth is pending and we see MARKER, the Connect command finished
                    if MARKER in line:
                        logger.info(f"[{self.tenant}:{self.module}] MARKER detected in reader thread (auth_pending={self.auth_pending})")
                        if self.auth_pending:
                            self._connect_marker_seen = True
                            logger.info(f"[{self.tenant}:{self.module}] Connect command completed, flag set")
            # Log why loop exited
            if self.process:
                exit_code = self.process.poll()
                logger.warning(f"[{self.tenant}:{self.module}] Reader thread exited, process exit code: {exit_code}")
            else:
                logger.warning(f"[{self.tenant}:{self.module}] Reader thread exited, process was None")
        except Exception as e:
            logger.error(f"[{self.tenant}:{self.module}] Reader thread exception: {e}")

    def _send(self, cmd: str, timeout: int = 120, early_return_pattern: str = None) -> str:
        """Send command and collect output."""
        if not self.process or self.process.poll() is not None:
            return "ERROR: Process not running"

        with self._lock:
            # Clear queue
            while not self._output_queue.empty():
                try:
                    self._output_queue.get_nowait()
                except queue.Empty:
                    break

            # Send with marker
            self.process.stdin.write(f"{cmd}; Write-Output '{MARKER}'\n")
            self.process.stdin.flush()

            # Collect until marker or early return
            lines = []
            start = time.time()
            while time.time() - start < timeout:
                try:
                    line = self._output_queue.get(timeout=1)
                    if MARKER in line:
                        break
                    lines.append(line.rstrip())

                    if early_return_pattern:
                        current = "\n".join(lines)
                        if re.search(early_return_pattern, current, re.IGNORECASE):
                            logger.info(f"[{self.tenant}:{self.module}] Early return: {early_return_pattern}")
                            return current
                except queue.Empty:
                    continue

            self.last_used = datetime.now()
            return "\n".join(lines)

    def connect(self) -> tuple[bool, str]:
        """Connect to the service using device code auth."""
        connect_start = time.time()

        if not self.start():
            return False, "Failed to start session"

        start_ms = int((time.time() - connect_start) * 1000)
        logger.info(f"[{self.tenant}:{self.module}] TIMING: Session start took {start_ms}ms")

        config = MODULES[self.module]
        use_pac_cli = config.get("use_pac_cli", False)

        # Format connect command with tenant
        if use_pac_cli:
            # PAC CLI doesn't need tenant in connect command
            cmd = config["connect_cmd"]
        elif self.module == "pnp":
            # PnP needs SharePoint tenant prefix (e.g., "foritllc" for foritllc.sharepoint.com)
            if self.sharepoint_tenant:
                tenant_part = self.sharepoint_tenant
            elif "." in self.tenant:
                tenant_part = self.tenant.split(".")[0]
            else:
                tenant_part = self.tenant
            cmd = config["connect_cmd"].format(tenant=tenant_part)
        else:
            cmd = config["connect_cmd"].format(tenant=self.tenant)

        logger.info(f"[{self.tenant}:{self.module}] Connecting: {cmd}")

        # Device code pattern - handles ANSI escape codes and various output formats
        # Azure wraps codes like [4mBZ6ADWR6L[0m, EXO outputs plain codes
        # PnP uses "code XXXX to proceed" instead of "to authenticate"
        # PAC CLI uses "enter the code XXXX to authenticate"
        device_code_pattern = r"(?:code|enter the code)\s+(?:\x1b\[[0-9;]*m)?([A-Z0-9]{8,})(?:\x1b\[[0-9;]*m)?\s+to\s+(?:authenticate|proceed)"
        cmd_start = time.time()
        result = self._send(cmd, timeout=60, early_return_pattern=device_code_pattern)
        cmd_ms = int((time.time() - cmd_start) * 1000)
        total_ms = int((time.time() - connect_start) * 1000)

        logger.info(f"[{self.tenant}:{self.module}] TIMING: Connect command took {cmd_ms}ms (total connect: {total_ms}ms)")
        logger.info(f"[{self.tenant}:{self.module}] Connect result: {result[:200]}")

        # Check for device code
        if re.search(device_code_pattern, result, re.IGNORECASE):
            self.auth_pending = True
            self.auth_pending_since = datetime.now()
            return True, result

        # Check for errors
        if "error" in result.lower() and ("exception" in result.lower() or "failed" in result.lower()):
            return False, f"ERROR: {result}"

        # No device code and no error = timeout or unexpected output
        if not result.strip():
            return False, "ERROR: No response from PowerShell (timeout after 15s)"

        # Check if already connected (no device code needed)
        connected, msg = self._verify_connection()
        if connected:
            return True, msg

        return False, f"ERROR: No device code received. Output: {result[:200]}"

    def _verify_connection(self) -> tuple[bool, str]:
        """Verify the connection is established."""
        config = MODULES[self.module]
        check = self._send(config["check_cmd"], timeout=15)

        if re.search(config["check_pattern"], check):
            self.authenticated = True
            self.auth_pending = False
            self.auth_pending_since = None
            self.last_health_check = datetime.now()
            return True, "Connected"
        return False, f"Connection verification failed: {check}"

    def check(self) -> bool:
        """Check if session is connected."""
        check_start = time.time()

        if not self.process or self.process.poll() is not None:
            return False

        if self.auth_pending:
            # Check if reader thread detected MARKER (Connect command completed)
            if not self._connect_marker_seen:
                # Still waiting for Connect command to complete
                return False
            # Flag seen - Connect finished, now verify
            pending_ms = int((datetime.now() - self.auth_pending_since).total_seconds() * 1000) if self.auth_pending_since else 0
            logger.info(f"[{self.tenant}:{self.module}] TIMING: Auth completion detected after {pending_ms}ms pending")

        config = MODULES[self.module]
        cmd_start = time.time()
        result = self._send(config["check_cmd"], timeout=15)
        cmd_ms = int((time.time() - cmd_start) * 1000)
        logger.info(f"[{self.tenant}:{self.module}] TIMING: Check command took {cmd_ms}ms")
        logger.info(f"[{self.tenant}:{self.module}] Check result: {result[:200] if result else 'empty'}")
        connected = bool(re.search(config["check_pattern"], result))

        if connected:
            # Track if transitioning from pending to authenticated
            was_pending = self.auth_pending and self.auth_pending_since is not None
            self._was_pending = was_pending
            if was_pending:
                self.auth_completed_at = datetime.now()
            self.authenticated = True
            self.auth_pending = False
            self._connect_marker_seen = False
            total_ms = int((time.time() - check_start) * 1000)
            logger.info(f"[{self.tenant}:{self.module}] TIMING: Auth verified, total check took {total_ms}ms (was_pending={was_pending})")
            self.auth_pending_since = None
            self.last_health_check = datetime.now()
        elif self.auth_pending:
            # Auth completed (flag seen) but check failed - log but keep auth_pending
            # so we don't spam new login attempts. Watchdog will timeout if stuck.
            logger.warning(f"[{self.tenant}:{self.module}] Auth flag seen but check failed, keeping pending state")
        return connected

    def run(self, command: str) -> tuple[bool, str]:
        """Run a PowerShell command."""
        run_start = time.time()

        # If auth was pending, check if it completed
        if self.auth_pending:
            self.check()

        if not self.authenticated:
            if self.auth_pending:
                return False, "Authentication still pending - complete the device code flow first"
            return False, "Not authenticated - call login first"

        cmd_start = time.time()
        result = self._send(
            f"$result = {command}; if ($result) {{ $result | ConvertTo-Json -Depth 10 }} else {{ 'OK' }}"
        )
        cmd_ms = int((time.time() - cmd_start) * 1000)
        total_ms = int((time.time() - run_start) * 1000)
        logger.info(f"[{self.tenant}:{self.module}] TIMING: Command took {cmd_ms}ms (total run: {total_ms}ms) - {command[:50]}...")

        if "error" in result.lower() and "exception" in result.lower():
            return False, result
        return True, result

    def disconnect(self):
        """Disconnect and stop session."""
        if self.process:
            config = MODULES[self.module]
            if config["disconnect_cmd"]:
                try:
                    self._send(config["disconnect_cmd"], timeout=10)
                except Exception:
                    pass
            self.process.terminate()
            self.process = None
        self.authenticated = False
        self.auth_pending = False
        self.auth_pending_since = None

    def force_kill(self) -> bool:
        """Force kill the session process without graceful disconnect."""
        killed = False
        if self.process:
            try:
                self.process.kill()  # SIGKILL, no waiting
                killed = True
                logger.warning(f"[{self.tenant}:{self.module}] Force killed process")
            except Exception as e:
                logger.error(f"[{self.tenant}:{self.module}] Force kill failed: {e}")
            self.process = None
        self.authenticated = False
        self.auth_pending = False
        self.auth_pending_since = None
        return killed

    def is_stuck(self) -> tuple[bool, str]:
        """Check if session is stuck and needs recovery."""
        now = datetime.now()

        # Check if auth_pending too long
        if self.auth_pending and self.auth_pending_since:
            pending_seconds = (now - self.auth_pending_since).total_seconds()
            if pending_seconds > AUTH_PENDING_TIMEOUT:
                return True, f"auth_pending for {int(pending_seconds)}s (limit: {AUTH_PENDING_TIMEOUT}s)"

        # Check if process is dead but session thinks it's alive
        if self.process and self.process.poll() is not None:
            return True, "process died unexpectedly"

        # Check if authenticated session hasn't been health-checked recently
        if self.authenticated and self.last_health_check:
            idle_seconds = (now - self.last_health_check).total_seconds()
            if idle_seconds > IDLE_SESSION_TIMEOUT:
                return True, f"idle for {int(idle_seconds)}s (limit: {IDLE_SESSION_TIMEOUT}s)"

        return False, "healthy"


class SessionManager:
    """Manages all PowerShell sessions."""

    def __init__(self):
        self.sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._watchdog_thread: Optional[threading.Thread] = None
        self._watchdog_running = False

    def start_watchdog(self):
        """Start the session health watchdog thread."""
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return
        self._watchdog_running = True
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()
        logger.info(f"Watchdog started (interval: {WATCHDOG_INTERVAL}s, auth_timeout: {AUTH_PENDING_TIMEOUT}s)")

    def stop_watchdog(self):
        """Stop the watchdog thread."""
        self._watchdog_running = False

    def _watchdog_loop(self):
        """Background thread that monitors session health."""
        while self._watchdog_running:
            try:
                self._check_all_sessions()
            except Exception as e:
                logger.error(f"Watchdog error: {e}")
            time.sleep(WATCHDOG_INTERVAL)

    def _check_all_sessions(self):
        """Check all sessions and recover stuck ones."""
        session_count = len(self.sessions)
        if session_count > 0:
            logger.debug(f"Watchdog checking {session_count} sessions...")

        with self._lock:
            sessions_to_kill = []
            for key, session in self.sessions.items():
                stuck, reason = session.is_stuck()
                if stuck:
                    logger.warning(f"[{key}] Stuck session detected: {reason}")
                    sessions_to_kill.append(key)

        # Kill stuck sessions outside the lock to avoid deadlock
        for key in sessions_to_kill:
            tenant, module = key.split(":", 1)
            logger.info(f"[{key}] Watchdog force-killing stuck session")
            self.force_kill_session(tenant, module)
            logger.info(f"[{key}] Session killed by watchdog")

    def force_kill_session(self, tenant: str, module: str) -> bool:
        """Force kill a session without graceful disconnect."""
        key = self._key(tenant, module)
        with self._lock:
            if key in self.sessions:
                killed = self.sessions[key].force_kill()
                del self.sessions[key]
                return killed
        return False

    def _key(self, tenant: str, module: str) -> str:
        return f"{tenant}:{module}"

    def get_session(self, tenant: str, module: str, sharepoint_tenant: str = None, conversation_id: str = None) -> Session:
        """Get or create a session."""
        key = self._key(tenant, module)
        with self._lock:
            if key not in self.sessions:
                self.sessions[key] = Session(
                    tenant=tenant,
                    module=module,
                    sharepoint_tenant=sharepoint_tenant,
                    conversation_id=conversation_id
                )
            else:
                # Update existing session with optional params if not set
                if sharepoint_tenant and not self.sessions[key].sharepoint_tenant:
                    self.sessions[key].sharepoint_tenant = sharepoint_tenant
                if conversation_id and not self.sessions[key].conversation_id:
                    self.sessions[key].conversation_id = conversation_id
            return self.sessions[key]

    def list_sessions(self) -> list[dict]:
        """List all sessions with status."""
        result = []
        for key, session in self.sessions.items():
            stuck, stuck_reason = session.is_stuck()
            result.append({
                "tenant": session.tenant,
                "module": session.module,
                "conversation_id": session.conversation_id,
                "authenticated": session.authenticated,
                "auth_pending": session.auth_pending,
                "auth_pending_since": session.auth_pending_since.isoformat() if session.auth_pending_since else None,
                "auth_completed_at": session.auth_completed_at.isoformat() if session.auth_completed_at else None,
                "connected": session.check() if session.authenticated else False,
                "created_at": session.created_at.isoformat(),
                "last_used": session.last_used.isoformat(),
                "last_health_check": session.last_health_check.isoformat() if session.last_health_check else None,
                "stuck": stuck,
                "stuck_reason": stuck_reason if stuck else None,
            })
        return result

    def remove_session(self, tenant: str, module: str):
        """Remove and disconnect a session."""
        key = self._key(tenant, module)
        with self._lock:
            if key in self.sessions:
                self.sessions[key].disconnect()
                del self.sessions[key]


# Global session manager
manager = SessionManager()


# Flask routes
@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})


@app.route("/modules", methods=["GET"])
def list_modules():
    """List supported modules."""
    return jsonify({
        "modules": list(MODULES.keys()),
        "details": {k: {"name": v["name"]} for k, v in MODULES.items()}
    })


@app.route("/sessions", methods=["GET"])
def list_sessions():
    """List all sessions (tenant:module)."""
    return jsonify({"sessions": manager.list_sessions()})


@app.route("/login", methods=["POST"])
def login():
    """Initiate authentication for a tenant/module."""
    data = request.get_json() or {}
    tenant = data.get("tenant")
    module = data.get("module", "exo")
    sharepoint_tenant = data.get("sharepoint_tenant")  # Optional: SharePoint tenant prefix
    account = data.get("account", "1")  # Which account to select for Azure (default: 1)
    conversation_id = data.get("conversation_id")  # MCP conversation tracking

    if not tenant:
        return jsonify({"success": False, "error": "tenant is required"}), 400
    if module not in MODULES:
        return jsonify({"success": False, "error": f"Unknown module: {module}"}), 400

    session = manager.get_session(tenant, module, sharepoint_tenant, conversation_id)
    # Set Azure account choice before connecting
    if module == "azure":
        session._azure_account_choice = str(account)
    success, result = session.connect()

    # Extract device code if present (handles ANSI escape codes from Azure)
    code_match = re.search(r"(?:code|enter the code)\s+(?:\x1b\[[0-9;]*m)?([A-Z0-9]{8,})(?:\x1b\[[0-9;]*m)?\s+to\s+(?:authenticate|proceed)", result, re.IGNORECASE)

    response = {
        "success": success,
        "result": result,
        "auth_pending": session.auth_pending,
    }

    if code_match:
        response["device_code"] = code_match.group(1)
        response["auth_url"] = "https://microsoft.com/devicelogin"

    return jsonify(response)


@app.route("/status", methods=["POST"])
def status():
    """Check connection status for a tenant/module."""
    data = request.get_json() or {}
    tenant = data.get("tenant")
    module = data.get("module", "exo")
    conversation_id = data.get("conversation_id")  # MCP conversation tracking

    if not tenant:
        return jsonify({"success": False, "error": "tenant is required"}), 400

    session = manager.get_session(tenant, module, conversation_id=conversation_id)
    # Always call check() - it handles auth_pending detection and connection verification
    connected = session.check()

    # Calculate auth duration if auth just completed
    auth_duration_seconds = None
    if session._was_pending and session.auth_completed_at and session.created_at:
        auth_duration_seconds = (session.auth_completed_at - session.created_at).total_seconds()

    response = {
        "success": True,
        "tenant": tenant,
        "module": module,
        "connected": connected,
        "auth_pending": session.auth_pending,
        "was_pending": session._was_pending,  # Auth just completed from pending state
        "conversation_id": session.conversation_id,
    }
    if auth_duration_seconds is not None:
        response["auth_duration_seconds"] = auth_duration_seconds

    # Reset was_pending flag after reporting
    session._was_pending = False

    return jsonify(response)


@app.route("/run", methods=["POST"])
def run_command():
    """Execute a PowerShell command."""
    data = request.get_json() or {}
    tenant = data.get("tenant")
    module = data.get("module", "exo")
    command = data.get("command")

    if not tenant:
        return jsonify({"success": False, "error": "tenant is required"}), 400
    if not command:
        return jsonify({"success": False, "error": "command is required"}), 400
    if module not in MODULES:
        return jsonify({"success": False, "error": f"Unknown module: {module}"}), 400

    session = manager.get_session(tenant, module)

    if not session.authenticated:
        return jsonify({
            "success": False,
            "error": "Not authenticated. Call /login first.",
            "connected": False,
        }), 401

    success, result = session.run(command)
    return jsonify({"success": success, "result": result})


@app.route("/disconnect", methods=["POST"])
def disconnect():
    """Disconnect a session."""
    data = request.get_json() or {}
    tenant = data.get("tenant")
    module = data.get("module", "exo")

    if not tenant:
        return jsonify({"success": False, "error": "tenant is required"}), 400

    manager.remove_session(tenant, module)
    return jsonify({"success": True, "message": f"Disconnected {tenant}:{module}"})


@app.route("/kill", methods=["POST"])
def kill_session():
    """Force kill a stuck session without graceful disconnect."""
    data = request.get_json() or {}
    tenant = data.get("tenant")
    module = data.get("module", "exo")

    if not tenant:
        return jsonify({"success": False, "error": "tenant is required"}), 400

    killed = manager.force_kill_session(tenant, module)
    return jsonify({
        "success": True,
        "killed": killed,
        "message": f"Force killed {tenant}:{module}" if killed else f"No session for {tenant}:{module}"
    })


@app.route("/watchdog", methods=["GET"])
def watchdog_status():
    """Get watchdog status and configuration."""
    return jsonify({
        "running": manager._watchdog_running,
        "config": {
            "interval_seconds": WATCHDOG_INTERVAL,
            "auth_pending_timeout": AUTH_PENDING_TIMEOUT,
            "idle_session_timeout": IDLE_SESSION_TIMEOUT,
            "stuck_process_timeout": STUCK_PROCESS_TIMEOUT,
        }
    })


def post_worker_init(worker):
    """Gunicorn hook: called after worker process is initialized."""
    logger.info(f"Worker {worker.pid} initialized, starting watchdog...")
    manager.start_watchdog()


if __name__ == "__main__":
    logger.info(f"Starting PowerShell Session Manager on {HOST}:{PORT}")
    logger.info(f"Supported modules: {list(MODULES.keys())}")

    # Use gunicorn in production, Flask dev server for debugging
    if os.getenv("PWSH_MANAGER_DEBUG", "false").lower() == "true":
        # Debug mode: start watchdog here (single process)
        manager.start_watchdog()
        app.run(host=HOST, port=PORT, debug=True)
    else:
        from gunicorn.app.base import BaseApplication

        class StandaloneApplication(BaseApplication):
            def __init__(self, app, options=None):
                self.options = options or {}
                self.application = app
                super().__init__()

            def load_config(self):
                for key, value in self.options.items():
                    if key in self.cfg.settings and value is not None:
                        self.cfg.set(key.lower(), value)

            def load(self):
                return self.application

        options = {
            "bind": f"{HOST}:{PORT}",
            "workers": 1,  # Single worker to maintain session state
            "threads": 4,  # Multiple threads so /health responds while /login blocks
            "timeout": 300,
            "accesslog": "-",
            "errorlog": "-",
            "post_worker_init": post_worker_init,  # Start watchdog in worker process
        }
        StandaloneApplication(app, options).run()
