#!/usr/bin/env python3
"""
M365 Session Pool - Native device code auth with PowerShell sessions.

All modules use their native device code flow - no MSAL token juggling.
"""

import json
import logging
import os
import re
import subprocess
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any, List
from flask import Flask, jsonify, request

# Metrics
class Metrics:
    def __init__(self):
        self.start_time = datetime.now()
        self.request_count = 0
        self.error_count = 0
        self.auth_count = 0
        self.response_times: List[float] = []  # Last 100 response times
        self.lock = threading.Lock()

    def record_request(self, duration: float, error: bool = False):
        with self.lock:
            self.request_count += 1
            if error:
                self.error_count += 1
            self.response_times.append(duration)
            if len(self.response_times) > 100:
                self.response_times.pop(0)

    def record_auth(self):
        with self.lock:
            self.auth_count += 1

    def get_stats(self) -> dict:
        with self.lock:
            uptime = (datetime.now() - self.start_time).total_seconds()
            avg_response = sum(self.response_times) / len(self.response_times) if self.response_times else 0
            return {
                "uptime_seconds": round(uptime, 1),
                "uptime_human": f"{int(uptime // 3600)}h {int((uptime % 3600) // 60)}m {int(uptime % 60)}s",
                "total_requests": self.request_count,
                "total_errors": self.error_count,
                "error_rate": round(self.error_count / self.request_count * 100, 2) if self.request_count else 0,
                "total_auths": self.auth_count,
                "avg_response_ms": round(avg_response * 1000, 1),
                "last_100_responses": len(self.response_times),
            }

metrics = Metrics()

# Configuration
HOST = os.getenv("SESSION_POOL_HOST", "0.0.0.0")
PORT = int(os.getenv("SESSION_POOL_PORT", "5200"))
# Single-connection mode: if set, only handle this connection
SINGLE_CONNECTION = os.getenv("M365_CONNECTION", "")
LOG_LEVEL = os.getenv("SESSION_POOL_LOG_LEVEL", "INFO")
COMMAND_TIMEOUT = int(os.getenv("COMMAND_TIMEOUT", "300"))
LOCK_TIMEOUT = COMMAND_TIMEOUT + 30  # How long to wait for process_lock before giving up

# Logging — dual output: stdout (docker logs) + persistent file (/app/logs/)
LOG_DIR = os.getenv("SESSION_POOL_LOG_DIR", "/app/logs")
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("session_pool")
logger.setLevel(getattr(logging, LOG_LEVEL))

_log_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

# Console handler (captured by docker logs)
_console = logging.StreamHandler()
_console.setFormatter(_log_fmt)
logger.addHandler(_console)

# File handler (persists on host via volume mount)
from logging.handlers import RotatingFileHandler
_log_file = os.path.join(LOG_DIR, f"session-pool-{SINGLE_CONNECTION or 'unified'}.log")
_file_handler = RotatingFileHandler(_log_file, maxBytes=10_000_000, backupCount=5)
_file_handler.setFormatter(_log_fmt)
logger.addHandler(_file_handler)

logger.info(f"Logging to stdout + {_log_file}")

# Connection registry
REGISTRY_PATH = os.path.expanduser("~/.m365-connections.json")

# Session state persistence — survives container restarts
STATE_DIR = os.getenv("SESSION_POOL_STATE_DIR", "/app/state")
os.makedirs(STATE_DIR, exist_ok=True)
STATE_FILE = os.path.join(STATE_DIR, f"sessions-{SINGLE_CONNECTION or 'unified'}.json")

# Module configurations - all use native device code
MODULES = {
    "exo": {
        "name": "Exchange Online",
        "connect_cmd": "Connect-ExchangeOnline -Device -ShowBanner:$false",
        "health_cmd": "Get-ConnectionInformation | Select-Object -First 1 | ConvertTo-Json",
        "health_pattern": r"(Organization|TenantId)",
        "device_code_pattern": r"code\s+([A-Z0-9]{8,})",
    },
    "azure": {
        "name": "Azure PowerShell",
        # Disable interactive subscription picker (Az.Accounts v2+ prompts for selection)
        "connect_cmd": "Update-AzConfig -LoginExperienceV2 Off -ErrorAction SilentlyContinue | Out-Null; Connect-AzAccount -UseDeviceAuthentication -TenantId {tenant_id}",
        "health_cmd": "(Get-AzContext) | Select-Object Name, Account | ConvertTo-Json",
        "health_pattern": r"(Name|Account)",
        "device_code_pattern": r"code\s+([A-Z0-9]{8,})",
    },
    "teams": {
        "name": "Microsoft Teams",
        "connect_cmd": "Connect-MicrosoftTeams -UseDeviceAuthentication -TenantId {tenant_id}",
        "health_cmd": "Get-CsTenant | Select-Object TenantId, DisplayName | ConvertTo-Json",
        "health_pattern": r"(TenantId|DisplayName)",
        "device_code_pattern": r"code\s+([A-Z0-9]{8,})",
    },
    "pnp": {
        "name": "PnP PowerShell",
        "connect_cmd": 'Connect-PnPOnline -Url "https://{sharepoint_host}.sharepoint.com" -DeviceLogin -ClientId "{app_id}" -Tenant "{tenant}"',
        "health_cmd": "Get-PnPConnection | Select-Object Url, ConnectionType | ConvertTo-Json",
        "health_pattern": r"(Url|ConnectionType)",
        "device_code_pattern": r"code\s+([A-Z0-9]{8,})",
    },
    # Power Platform disabled - PAC CLI device code only works with its first-party app
    # To re-enable, would need service principal auth (--applicationId + --clientSecret + --tenant)
    # "powerplatform": {
    #     "name": "Power Platform (PAC CLI)",
    #     "use_pac": True,
    #     "connect_cmd": "pac auth create --deviceCode --tenant {tenant_id}",
    #     "health_cmd": "pac auth list",
    #     "health_pattern": r"(UNIVERSAL|Active)",
    #     "device_code_pattern": r"code\s+([A-Z0-9]{8,})",
    # },
}

