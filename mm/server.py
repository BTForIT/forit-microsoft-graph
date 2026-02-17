#!/usr/bin/env python3
"""
mm MCP - Microsoft Master. Unified Microsoft 365 management.

Tools:
  run            - PowerShell commands via session pool (Exchange, SharePoint, Azure, Teams)
  graph_request  - Direct Microsoft Graph REST API calls via MSAL

Connection registry (~/.m365-connections.json) is READ-ONLY.
Connections must be pre-created by the user - MCPs cannot modify the registry.
"""

import json
import os
import re
import sys
import time
from pathlib import Path
import httpx
import msal
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Shared logger
sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from mcp_logger import log_tool_call
except ImportError:
    def log_tool_call(*args, **kwargs): pass

# Session pool endpoint (for PowerShell)
SESSION_POOL_URL = os.getenv("MM_SESSION_POOL_URL", "http://localhost:5200")

# Connection registry (READ-ONLY)
CONNECTIONS_FILE = Path.home() / ".m365-connections.json"

# Graph token cache directory
GRAPH_TOKEN_DIR = Path.home() / ".mm-graph-tokens"

# Default Graph scopes - broad enough for most operations
GRAPH_SCOPES = [
    "Mail.ReadWrite",
    "Calendars.ReadWrite",
    "Files.ReadWrite.All",
    "Sites.ReadWrite.All",
    "User.Read",
    "User.ReadBasic.All",
    "Contacts.ReadWrite",
    "Tasks.ReadWrite",
    "Notes.ReadWrite.All",
    "Chat.ReadWrite",
    "Team.ReadBasic.All",
    "Channel.ReadBasic.All",
    "ChannelMessage.Send",
]

# Sentinel: guard hook blocked execution (body can legitimately be None on some endpoints)
_GRAPH_BLOCKED = object()

# Send guards: env var global toggle, per-connection override via "skipSendGuards" in registry
_SEND_GUARDS_DEFAULT = os.getenv("MM_SEND_GUARDS", "true").lower() != "false"


def _send_guards_enabled(conn_config: dict) -> bool:
    """Check if send guards are enabled. Per-connection overrides global."""
    per_conn = conn_config.get("skipSendGuards")
    if per_conn is not None:
        return not per_conn  # skipSendGuards: true means guards disabled
    return _SEND_GUARDS_DEFAULT

# Email signature patterns — CodeTwo adds signatures on most accounts.
# Connections with "skipSignatureStrip": true in the registry are excluded.
# Strip these before sending so CodeTwo doesn't double-sign.
EMAIL_SIG_PATTERNS = [
    r'(?i)best regards',
    r'(?i)kind regards',
    r'(?i)sincerely',
    r'(?i)thanks,?\s*\n',
    r'(?i)thank you,?\s*\n',
    r'(?i)cheers,?\s*\n',
    r'(?i)warm regards',
    r'(?i)respectfully',
    r'(?i)sent from my',
    r'\n--\s*\n',  # standard sig separator
]

# Resource configurations for different Microsoft APIs
RESOURCE_CONFIGS = {
    "graph": {
        "base_url": "https://graph.microsoft.com",
        "scopes": GRAPH_SCOPES,
    },
    "flow": {
        "base_url": "https://api.flow.microsoft.com",
        "scopes": ["https://service.flow.microsoft.com/.default"],
    },
}


def load_registry() -> dict:
    """Load connection registry. Read-only - never writes."""
    try:
        return json.loads(CONNECTIONS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"connections": {}}


def get_connection_config(connection: str) -> tuple:
    """Get connection config. Returns (config, error_text)."""
    registry = load_registry()
    conn_config = registry.get("connections", {}).get(connection)
    if not conn_config:
        available = list(registry.get("connections", {}).keys())
        return None, f"Error: Connection '{connection}' not found.\nAvailable: {', '.join(available)}"
    return conn_config, None


# === MSAL Graph Auth ===

def _get_token_cache_path(connection: str) -> Path:
    """Get file path for a connection's MSAL token cache."""
    GRAPH_TOKEN_DIR.mkdir(exist_ok=True)
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', connection)
    return GRAPH_TOKEN_DIR / f"{safe_name}.json"


def _get_msal_app(connection: str, conn_config: dict) -> msal.PublicClientApplication:
    """Create MSAL app with persistent token cache for a connection."""
    app_id = conn_config.get("appId")
    tenant = conn_config.get("tenantId") or conn_config.get("tenant", "common")

    # If tenant is a domain (e.g. contoso.com), use it as-is - MSAL handles it
    authority = f"https://login.microsoftonline.com/{tenant}"

    cache = msal.SerializableTokenCache()
    cache_path = _get_token_cache_path(connection)
    if cache_path.exists():
        cache.deserialize(cache_path.read_text())

    app = msal.PublicClientApplication(
        client_id=app_id,
        authority=authority,
        token_cache=cache,
    )
    return app, cache, cache_path