# Command guardrails — block commands that modify the container or escalate access
# Two tiers: BLOCKED (hard reject) and WARNED (logged at WARNING, still executes)
BLOCKED_PATTERNS = [
    # Container integrity — no installing/removing modules inside the container
    (r'\bInstall-Module\b', "Installing PowerShell modules modifies container state"),
    (r'\bUninstall-Module\b', "Uninstalling PowerShell modules modifies container state"),
    (r'\bUpdate-Module\b', "Updating PowerShell modules modifies container state"),
    (r'\bInstall-Package\b', "Installing packages modifies container state"),
    # Identity/access escalation
    (r'\bNew-AzRoleAssignment\b', "Creating role assignments is an access escalation"),
    (r'\bRemove-AzRoleAssignment\b', "Removing role assignments modifies access control"),
    (r'\bSet-AzKeyVaultAccessPolicy\b', "Modifying Key Vault access policies is an access escalation"),
    (r'\bRemove-AzKeyVaultAccessPolicy\b', "Removing Key Vault access policies modifies access control"),
    (r'\bNew-AzADApplication\b', "Creating app registrations is an access escalation"),
    (r'\bRemove-AzADApplication\b', "Deleting app registrations is destructive"),
    (r'\bNew-AzADServicePrincipal\b', "Creating service principals is an access escalation"),
    (r'\bRemove-AzADServicePrincipal\b', "Deleting service principals is destructive"),
    # Raw token crafting — no hand-rolling OAuth requests
    (r'Invoke-RestMethod.*login\.microsoftonline', "Direct OAuth token requests bypass auth guardrails"),
    (r'Invoke-WebRequest.*login\.microsoftonline', "Direct OAuth token requests bypass auth guardrails"),
]

WARNED_PATTERNS = [
    # Bulk deletions
    (r'\bRemove-Az\w+\b', "Azure resource deletion"),
    (r'\bRemove-Mailbox\b', "Mailbox deletion"),
    (r'\bRemove-PnP\w+\b', "SharePoint resource deletion"),
    (r'\bRemove-Team\b', "Teams deletion"),
    # Forwarding rules (common attack vector)
    (r'ForwardingSmtpAddress', "Mail forwarding modification"),
    (r'Set-InboxRule.*Forward', "Inbox forwarding rule modification"),
]


def check_command_guardrails(command: str, session_id: str) -> Optional[str]:
    """Check command against guardrails. Returns error message if blocked, None if allowed."""
    for pattern, reason in BLOCKED_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            logger.warning(f"[{session_id}] BLOCKED command: {reason} | {command[:120]}")
            return f"Command blocked: {reason}. This operation is not allowed through the session pool."

    for pattern, reason in WARNED_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            logger.warning(f"[{session_id}] WARNED command: {reason} | {command[:120]}")
            # Warned but not blocked — continues to execute

    return None


# Command marker for PowerShell output sync
MARKER = "___M365_DONE___"


def load_connection_registry() -> Dict[str, Any]:
    """Load connection registry."""
    try:
        if os.path.exists(REGISTRY_PATH):
            with open(REGISTRY_PATH) as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load registry: {e}")
    return {"connections": {}}


def get_connection_config(connection_name: str) -> Optional[Dict[str, Any]]:
    """Get config for a named connection."""
    registry = load_connection_registry()
    return registry.get("connections", {}).get(connection_name)