def _save_cache(cache: msal.SerializableTokenCache, cache_path: Path):
    """Persist token cache if changed."""
    if cache.has_state_changed:
        cache_path.write_text(cache.serialize())


# Pending device code flows — persisted to disk so they survive process restarts
# (MCPJungle stateless mode spawns a new process per call)
GRAPH_FLOWS_DIR = GRAPH_TOKEN_DIR  # Reuse the same directory


def _get_flow_path(connection: str, resource: str = "graph") -> Path:
    """Get file path for a connection's pending device code flow."""
    GRAPH_FLOWS_DIR.mkdir(exist_ok=True)
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', connection)
    suffix = f"-{resource}" if resource != "graph" else ""
    return GRAPH_FLOWS_DIR / f"{safe_name}{suffix}.flow.json"


def _save_pending_flow(connection: str, flow: dict, device_code: str, resource: str = "graph"):
    """Persist a pending device code flow to disk."""
    flow_path = _get_flow_path(connection, resource)
    flow_path.write_text(json.dumps({"flow": flow, "code": device_code}))


def _load_pending_flow(connection: str, resource: str = "graph") -> dict | None:
    """Load a pending device code flow from disk. Returns None if not found or expired."""
    flow_path = _get_flow_path(connection, resource)
    if not flow_path.exists():
        return None
    try:
        data = json.loads(flow_path.read_text())
        flow = data.get("flow", {})
        # Check if the flow has expired
        expires_at = flow.get("expires_at", 0)
        if time.time() > expires_at:
            flow_path.unlink(missing_ok=True)
            return None
        return data
    except (json.JSONDecodeError, KeyError):
        flow_path.unlink(missing_ok=True)
        return None


def _clear_pending_flow(connection: str, resource: str = "graph"):
    """Remove a pending flow from disk."""
    _get_flow_path(connection, resource).unlink(missing_ok=True)


def _check_account_mismatch(result: dict, conn_config: dict,
                            cache: msal.SerializableTokenCache, cache_path: Path) -> dict | None:
    """Check if authenticated account matches expectedEmail. Returns error dict or None."""
    expected = conn_config.get("expectedEmail", "")
    if not expected:
        return None

    # MSAL returns id_token_claims with the account info
    claims = result.get("id_token_claims", {})
    actual_email = (
        claims.get("preferred_username")
        or claims.get("upn")
        or claims.get("email")
        or ""
    )

    if actual_email and expected.lower() != actual_email.lower():
        # Wrong account — nuke the cache so it doesn't persist
        cache_path.unlink(missing_ok=True)
        # Log the details, don't expose to the AI
        log_tool_call(
            mcp_name="mm", tool_name="graph_auth",
            arguments={"connection": conn_config.get("description", "")},
            error=f"Account mismatch: expected {expected}, got {actual_email}",
            duration_ms=0,
        )
        return {
            "error": "Authentication failed: wrong account. Token cache cleared. Retry the command."
        }
    return None


def _sanitize_auth_error(raw_error: str, connection: str) -> str:
    """Strip tenant/org details from Azure AD errors. Log raw, return generic."""
    log_tool_call(
        mcp_name="mm", tool_name="graph_auth",
        arguments={"connection": connection},
        error=f"Azure AD error: {raw_error}",
        duration_ms=0,
    )
    # Map known error codes to safe messages
    if "AADSTS700016" in raw_error:
        return "Authentication failed: app not found in this tenant. Check app registration and admin consent."
    if "AADSTS65001" in raw_error:
        return "Authentication failed: admin consent required. An admin must grant consent for this app."
    if "AADSTS50011" in raw_error:
        return "Authentication failed: reply URL mismatch. Check app registration redirect URIs."
    if "AADSTS7000218" in raw_error:
        return "Authentication failed: request must include client_secret or client_assertion. App may need reconfiguration."
    if "AADSTS" in raw_error:
        # Generic catch-all for any AADSTS error — never expose the raw message
        code = re.search(r'AADSTS\d+', raw_error)
        code_str = code.group(0) if code else "unknown"
        return f"Authentication failed ({code_str}). Check logs for details."
    # Non-AADSTS errors — still don't expose raw text
    return "Authentication failed. Check logs for details."


def _acquire_graph_token(connection: str, conn_config: dict,
                         scopes: list = None, resource: str = "graph") -> dict:
    """
    Acquire a token for a connection.

    Returns dict with either:
      {"access_token": "..."} on success
      {"device_code": "...", "message": "..."} when auth needed
      {"error": "..."} on failure
    """
    effective_scopes = scopes or GRAPH_SCOPES

    try:
        app, cache, cache_path = _get_msal_app(connection, conn_config)
    except Exception as e:
        return {"error": _sanitize_auth_error(str(e), connection)}

    # Try silent acquisition first
    accounts = app.get_accounts()
    expected = conn_config.get("expectedEmail", "")
    if accounts:
        for account in accounts:
            # Skip accounts that don't match expected email
            if expected and account.get("username", "").lower() != expected.lower():
                continue
            result = app.acquire_token_silent(effective_scopes, account=account)
            if result and "access_token" in result:
                _save_cache(cache, cache_path)
                return {"access_token": result["access_token"]}

    # Check if we have a pending device code flow (persisted to disk)
    flow_info = _load_pending_flow(connection, resource)
    if flow_info:
        try:
            result = app.acquire_token_by_device_flow(flow_info["flow"])
        except Exception as e:
            _clear_pending_flow(connection, resource)
            return {"error": _sanitize_auth_error(str(e), connection)}
        if result and "access_token" in result:
            _clear_pending_flow(connection, resource)
            # Verify correct account
            mismatch = _check_account_mismatch(result, conn_config, cache, cache_path)
            if mismatch:
                return mismatch
            _save_cache(cache, cache_path)
            return {"access_token": result["access_token"]}
        elif "error" in result:
            if result.get("error") == "authorization_pending":
                return {
                    "device_code": flow_info["code"],
                    "message": "Auth still pending. Complete the device code flow, then retry.",
                }
            _clear_pending_flow(connection, resource)
            raw = result.get("error_description", result.get("error", ""))
            return {"error": _sanitize_auth_error(raw, connection)}

    # Initiate new device code flow
    try:
        flow = app.initiate_device_flow(scopes=effective_scopes)
    except Exception as e:
        return {"error": _sanitize_auth_error(str(e), connection)}

    if "user_code" not in flow:
        raw = flow.get("error_description", "unknown error")
        return {"error": _sanitize_auth_error(raw, connection)}

    device_code = flow["user_code"]
    _save_pending_flow(connection, flow, device_code, resource)

    _save_cache(cache, cache_path)
    return {"device_code": device_code, "message": flow.get("message", "")}