@dataclass
class Session:
    """A PowerShell session with native device code auth."""

    tenant: str
    module: str
    connection_name: str
    app_id: str

    # Process
    process: Optional[subprocess.Popen] = None
    process_lock: threading.Lock = field(default_factory=threading.Lock)

    # State
    state: str = "initializing"  # initializing, ready, auth_pending, authenticated, error
    device_code: Optional[str] = None
    auth_initiated_by: Optional[str] = None
    auth_started_at: float = 0.0

    # Identity
    authenticated_as: Optional[str] = None

    # Tracking
    last_command: Optional[datetime] = None
    last_error: Optional[str] = None

    # Callback when auth completes (set by pool for state persistence)
    on_auth_complete: Optional[object] = None

    def __post_init__(self):
        self.process_lock = threading.Lock()

    @property
    def session_id(self) -> str:
        return f"{self.connection_name}/{self.module}"

    def start_process(self) -> bool:
        """Start the PowerShell process."""
        try:
            module_config = MODULES.get(self.module)
            if not module_config:
                self.last_error = f"Unknown module: {self.module}"
                self.state = "error"
                return False

            if module_config.get("use_pac"):
                self.state = "ready"
                return True

            # Use stdbuf to force line-buffered stdout, or pwsh will buffer in non-interactive mode
            self.process = subprocess.Popen(
                ["stdbuf", "-oL", "pwsh", "-NoLogo", "-NoProfile", "-NoExit", "-Command", "-"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            self._send_raw('$ErrorActionPreference = "Continue"')
            self._send_raw('function prompt { "" }')

            # In unified mode, isolate Azure contexts to prevent cross-tenant contamination
            # 1. Disable autosave so this process won't overwrite shared ~/.Azure
            # 2. Select the correct context by matching the expected account email
            if self.module == "azure" and not SINGLE_CONNECTION:
                self._send_raw('Disable-AzContextAutosave -Scope Process -ErrorAction SilentlyContinue | Out-Null')
                conn_config = get_connection_config(self.connection_name)
                expected_email = conn_config.get("expectedEmail", "") if conn_config else ""
                if expected_email:
                    select_cmd = (
                        f'$ctx = Get-AzContext -ListAvailable | Where-Object {{ $_.Account.Id -eq "{expected_email}" }} | Select-Object -First 1; '
                        f'if ($ctx) {{ $ctx | Select-AzContext | Out-Null; Write-Host "Selected: $($ctx.Account.Id)" }} '
                        f'else {{ Write-Host "No context found for {expected_email}" }}'
                    )
                    result = self._send_raw(select_cmd, timeout=15)
                    logger.info(f"[{self.session_id}] Azure context isolation: {result.strip()}")
                else:
                    logger.info(f"[{self.session_id}] Azure context isolated (no expectedEmail to pin)")

            # Verify cached auth before falling through to ready state.
            # Docker volume tokens (e.g. ~/.Azure) survive container restarts,
            # so sessions can restore without a device code even when not in
            # the state file (e.g. container restarted during auth_pending).
            module_config = MODULES.get(self.module)
            if module_config and not module_config.get("use_pac"):
                try:
                    health_output = self._send_raw(module_config["health_cmd"], timeout=15)
                    if re.search(module_config["health_pattern"], health_output):
                        self.state = "authenticated"
                        self.authenticated_as = self._extract_identity(health_output)
                        logger.info(f"[{self.session_id}] Cached auth valid! Identity: {self.authenticated_as}")
                        logger.info(f"[{self.session_id}] PowerShell started (PID: {self.process.pid})")
                        if self.on_auth_complete:
                            try:
                                self.on_auth_complete()
                            except Exception:
                                pass
                        return True
                except Exception:
                    pass  # No cached auth, proceed to ready state

            self.state = "ready"
            logger.info(f"[{self.session_id}] PowerShell started (PID: {self.process.pid})")
            return True

        except Exception as e:
            self.last_error = str(e)
            self.state = "error"
            logger.error(f"[{self.session_id}] Failed to start: {e}")
            return False

    def _send_raw(self, command: str, timeout: int = 30) -> str:
        """Send command and read output until marker."""
        import select

        if not self.process or self.process.poll() is not None:
            raise RuntimeError("Process not running")

        # Force flush stdout after command to prevent buffering issues
        full_cmd = f'{command}; [Console]::Out.Flush(); Write-Host "{MARKER}"\n'
        logger.info(f"[{self.session_id}] Sending command: {command[:80]}")
        self.process.stdin.write(full_cmd)
        self.process.stdin.flush()

        output_lines = []
        start = time.time()

        while True:
            if time.time() - start > timeout:
                logger.error(f"[{self.session_id}] Command timed out after {timeout}s. Output so far: {output_lines[:5]}")
                raise TimeoutError(f"Command timed out after {timeout}s")

            # Use select to avoid blocking indefinitely on readline
            ready, _, _ = select.select([self.process.stdout], [], [], 1.0)
            if not ready:
                continue  # No data yet, loop back to check timeout

            line = self.process.stdout.readline()
            if not line:
                time.sleep(0.01)
                continue

            line = line.rstrip('\n\r')
            logger.debug(f"[{self.session_id}] Output line: {line[:100]}")
            if MARKER in line:
                break
            output_lines.append(line)

        logger.info(f"[{self.session_id}] Command completed, {len(output_lines)} lines")
        return '\n'.join(output_lines)

    def initiate_auth(self, caller_id: str) -> Dict[str, Any]:
        """Start native device code authentication.

        The auth reader thread owns stdout exclusively through the entire auth lifecycle:
        1. Sends connect command, captures device code
        2. Waits for auth completion (MARKER)
        3. Runs health check to verify and transition to authenticated state
        This avoids the non-thread-safe TextIOWrapper corruption from multiple readers.
        """
        module_config = MODULES.get(self.module)
        conn_config = get_connection_config(self.connection_name)

        self.state = "auth_pending"
        self.auth_initiated_by = caller_id
        self.auth_started_at = time.time()
        self.device_code = None

        try:
            # Build connect command with placeholders
            sharepoint_host = conn_config.get("sharepoint_host", self.tenant.split('.')[0]) if conn_config else self.tenant.split('.')[0]
            tenant_id = conn_config.get("tenantId", self.tenant) if conn_config else self.tenant
            connect_cmd = module_config["connect_cmd"].format(
                tenant=self.tenant,
                tenant_id=tenant_id,
                sharepoint_host=sharepoint_host,
                app_id=self.app_id,
            )

            logger.info(f"[{self.session_id}] Starting auth: {connect_cmd}")

            if module_config.get("use_pac"):
                return self._initiate_pac_auth(connect_cmd, module_config)

            device_code = None
            output_buffer = []

            def reader_thread():
                """Owns stdout exclusively: auth -> health check -> state transition."""
                nonlocal device_code
                try:
                    # Phase 1: Send connect command, capture device code, wait for auth
                    self.process.stdin.write(f'{connect_cmd}; Write-Host "{MARKER}"\n')
                    self.process.stdin.flush()

                    start = time.time()
                    while time.time() - start < 900:  # 15 min = device code lifetime
                        line = self.process.stdout.readline()
                        if not line:
                            time.sleep(0.1)
                            continue

                        line = line.strip()
                        output_buffer.append(line)
                        logger.info(f"[{self.session_id}] Auth output: {line[:100]}")

                        if not device_code:
                            # Strip ANSI escape codes before matching — Azure wraps output in color codes
                            clean_line = re.sub(r'\x1b\[[\?0-9;]*[a-zA-Z]', '', line)
                            match = re.search(module_config["device_code_pattern"], clean_line, re.IGNORECASE)
                            if match:
                                device_code = match.group(1)
                                self.device_code = device_code
                                logger.info(f"[{self.session_id}] Device code: {device_code}")

                        if MARKER in line:
                            if device_code:
                                logger.info(f"[{self.session_id}] Auth connect completed, running health check...")
                            break

                    if not device_code:
                        self.state = "error"
                        self.last_error = "No device code / auth timed out"
                        logger.error(f"[{self.session_id}] Auth reader exiting: no device code after {time.time()-start:.0f}s")
                        return

                    # Phase 2: Health check (still on this thread — exclusive stdout access)
                    try:
                        health_output = self._send_raw(module_config["health_cmd"], timeout=30)
                        logger.info(f"[{self.session_id}] Post-auth health ({len(health_output)} chars): {health_output[:200]}")

                        if re.search(module_config["health_pattern"], health_output):
                            self.state = "authenticated"
                            self.device_code = None
                            self.auth_initiated_by = None
                            self.authenticated_as = self._extract_identity(health_output)
                            logger.info(f"[{self.session_id}] Auth completed! Identity: {self.authenticated_as}")

                            # Notify pool to persist state
                            if self.on_auth_complete:
                                try:
                                    self.on_auth_complete()
                                except Exception as e:
                                    logger.warning(f"[{self.session_id}] on_auth_complete callback failed: {e}")
                        else:
                            logger.warning(f"[{self.session_id}] Health pattern not matched, staying in auth_pending")
                    except Exception as e:
                        logger.error(f"[{self.session_id}] Post-auth health check failed: {e}")

                except Exception as e:
                    logger.error(f"[{self.session_id}] Auth reader thread error: {e}")

            reader = threading.Thread(target=reader_thread, daemon=True)
            reader.start()

            # Wait for device code to appear (up to 30 sec)
            for _ in range(60):
                if self.device_code:
                    break
                time.sleep(0.5)

            if self.device_code:
                return {
                    "status": "auth_required",
                    "device_code": self.device_code,
                    "auth_url": "https://microsoft.com/devicelogin",
                    "message": f"To sign in, use a web browser to open https://microsoft.com/devicelogin and enter the code {self.device_code}",
                    "expires_in": 900,
                }
            else:
                self.state = "error"
                self.last_error = "No device code received"
                return {"status": "error", "error": "No device code received", "output": '\n'.join(output_buffer[:10])}

        except Exception as e:
            self.state = "error"
            self.last_error = str(e)
            logger.error(f"[{self.session_id}] Auth initiation failed: {e}")
            return {"status": "error", "error": str(e)}

    def _initiate_pac_auth(self, connect_cmd: str, module_config: dict) -> Dict[str, Any]:
        """Handle PAC CLI auth - uses Popen since PAC waits for auth completion."""
        try:
            # Start PAC in background - it blocks until auth completes
            self.process = subprocess.Popen(
                connect_cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            # Read output until we find device code (or timeout)
            output_lines = []
            start = time.time()
            while time.time() - start < 30:
                line = self.process.stdout.readline()
                if not line:
                    if self.process.poll() is not None:
                        break
                    time.sleep(0.1)
                    continue

                output_lines.append(line.strip())
                logger.info(f"[{self.session_id}] PAC output: {line.strip()}")

                match = re.search(module_config["device_code_pattern"], line, re.IGNORECASE)
                if match:
                    self.device_code = match.group(1)
                    return {
                        "status": "auth_required",
                        "device_code": self.device_code,
                        "auth_url": "https://microsoft.com/devicelogin",
                        "message": f"Complete PAC CLI auth with code {self.device_code}",
                        "expires_in": 900,
                    }

            return {"status": "error", "error": "No device code in PAC output", "output": '\n'.join(output_lines)}

        except Exception as e:
            return {"status": "error", "error": str(e)}

    def _extract_identity(self, health_output: str) -> Optional[str]:
        """Extract authenticated identity from health check output."""
        clean = re.sub(r'\x1b\[[\?0-9;]*[a-zA-Z]', '', health_output).strip()
        try:
            data = json.loads(clean)
            if self.module == "azure":
                acct = data.get("Account", {})
                return acct.get("Id") if isinstance(acct, dict) else str(acct)
            elif self.module == "exo":
                return data.get("UserPrincipalName") or data.get("Organization")
            elif self.module == "teams":
                return data.get("DisplayName")
            elif self.module == "pnp":
                return data.get("Url")
        except (json.JSONDecodeError, AttributeError):
            pass
        return None

    def check_auth_complete(self) -> bool:
        """Check if the auth reader thread has transitioned state to authenticated.

        The reader thread handles the full lifecycle (auth + health check + state transition).
        This method just checks the result — no stdout access, no thread-safety issues.
        """
        if self.module and MODULES.get(self.module, {}).get("use_pac"):
            return self._check_pac_auth()

        is_done = self.state == "authenticated"
        logger.info(f"[{self.session_id}] check_auth_complete: state={self.state}, authenticated={is_done}")
        return is_done

    def _check_pac_auth(self) -> bool:
        """Check if PAC auth completed."""
        try:
            result = subprocess.run("pac auth list", shell=True, capture_output=True, text=True, timeout=10)
            if "Active" in result.stdout or "UNIVERSAL" in result.stdout:
                self.state = "authenticated"
                self.device_code = None
                logger.info(f"[{self.session_id}] PAC auth completed")
                return True
        except:
            pass
        return False

    def run_command(self, command: str, caller_id: str, timeout: int = COMMAND_TIMEOUT) -> Dict[str, Any]:
        """Execute a command."""
        module_config = MODULES.get(self.module)
        logger.info(f"[{self.session_id}] run_command called, state={self.state}, caller={caller_id}")

        # Wait briefly if session is still starting up (start_process running outside pool lock)
        if self.state == "initializing":
            for _ in range(30):  # Wait up to 30 seconds
                time.sleep(1)
                if self.state != "initializing":
                    break
            if self.state == "initializing":
                return {"status": "error", "error": "Session startup timed out. Retry in a moment."}

        # Check auth state
        if self.state == "auth_pending":
            auth_age = time.time() - self.auth_started_at
            logger.info(f"[{self.session_id}] State is auth_pending, age={int(auth_age)}s, device_code={self.device_code}")

            # Check if auth has gone stale (device code expired after 15 min)
            if auth_age > 900:
                logger.warning(f"[{self.session_id}] Auth stale after {int(auth_age)}s, resetting session")
                self.stop()
                self.start_process()
                return self.initiate_auth(caller_id)

            # Any caller can check if auth completed (stateless MCP = new caller_id each time)
            logger.info(f"[{self.session_id}] Checking if auth completed...")
            if self.check_auth_complete():
                logger.info(f"[{self.session_id}] Auth check passed, proceeding to execute command")
            else:
                logger.info(f"[{self.session_id}] Auth check failed, returning auth_required")
                return {
                    "status": "auth_required",
                    "device_code": self.device_code,
                    "auth_url": "https://microsoft.com/devicelogin",
                    "message": "Complete device code auth, then retry",
                }

        if self.state != "authenticated":
            logger.info(f"[{self.session_id}] State is {self.state}, initiating auth")
            return self.initiate_auth(caller_id)

        # Guardrails — check command before executing
        blocked = check_command_guardrails(command, self.session_id)
        if blocked:
            return {"status": "error", "error": blocked}

        # Execute command
        logger.info(f"[{self.session_id}] Executing command (state=authenticated)")
        if not self.process_lock.acquire(timeout=LOCK_TIMEOUT):
            logger.error(f"[{self.session_id}] Lock timeout after {LOCK_TIMEOUT}s — another command is stuck")
            self.state = "error"
            self.last_error = "Lock timeout — session stuck"
            if self.process:
                try:
                    self.process.kill()
                except Exception:
                    pass
            return {"status": "error", "error": "Session busy (another command hung). Session reset — retry will create a fresh session."}
        try:
            # Check process is alive before sending anything
            if self.process and self.process.poll() is not None:
                logger.error(f"[{self.session_id}] Process dead (exit code {self.process.returncode}), marking error")
                self.state = "error"
                self.last_error = f"Process exited with code {self.process.returncode}"
                return {"status": "error", "error": "PowerShell process died. Session reset — retry will create a fresh session."}

            if module_config.get("use_pac"):
                result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
                output = result.stdout + result.stderr
            else:
                output = self._send_raw(command, timeout=timeout)

            self.last_command = datetime.now()
            response = {"status": "success", "output": output}
            if self.authenticated_as:
                response["authenticated_as"] = self.authenticated_as

            # Log output content — always preview, flag errors at WARNING
            output_preview = output[:500].replace('\n', ' | ') if output else '(empty)'
            error_patterns = re.search(r'(AADSTS\d+|error|exception|unauthorized|forbidden|access.denied)', output, re.IGNORECASE)
            if error_patterns:
                logger.warning(f"[{self.session_id}] Command output contains error pattern [{error_patterns.group(0)}]: {output_preview}")
            else:
                logger.info(f"[{self.session_id}] Command succeeded ({len(output)} chars): {output_preview}")
            return response

        except TimeoutError as e:
            # Kill the hung process — subsequent commands to a stuck process
            # will also hang, cascading into total thread starvation
            logger.error(f"[{self.session_id}] Command timed out, killing process: {e}")
            self.state = "error"
            self.last_error = f"Timeout: {e}"
            if self.process:
                try:
                    self.process.kill()
                except Exception:
                    pass
            return {"status": "error", "error": f"Timeout: {e}. Session reset — retry will create a fresh session."}
        except Exception as e:
            self.last_error = str(e)
            logger.error(f"[{self.session_id}] Command failed: {e}")
            return {"status": "error", "error": str(e)}
        finally:
            self.process_lock.release()

    def stop(self):
        """Stop the session."""
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except:
                self.process.kill()
        self.state = "stopped"


class SessionPool:
    """Manages pool of sessions."""

    def __init__(self):
        self.sessions: Dict[str, Session] = {}
        self.lock = threading.Lock()
        self.restoring = False
        # Restore in background so Flask starts serving immediately
        threading.Thread(target=self._restore_sessions_background, daemon=True).start()

    def _restore_sessions_background(self):
        """Run restore in background thread so HTTP server starts immediately."""
        self.restoring = True
        try:
            self._restore_sessions()
        finally:
            self.restoring = False

    def save_state(self):
        """Persist session metadata to disk so we can restore after restart."""
        with self.lock:
            state = []
            for session in self.sessions.values():
                if session.state == "authenticated":
                    state.append({
                        "connection_name": session.connection_name,
                        "module": session.module,
                        "tenant": session.tenant,
                        "app_id": session.app_id,
                        "authenticated_as": session.authenticated_as,
                        "saved_at": time.time(),
                    })
        try:
            with open(STATE_FILE, 'w') as f:
                json.dump(state, f, indent=2)
            logger.info(f"Saved {len(state)} session(s) to {STATE_FILE}")
        except Exception as e:
            logger.error(f"Failed to save session state: {e}")

    def _restore_sessions(self):
        """Restore sessions from saved state on startup.

        PowerShell module tokens are persisted in Docker volumes (.Azure, .config, .local).
        We re-launch pwsh and run a health check — if the cached token is still valid,
        the session comes back authenticated without needing a device code.
        """
        if not os.path.exists(STATE_FILE):
            return

        try:
            with open(STATE_FILE) as f:
                saved = json.load(f)
        except Exception as e:
            logger.warning(f"Could not load saved state: {e}")
            return

        if not saved:
            return

        logger.info(f"Restoring {len(saved)} session(s) from saved state...")

        for entry in saved:
            conn_name = entry["connection_name"]
            module = entry["module"]
            session_id = f"{conn_name}/{module}"

            # Skip if module not configured
            module_config = MODULES.get(module)
            if not module_config or module_config.get("use_pac"):
                logger.info(f"[{session_id}] Skipping restore — module not supported for restore")
                continue

            try:
                session = Session(
                    tenant=entry["tenant"],
                    module=module,
                    connection_name=conn_name,
                    app_id=entry["app_id"],
                )

                if not session.start_process():
                    logger.warning(f"[{session_id}] Restore failed — could not start pwsh")
                    continue

                # Run health check — if cached tokens are valid, this succeeds
                logger.info(f"[{session_id}] Verifying cached auth tokens...")
                try:
                    health_output = session._send_raw(module_config["health_cmd"], timeout=30)
                    if re.search(module_config["health_pattern"], health_output):
                        session.state = "authenticated"
                        session.authenticated_as = session._extract_identity(health_output) or entry.get("authenticated_as")
                        session.on_auth_complete = self.save_state
                        self.sessions[session_id] = session
                        logger.info(f"[{session_id}] Restored! Identity: {session.authenticated_as}")
                    else:
                        session.stop()
                        logger.info(f"[{session_id}] Cached tokens expired — will re-auth on next use")
                except Exception as e:
                    session.stop()
                    logger.info(f"[{session_id}] Restore health check failed: {e} — will re-auth on next use")

            except Exception as e:
                logger.warning(f"[{session_id}] Restore error: {e}")

    def get_or_create_session(self, connection_name: str, module: str) -> Session:
        """Get or create a session.

        IMPORTANT: start_process() runs OUTSIDE the pool lock to prevent
        global thread starvation. PowerShell startup + health checks do I/O
        that can hang — holding the pool lock during that would block ALL
        connections, not just the one being created.
        """
        session_id = f"{connection_name}/{module}"
        new_session = None

        with self.lock:
            # Evict dead sessions (killed after timeout or process crash)
            existing = self.sessions.get(session_id)
            if existing and existing.state == "error":
                logger.info(f"[{session_id}] Evicting errored session, will recreate")
                existing.stop()
                del self.sessions[session_id]

            if session_id in self.sessions:
                return self.sessions[session_id]

            # Create session object and register it BEFORE starting the process.
            # State is "initializing" so other threads won't try to use it yet.
            registry = load_connection_registry()
            conn_config = registry.get("connections", {}).get(connection_name)
            if not conn_config:
                raise ValueError(f"Connection '{connection_name}' not found")

            known_module_apps = registry.get("_knownModuleApps", {})
            module_apps = conn_config.get("moduleApps", {})
            app_id = module_apps.get(module) or known_module_apps.get(module) or conn_config.get("appId", "")

            logger.info(f"[{session_id}] Using app_id: {app_id}")

            new_session = Session(
                tenant=conn_config.get("tenant", ""),
                module=module,
                connection_name=connection_name,
                app_id=app_id,
            )
            new_session.on_auth_complete = self.save_state
            self.sessions[session_id] = new_session
            # Pool lock released here — start_process() runs outside

        # Start process OUTSIDE pool lock — this does I/O (pwsh startup,
        # Azure context isolation, cached auth health check) that can hang.
        # Only blocks this session, not the entire pool.
        try:
            new_session.start_process()
            logger.info(f"Created session: {session_id}")
        except Exception as e:
            logger.error(f"[{session_id}] start_process failed: {e}")
            with self.lock:
                if session_id in self.sessions and self.sessions[session_id] is new_session:
                    del self.sessions[session_id]
            raise

        return new_session

    def run_command(self, connection_name: str, module: str, command: str, caller_id: str) -> Dict[str, Any]:
        """Run a command."""
        try:
            session = self.get_or_create_session(connection_name, module)
            return session.run_command(command, caller_id)
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def reset_connection(self, connection_name: str, module: str = None) -> Dict[str, Any]:
        """Reset sessions for a connection. If module specified, reset only that session."""
        with self.lock:
            reset = []
            to_remove = []
            for session_id, session in self.sessions.items():
                if session.connection_name == connection_name:
                    if module and session.module != module:
                        continue
                    session.stop()
                    to_remove.append(session_id)
                    reset.append(session_id)
            for sid in to_remove:
                del self.sessions[sid]

        # Save state outside lock to avoid deadlock
        if reset:
            self.save_state()
        return {"reset": reset, "count": len(reset)}

    def get_status(self) -> Dict[str, Any]:
        """Get status of all sessions."""
        with self.lock:
            result = {
                "sessions": [
                    {
                        "session_id": s.session_id,
                        "state": s.state,
                        "authenticated": s.state == "authenticated",
                        "last_command": s.last_command.isoformat() if s.last_command else None,
                    }
                    for s in self.sessions.values()
                ]
            }
            if self.restoring:
                result["restoring"] = True
            return result


# Global pool
pool = SessionPool()


# Keepalive thread - pings authenticated sessions to prevent token expiry
class SessionKeepalive:
    def __init__(self, pool: SessionPool, interval: int = 300):
        self.pool = pool
        self.interval = interval  # seconds between keepalive pings
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.last_ping = {}
        self.ping_count = 0
        self.ping_failures = 0

    def start(self):
        self.thread.start()
        logger.info(f"Keepalive started (interval: {self.interval}s)")

    def stop(self):
        self.running = False

    def _run(self):
        while self.running:
            time.sleep(self.interval)
            self._reap_stale_sessions()
            self._ping_sessions()

    def _reap_stale_sessions(self):
        """Clean up sessions stuck in auth_pending or error for too long."""
        with self.pool.lock:
            stale = []
            for session_id, session in self.pool.sessions.items():
                if session.state in ("auth_pending", "error"):
                    auth_age = time.time() - getattr(session, 'auth_started_at', time.time())
                    if auth_age > 900:  # 15 min = device code expiry
                        stale.append(session_id)

            for sid in stale:
                session = self.pool.sessions.pop(sid)
                session.stop()
                logger.info(f"[{sid}] Reaped stale session (state={session.state})")

    def _ping_sessions(self):
        with self.pool.lock:
            sessions = list(self.pool.sessions.values())

        for session in sessions:
            if session.state != "authenticated":
                continue

            try:
                module_config = MODULES.get(session.module)
                if not module_config or module_config.get("use_pac"):
                    continue

                # Non-blocking lock — skip sessions with a command in progress
                if not session.process_lock.acquire(blocking=False):
                    logger.debug(f"[{session.session_id}] Keepalive skipped — command in progress")
                    continue
                try:
                    health_output = session._send_raw(module_config["health_cmd"], timeout=30)
                finally:
                    session.process_lock.release()

                if re.search(module_config["health_pattern"], health_output):
                    self.last_ping[session.session_id] = datetime.now()
                    self.ping_count += 1
                    logger.debug(f"[{session.session_id}] Keepalive OK")
                else:
                    self.ping_failures += 1
                    logger.warning(f"[{session.session_id}] Keepalive failed: {health_output[:100]}")

            except Exception as e:
                self.ping_failures += 1
                logger.error(f"[{session.session_id}] Keepalive error: {e}")

    def get_stats(self) -> dict:
        return {
            "interval_seconds": self.interval,
            "total_pings": self.ping_count,
            "failures": self.ping_failures,
            "last_pings": {k: v.isoformat() for k, v in self.last_ping.items()},
        }


# Start keepalive (5 min interval)
keepalive = SessionKeepalive(pool, interval=300)
keepalive.start()

# Flask app
app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})


@app.route("/status", methods=["GET"])
def status():
    return jsonify(pool.get_status())


@app.route("/connections", methods=["GET"])
def list_connections():
    registry = load_connection_registry()
    return jsonify({"connections": registry.get("connections", {})})


@app.route("/run", methods=["POST"])
def run_command():
    start = time.time()
    data = request.get_json()
    module = data.get("module")
    command = data.get("command")
    caller_id = data.get("caller_id", "anonymous")

    # Single-connection mode: use env var, ignore request connection
    if SINGLE_CONNECTION:
        connection = SINGLE_CONNECTION
    else:
        connection = data.get("connection")

    logger.info(f"[HTTP] POST /run connection={connection} module={module} caller={caller_id} command={command[:60] if command else 'None'}")

    if not all([connection, module, command]):
        metrics.record_request(time.time() - start, error=True)
        logger.warning(f"[HTTP] POST /run rejected: missing params (connection={connection}, module={module}, command={'yes' if command else 'None'})")
        return jsonify({"status": "error", "error": "Missing connection, module, or command"}), 400

    # In single-connection mode, reject requests for other connections
    if SINGLE_CONNECTION and connection != SINGLE_CONNECTION:
        metrics.record_request(time.time() - start, error=True)
        return jsonify({"status": "error", "error": f"This container only handles {SINGLE_CONNECTION}"}), 400

    if module not in MODULES:
        metrics.record_request(time.time() - start, error=True)
        return jsonify({"status": "error", "error": f"Unknown module: {module}"}), 400

    result = pool.run_command(connection, module, command, caller_id)
    elapsed = time.time() - start
    is_error = result.get("status") == "error"
    is_auth = result.get("status") == "auth_required"
    metrics.record_request(elapsed, error=is_error)
    if is_auth:
        metrics.record_auth()
    log_level = "warning" if is_error else "info"
    getattr(logger, log_level)(f"[HTTP] POST /run connection={connection} module={module} -> status={result.get('status')} elapsed={elapsed:.1f}s")
    if is_error:
        logger.warning(f"[HTTP] POST /run error detail: {result.get('error', 'unknown')}")
    return jsonify(result)


@app.route("/reset", methods=["POST"])
def reset_connection():
    """Reset (kill + remove) sessions for a specific connection."""
    data = request.get_json()
    connection = data.get("connection")
    module = data.get("module")  # Optional: reset only one module

    if not connection:
        return jsonify({"status": "error", "error": "Missing connection parameter"}), 400

    result = pool.reset_connection(connection, module)
    logger.info(f"Reset connection {connection} (module={module}): {result}")
    return jsonify({"status": "success", **result})


@app.route("/metrics", methods=["GET"])
def get_metrics():
    stats = metrics.get_stats()
    stats["sessions"] = pool.get_status()["sessions"]
    stats["active_sessions"] = len([s for s in stats["sessions"] if s["authenticated"]])
    stats["keepalive"] = keepalive.get_stats()
    return jsonify(stats)


if __name__ == "__main__":
    logger.info(f"Starting M365 Session Pool on {HOST}:{PORT}")
    app.run(host=HOST, port=PORT, threaded=True)