def _make_graph_request(access_token: str, endpoint: str, method: str = "GET",
                        body: dict = None, headers: dict = None,
                        base_url: str = None) -> dict:
    """Make a direct HTTP request to a Microsoft API."""
    base = base_url or "https://graph.microsoft.com"

    # Normalize endpoint
    if not endpoint.startswith("/"):
        endpoint = f"/{endpoint}"

    # Only add version prefix for Graph API
    if base == "https://graph.microsoft.com":
        if endpoint.startswith("/beta/") or endpoint.startswith("/v1.0/"):
            url = f"{base}{endpoint}"
        else:
            url = f"{base}/v1.0{endpoint}"
    else:
        url = f"{base}{endpoint}"

    req_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    if headers:
        req_headers.update(headers)

    try:
        resp = httpx.request(
            method=method.upper(),
            url=url,
            headers=req_headers,
            json=body if body and method.upper() != "GET" else None,
            timeout=120,
        )

        if resp.status_code == 204:
            return {"status": "success", "data": {"message": "OK (no content)"}}

        if resp.status_code >= 400:
            try:
                error_data = resp.json()
            except Exception:
                error_data = {"raw": resp.text[:500]}
            # Log full error details, return sanitized version
            log_tool_call(
                mcp_name="mm", tool_name="graph_request",
                arguments={"endpoint": endpoint, "method": method},
                error=f"Graph API {resp.status_code}: {json.dumps(error_data)}",
                duration_ms=0,
            )
            # Extract just the error code and safe message
            if isinstance(error_data, dict) and "error" in error_data:
                err_obj = error_data["error"]
                code = err_obj.get("code", "UnknownError") if isinstance(err_obj, dict) else str(err_obj)
                msg = err_obj.get("message", "") if isinstance(err_obj, dict) else ""
                # Strip any tenant/org references from the message
                msg = re.sub(r"'[^']*'", "'[redacted]'", msg)
                msg = re.sub(r"tenant\s+[a-f0-9-]+", "tenant [redacted]", msg, flags=re.IGNORECASE)
                return {
                    "status": "error",
                    "error": f"Graph API {resp.status_code} ({code}): {msg}" if msg else f"Graph API {resp.status_code} ({code})",
                }
            return {
                "status": "error",
                "error": f"Graph API returned {resp.status_code}. Check logs for details.",
            }

        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text[:2000]}

        # Strip OData noise
        if isinstance(data, dict):
            odata_keys = [k for k in data if k.startswith("@odata.")]
            for k in odata_keys:
                del data[k]
            # Also strip from value items
            if "value" in data and isinstance(data["value"], list):
                for item in data["value"]:
                    if isinstance(item, dict):
                        for k in [k for k in item if k.startswith("@odata.")]:
                            del item[k]

        return {"status": "success", "data": data}

    except httpx.TimeoutException:
        return {"status": "error", "error": "Graph API request timed out"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# === Email Interceptors ===

def _strip_email_signature(body, endpoint, conn_config=None):
    """Strip auto-added signature patterns from email content.

    CodeTwo adds signatures on most accounts. Connections with
    "skipSignatureStrip": true in the registry are excluded.
    """
    if not body or not isinstance(body, dict):
        return body
    if not any(k in endpoint for k in ("sendMail", "messages", "reply")):
        return body

    # Skip stripping if connection opts out
    if conn_config and conn_config.get("skipSignatureStrip"):
        return body

    # Reply format — comment is the content
    if "comment" in body:
        content = body["comment"]
        for pattern in EMAIL_SIG_PATTERNS:
            content = re.split(pattern, content)[0]
        body["comment"] = content.rstrip()
        return body

    # sendMail / create message format
    if "message" in body:
        content_obj = body["message"].get("body", {})
    elif "body" in body:
        content_obj = body["body"]
    else:
        return body

    content = content_obj.get("content", "")
    if not content:
        return body

    for pattern in EMAIL_SIG_PATTERNS:
        content = re.split(pattern, content)[0]
    content_obj["content"] = content.rstrip()
    return body


# === Graph Request Hooks ===
# Each hook: (match_fn, handler_fn)
#   match_fn(endpoint, method) -> bool
#   handler_fn(endpoint, method, body, conn_config) -> (body, note)
#     - body: modified body (or original if unchanged)
#     - note: string to prepend to response, or None
# Hooks run in order. All matching hooks fire (notes accumulate, body chains).

# === Preview Extractors ===

def _extract_email_preview(body, endpoint):
    """Format a human-readable draft preview from a Graph email payload."""
    lines = ["**Draft Email Preview:**\n"]

    if not body or not isinstance(body, dict):
        lines.append(f"_(empty body — endpoint: `{endpoint}`)_")
        return "\n".join(lines)

    # sendMail wraps in "message", reply/forward use flat body
    msg = body.get("message", body)

    # Recipients
    for field in ("toRecipients", "ccRecipients", "bccRecipients"):
        recipients = msg.get(field, [])
        if recipients:
            label = field.replace("Recipients", "").upper()
            addrs = ", ".join(
                r.get("emailAddress", {}).get("address", "?") for r in recipients
            )
            lines.append(f"**{label}:** {addrs}")

    # Subject
    subject = msg.get("subject")
    if subject:
        lines.append(f"**Subject:** {subject}")

    # Body content
    body_obj = msg.get("body", {})
    content = body_obj.get("content", "")
    if content:
        # Truncate for preview
        preview = content[:500]
        if len(content) > 500:
            preview += "…"
        lines.append(f"\n**Body:**\n{preview}")

    # Comment (reply/forward)
    comment = body.get("comment")
    if comment:
        preview = comment[:500]
        if len(comment) > 500:
            preview += "…"
        lines.append(f"\n**Comment:**\n{preview}")

    # Attachments
    attachments = msg.get("attachments", [])
    if attachments:
        names = [a.get("name", "unnamed") for a in attachments]
        lines.append(f"\n**Attachments:** {', '.join(names)}")

    lines.append("\n---\n**BLOCKED — human approval required.** Show this preview to the user and ask for confirmation.\nDo NOT retry automatically. To send after approval, re-call this tool with the same parameters plus `\"confirmed\": true` in the `body` object.")
    return "\n".join(lines)


def _extract_teams_preview(body, endpoint):
    """Format a human-readable draft preview from a Teams message payload."""
    lines = ["**Draft Teams Message Preview:**\n"]

    lines.append(f"**Endpoint:** `{endpoint}`")

    if not body or not isinstance(body, dict):
        lines.append("_(empty body)_")
        return "\n".join(lines)

    # Message body
    body_obj = body.get("body", {})
    content = body_obj.get("content", "")
    if content:
        preview = content[:500]
        if len(content) > 500:
            preview += "…"
        lines.append(f"\n**Message:**\n{preview}")

    # Mentions
    mentions = body.get("mentions", [])
    if mentions:
        names = [m.get("mentioned", {}).get("user", {}).get("displayName", "?") for m in mentions]
        lines.append(f"\n**Mentions:** {', '.join(names)}")

    lines.append("\n---\n**BLOCKED — human approval required.** Show this preview to the user and ask for confirmation.\nDo NOT retry automatically. To send after approval, re-call this tool with the same parameters plus `\"confirmed\": true` in the `body` object.")
    return "\n".join(lines)


def _extract_ps_send_preview(command):
    """Format a human-readable preview of a PowerShell send command."""
    lines = ["**Draft PowerShell Send Preview:**\n"]
    lines.append(f"```powershell\n{command}\n```")
    lines.append("\n---\n**BLOCKED — human approval required.** Show this preview to the user and ask for confirmation.\nDo NOT retry automatically. To execute after approval, re-call this tool with `\"confirmed\": true` added as a tool parameter.")
    return "\n".join(lines)


# === Guard Hooks ===

def _hook_guard_email_send(endpoint, method, body, conn_config, confirmed=False):
    """Block email sends until confirmed. Returns draft preview on first call."""
    if confirmed or not _send_guards_enabled(conn_config):
        return body, None
    preview = _extract_email_preview(body, endpoint)
    return _GRAPH_BLOCKED, preview


def _hook_guard_teams_message(endpoint, method, body, conn_config, confirmed=False):
    """Block Teams message sends until confirmed. Returns draft preview on first call."""
    if confirmed or not _send_guards_enabled(conn_config):
        return body, None
    preview = _extract_teams_preview(body, endpoint)
    return _GRAPH_BLOCKED, preview


def _hook_guard_ps_send(command, module, conn_config, confirmed=False):
    """Block PowerShell send commands until confirmed. Returns command preview on first call."""
    if confirmed or not _send_guards_enabled(conn_config):
        return command, None
    preview = _extract_ps_send_preview(command)
    return None, preview


def _hook_strip_signature(endpoint, method, body, conn_config, confirmed=False):
    """Strip CodeTwo email signatures from outbound messages."""
    return _strip_email_signature(body, endpoint, conn_config), None


GRAPH_HOOKS = [
    # Guard hooks — block until confirmed
    (
        lambda ep, m: m.upper() == "POST" and any(
            k in ep for k in ("sendMail", "/reply", "/replyAll", "/forward", "/send")
        ),
        _hook_guard_email_send,
    ),
    (
        lambda ep, m: m.upper() == "POST" and (
            re.search(r'/teams/[^/]+/channels/[^/]+/messages', ep)
            or re.search(r'/chats/[^/]+/messages', ep)
        ),
        _hook_guard_teams_message,
    ),
    # Body modification — only fires after guards pass
    (
        lambda ep, m: m.upper() == "POST" and any(
            k in ep for k in ("sendMail", "reply", "forward", "/messages")
        ),
        _hook_strip_signature,
    ),
]


def _run_graph_hooks(endpoint, method, body, conn_config, confirmed=False):
    """Run all matching Graph hooks. Returns (body, [notes]).

    If a guard hook returns _GRAPH_BLOCKED, stop processing and return immediately.
    """
    notes = []
    for match_fn, handler_fn in GRAPH_HOOKS:
        if match_fn(endpoint, method):
            body, note = handler_fn(endpoint, method, body, conn_config, confirmed=confirmed)
            if note:
                notes.append(note)
            if body is _GRAPH_BLOCKED:
                break
    return body, notes


# === PowerShell Run Hooks ===
# Each hook: (match_fn, handler_fn)
#   match_fn(command, module) -> bool
#   handler_fn(command, module, conn_config) -> (command, note)
#     - command: modified command (or original if unchanged)
#     - note: string to prepend to response, or None
# Hooks run in order. All matching hooks fire (notes accumulate, command chains).

# Az modules installed in the container (Dockerfile)
_INSTALLED_AZ_MODULES = {"Az.Accounts"}

# Common cmdlet prefixes from Az modules NOT installed — redirect to Invoke-AzRestMethod
_MISSING_AZ_CMDLETS = re.compile(
    r'\b(Get|Set|New|Remove|Update)-(Az(?:ApplicationInsights|Monitor|OperationalInsights|LogAnalytics'
    r'|Network|Compute|Storage|WebApp|FunctionApp|Sql|CosmosDB|ServiceBus|EventHub|ApiManagement'
    r'|Aks|ContainerRegistry|KeyVault|Resource|Subscription|Cdn|FrontDoor|Dns|TrafficManager'
    r'|RedisCache|SignalR|AppConfiguration|CognitiveServices|MachineLearning|DataFactory)\w*)\b',
    re.IGNORECASE,
)


def _hook_missing_az_module(command, module, conn_config, confirmed=False):
    """Block cmdlets from uninstalled Az modules — redirect to Invoke-AzRestMethod."""
    if module != "azure":
        return command, None
    match = _MISSING_AZ_CMDLETS.search(command)
    if not match:
        return command, None
    cmdlet = match.group(0)
    # Return None command to signal "don't execute, just return the note"
    return None, (
        f"'{cmdlet}' requires an Az module that isn't installed in the container. "
        f"Only Az.Accounts is installed. Use Invoke-AzRestMethod to call the Azure REST API directly. "
        f"Example: Invoke-AzRestMethod -Path '/subscriptions/{{subId}}/resourceGroups/{{rg}}/providers/Microsoft.Insights/components?api-version=2020-02-02' -Method GET"
    )


def _hook_teams_error_action_stop(command, module, conn_config, confirmed=False):
    """Wrap Teams commands with ErrorAction Stop to prevent hanging on API errors.

    MicrosoftTeams 7.6.0 Set-CsCallQueue (and similar) throws non-terminating
    errors when the API fails, then continues into internal retry loops that hang
    forever (~300s until our timeout kills the process, cascading into broken pipes
    and forced re-auth). Wrapping with Stop makes the error terminate immediately,
    so the PowerShell marker still gets written and the session stays alive.
    """
    return (
        f'$ErrorActionPreference = "Stop"; try {{ {command} }} '
        f'catch {{ Write-Error "$_" -ErrorAction Continue }} '
        f'finally {{ $ErrorActionPreference = "Continue" }}',
        None,
    )


# PowerShell send commands that require confirmation
_PS_SEND_PATTERN = re.compile(
    r'\b(Send-MailMessage|Send-MgUserMail|New-MgChatMessage|New-MgTeamChannelMessage|Submit-PnPTeamsChannelMessage)\b',
    re.IGNORECASE,
)

RUN_HOOKS = [
    # Guard hooks — block until confirmed
    (
        lambda cmd, mod: bool(_PS_SEND_PATTERN.search(cmd)),
        _hook_guard_ps_send,
    ),
    # Existing hooks
    (
        lambda cmd, mod: mod == "azure" and bool(_MISSING_AZ_CMDLETS.search(cmd)),
        _hook_missing_az_module,
    ),
    (
        lambda cmd, mod: mod == "teams",
        _hook_teams_error_action_stop,
    ),
]


def _run_run_hooks(command, module, conn_config, confirmed=False):
    """Run all matching PowerShell run hooks. Returns (command, [notes]).

    If any hook sets command to None, execution should be skipped —
    just return the accumulated notes.
    """
    notes = []
    for match_fn, handler_fn in RUN_HOOKS:
        if match_fn(command, module):
            command, note = handler_fn(command, module, conn_config, confirmed=confirmed)
            if note:
                notes.append(note)
            if command is None:
                break  # Hook says don't execute
    return command, notes


# === Session Pool (PowerShell) ===

def call_pool(endpoint: str, method: str = "GET", data: dict = None) -> dict:
    """Call the session pool API."""
    url = f"{SESSION_POOL_URL}{endpoint}"
    try:
        if method == "GET":
            resp = httpx.get(url, timeout=120)
        else:
            resp = httpx.post(url, json=data, timeout=120)
        return resp.json()
    except httpx.TimeoutException:
        return {"status": "error", "error": "Request timed out"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# === MCP Server ===

server = Server("mm")


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="run",
            description="Execute a PowerShell command via session pool. Omit all params to list connections. Provide connection+module+command to execute.",
            inputSchema={
                "type": "object",
                "properties": {
                    "connection": {
                        "type": "string",
                        "description": "Connection name from ~/.m365-connections.json",
                    },
                    "module": {
                        "type": "string",
                        "description": "exo=Exchange, pnp=SharePoint, azure, teams",
                        "enum": ["exo", "pnp", "azure", "teams"],
                    },
                    "command": {
                        "type": "string",
                        "description": "PowerShell command",
                    },
                    "confirmed": {
                        "type": "boolean",
                        "description": "Set to true to bypass send guards after reviewing the draft preview.",
                    },
                },
            },
        ),
        Tool(
            name="graph_request",
            description="Microsoft REST API. Omit all params to list connections. Provide connection+endpoint to call Graph.\n\nFor Power Automate: use resource='flow' — this handles auth to https://service.flow.microsoft.com automatically. Do NOT use Get-AzAccessToken or m365 util accesstoken for Flow API tokens.",
            inputSchema={
                "type": "object",
                "properties": {
                    "connection": {
                        "type": "string",
                        "description": "Connection name from ~/.m365-connections.json",
                    },
                    "endpoint": {
                        "type": "string",
                        "description": "API endpoint (e.g., '/me/messages' for Graph, '/providers/Microsoft.ProcessSimple/environments/{envId}/flows' for Flow)",
                    },
                    "method": {
                        "type": "string",
                        "description": "HTTP method",
                        "enum": ["GET", "POST", "PATCH", "PUT", "DELETE"],
                        "default": "GET",
                    },
                    "body": {
                        "type": "object",
                        "description": "Request body for POST/PATCH/PUT",
                    },
                    "resource": {
                        "type": "string",
                        "description": "Target API: 'graph' (default) for Microsoft Graph, 'flow' for Power Automate",
                        "enum": ["graph", "flow"],
                        "default": "graph",
                    },
                    "confirmed": {
                        "type": "boolean",
                        "description": "Set to true to bypass send guards after reviewing the draft preview.",
                    },
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    start_time = time.time()
    error_msg = None
    result_summary = None
    connection_name = arguments.get("connection")

    try:
        if name == "run":
            result = _handle_run(arguments)
        elif name == "graph_request":
            result = _handle_graph_request(arguments)
        else:
            result = [TextContent(type="text", text=f"Unknown tool: {name}")]

        if result and len(result) > 0:
            text = result[0].text[:100] if hasattr(result[0], 'text') else str(result[0])[:100]
            if "error" in text.lower():
                error_msg = text
            else:
                result_summary = text
        return result
    except Exception as e:
        error_msg = str(e)
        raise
    finally:
        duration_ms = int((time.time() - start_time) * 1000)
        log_tool_call(
            mcp_name="mm",
            tool_name=name,
            arguments=arguments,
            connection_name=connection_name,
            result=result_summary,
            error=error_msg,
            duration_ms=duration_ms,
        )


def _list_connections() -> list:
    """List connections from registry. Only expose name + description."""
    registry = load_registry()
    connections = registry.get("connections", {})

    output = "**Available Connections:**\n"
    for conn_name, config in connections.items():
        output += f"- **{conn_name}**: {config.get('description', '')}\n"

    return [TextContent(type="text", text=output)]


def _format_device_code(device_code: str, connection: str, conn_config: dict, context: str = "") -> list:
    """Format device code response. Only expose what the human needs to act on."""
    expected_email = conn_config.get("expectedEmail", "")
    text = f"**DEVICE CODE: {device_code}**\nGo to: https://microsoft.com/devicelogin\n\nConnection: {connection}"
    if expected_email:
        text += f"\n**Sign in as: {expected_email}**"
    if context:
        text += f"\n{context}"
    text += "\n\nAfter authenticating, retry the command."

    return [TextContent(type="text", text=text)]


# === PowerShell (run) ===

def _handle_run(arguments: dict) -> list:
    connection = arguments.get("connection")
    module = arguments.get("module")
    command = arguments.get("command")

    # No params = list connections
    if not connection and not module and not command:
        return _list_connections()

    # Validate required params
    if not all([connection, module, command]):
        return [TextContent(type="text", text="Error: connection, module, and command are all required")]

    # Validate connection exists
    conn_config, err = get_connection_config(connection)
    if err:
        return [TextContent(type="text", text=err)]

    # Extract confirmed — check top-level args, then look for it embedded in the command string
    confirmed = arguments.get("confirmed", False)

    # Run hooks (missing module redirects, send guards, etc.)
    command, run_notes = _run_run_hooks(command, module, conn_config, confirmed=confirmed)
    if command is None:
        # Hook blocked execution — return the notes as the response
        return [TextContent(type="text", text="\n\n".join(run_notes))]

    # Execute command via session pool
    result = call_pool("/run", "POST", {
        "connection": connection,
        "module": module,
        "command": command,
        "caller_id": "mm-mcp",
    })

    status = result.get("status")

    # Log full pool response for diagnostics
    if status == "error":
        log_tool_call(
            mcp_name="mm", tool_name="run",
            arguments={"connection": connection, "module": module, "command": command[:80]},
            error=f"Pool error: {result.get('error', 'unknown')}",
            duration_ms=0,
        )
    elif status == "success":
        output_raw = result.get("output", "")
        # Flag auth/permission errors buried in "successful" output
        if re.search(r'(AADSTS\d+|Unauthorized|Forbidden|access.denied)', output_raw, re.IGNORECASE):
            log_tool_call(
                mcp_name="mm", tool_name="run",
                arguments={"connection": connection, "module": module, "command": command[:80]},
                error=f"Auth error in output: {output_raw[:300]}",
                duration_ms=0,
            )

    if status == "auth_required":
        device_code = result.get("device_code", "")
        return _format_device_code(device_code, connection, conn_config, f"Module: {module}")

    if status == "auth_in_progress":
        return [TextContent(type="text", text="Auth in progress by another caller. Retry in a few seconds.")]

    if status == "error":
        return [TextContent(type="text", text=f"Error: {result.get('error', 'Unknown error')}")]

    if status == "success":
        output = result.get("output", "")
        # Strip ANSI codes
        output = re.sub(r'\x1b\[[0-9;]*m', '', output)
        output = re.sub(r'\x1b\[\?[0-9]+[hl]', '', output)

        # Check for email mismatch — log it, don't expose details to AI
        authenticated_as = result.get("authenticated_as")
        if authenticated_as:
            expected_email = conn_config.get("expectedEmail", "")
            if expected_email and expected_email.lower() != authenticated_as.lower():
                log_tool_call(
                    mcp_name="mm", tool_name="run_auth",
                    arguments={"connection": connection, "module": module},
                    error=f"Account mismatch: expected {expected_email}, got {authenticated_as}",
                    duration_ms=0,
                )
                output = "WARNING: Authenticated with wrong account. Re-authentication required.\n\n" + output

        output = output.strip() if output.strip() else "(no output)"
        if run_notes:
            prefix = "\n".join(f"**Note:** {n}" for n in run_notes)
            output = f"{prefix}\n\n{output}"
        return [TextContent(type="text", text=output)]

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


# === Graph API (graph_request) ===

def _handle_graph_request(arguments: dict) -> list:
    connection = arguments.get("connection")
    endpoint = arguments.get("endpoint")
    resource = arguments.get("resource", "graph")

    # No params = list connections
    if not connection and not endpoint:
        return _list_connections()

    if not connection:
        return [TextContent(type="text", text="Error: connection is required")]

    if not endpoint:
        return [TextContent(type="text", text="Error: endpoint is required (e.g., '/me/messages')")]

    # Validate resource
    res_config = RESOURCE_CONFIGS.get(resource)
    if not res_config:
        available = ", ".join(RESOURCE_CONFIGS.keys())
        return [TextContent(type="text", text=f"Error: Unknown resource '{resource}'. Available: {available}")]

    # Validate connection exists
    conn_config, err = get_connection_config(connection)
    if err:
        return [TextContent(type="text", text=err)]

    if not conn_config.get("appId"):
        return [TextContent(type="text", text=f"Error: Connection '{connection}' is not configured for API access.")]

    # Acquire token with resource-specific scopes
    token_result = _acquire_graph_token(
        connection, conn_config,
        scopes=res_config["scopes"], resource=resource,
    )

    if "device_code" in token_result:
        label = "Graph API" if resource == "graph" else f"Power Automate ({resource})"
        return _format_device_code(token_result["device_code"], connection, conn_config, f"Tool: {label}")

    if "error" in token_result:
        return [TextContent(type="text", text=f"Error: {token_result['error']}")]

    access_token = token_result["access_token"]

    # Make the API request
    method = arguments.get("method", "GET")
    body = arguments.get("body")
    base_url = res_config["base_url"]

    # Extract confirmed — check top-level args first, then inside body (AIs put it there)
    confirmed = arguments.get("confirmed", False)
    if not confirmed and isinstance(body, dict):
        confirmed = body.pop("confirmed", False)

    # Run Graph hooks (send guards, signature stripping, etc.)
    body, notes = _run_graph_hooks(endpoint, method, body, conn_config, confirmed=confirmed)

    # Guard hook blocked execution — return the draft preview
    if body is _GRAPH_BLOCKED:
        return [TextContent(type="text", text="\n\n".join(notes))]

    result = _make_graph_request(
        access_token, endpoint, method, body,
        base_url=base_url,
    )

    if result["status"] == "error":
        return [TextContent(type="text", text=f"Error: {result['error']}")]

    data = result["data"]
    output = json.dumps(data, indent=2)
    if notes:
        prefix = "\n".join(f"**Note:** {n}" for n in notes)
        output = f"{prefix}\n\n{output}"
    return [TextContent(type="text", text=output)]


# === Main ===

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
