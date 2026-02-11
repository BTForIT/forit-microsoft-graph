#!/usr/bin/env python3
"""
PowerShell Manager MCP Server

Thin MCP client that communicates with the Docker-based PowerShell Session Manager.
Register this with MCPJungle or Claude Desktop.

Uses shared ~/.m365-connections.json registry.
"""

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

# Add parent dir to path for shared logger
sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from mcp_logger import log_tool_call, log_session_event, get_session_history
except ImportError:
    def log_tool_call(*args, **kwargs): pass
    def log_session_event(*args, **kwargs): pass
    def get_session_history(*args, **kwargs): return []

# Session ID for tracking - set during initialize or from environment
# This is mutable - can be updated when MCP client sends session info
_session_id: Optional[str] = None


def get_session_id() -> str:
    """Get current session ID, generating fallback if needed."""
    global _session_id
    if _session_id:
        return _session_id

    # Check environment (generic, then Claude-specific for compatibility)
    for env_var in ["MCP_SESSION_ID", "CLAUDE_SESSION_ID", "PWSH_get_session_id()"]:
        val = os.getenv(env_var, "")
        if val:
            _session_id = val[:8]
            return _session_id

    # Check session file (written by client hooks)
    for session_file in ["~/.mcp-session", "~/.claude-current-session"]:
        try:
            path = Path(session_file).expanduser()
            if path.exists():
                val = path.read_text().strip()
                if val:
                    _session_id = val[:8]
                    return _session_id
        except Exception:
            pass

    # Generate random fallback
    _session_id = str(uuid.uuid4())[:8]
    return _session_id


def set_session_id(session_id: str):
    """Set session ID (called from initialize or tool params)."""
    global _session_id
    if session_id:
        _session_id = session_id[:8]

# Configuration
PWSH_MANAGER_URL = os.getenv("PWSH_MANAGER_URL", "http://localhost:5100")
REQUEST_TIMEOUT = int(os.getenv("PWSH_MANAGER_TIMEOUT", "300"))

# Universal connection registry
CONNECTIONS_FILE = Path.home() / ".m365-connections.json"
MCP_NAME = "pwsh-manager"

# SharePoint tenant prefixes (when different from domain prefix)
# Maps tenant domain -> SharePoint tenant prefix (e.g., "contoso" for contoso.sharepoint.com)
SHAREPOINT_TENANTS = {
    "contoso.com": "contoso",
    # Add more mappings as needed
}


def load_connections() -> dict:
    """Load connections from universal registry."""
    try:
        data = json.loads(CONNECTIONS_FILE.read_text())
        return data.get("connections", {})
    except Exception:
        return {}


def get_connection(name: str) -> Optional[Dict]:
    """Get a connection by name, filtered for this MCP."""
    connections = load_connections()
    conn = connections.get(name)
    if conn and MCP_NAME in conn.get("mcps", []):
        return conn
    return None


def list_available_connections() -> List[str]:
    """List all connections available to this MCP."""
    connections = load_connections()
    return [name for name, conn in connections.items() if MCP_NAME in conn.get("mcps", [])]


def get_sharepoint_tenant(tenant_domain: str) -> str:
    """Get SharePoint tenant prefix for a domain."""
    return SHAREPOINT_TENANTS.get(tenant_domain, tenant_domain.split(".")[0])


def api_call(endpoint: str, data: dict = None, include_conversation: bool = True) -> dict:
    """Make API call to session manager."""
    url = f"{PWSH_MANAGER_URL}{endpoint}"
    try:
        if data:
            # Include conversation_id for session tracking
            if include_conversation:
                data = {**data, "conversation_id": get_session_id()}
            response = requests.post(url, json=data, timeout=REQUEST_TIMEOUT)
        else:
            response = requests.get(url, timeout=REQUEST_TIMEOUT)
        return response.json()
    except requests.exceptions.ConnectionError:
        return {"success": False, "error": "Cannot connect to pwsh-manager. Is Docker running?"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# MCP Protocol Implementation
def handle_initialize(params: dict) -> dict:
    """Handle MCP initialize request."""
    # Extract session ID from client info if provided (works with any MCP client)
    client_info = params.get("clientInfo", {})
    session_id = (
        params.get("sessionId")  # Direct param
        or client_info.get("sessionId")  # In clientInfo
        or client_info.get("session_id")  # Snake case variant
        or params.get("session_id")
    )
    if session_id:
        set_session_id(session_id)

    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}},
        "serverInfo": {
            "name": "pwsh-manager",
            "version": "1.0.0",
            "sessionId": get_session_id(),  # Echo back so client knows what we're using
        },
    }


def handle_list_tools() -> dict:
    """Return available tools."""
    return {
        "tools": [
            {
                "name": "pwsh_login",
                "description": "Authenticate to a Microsoft service (EXO, PnP, Azure, Power Platform, Teams). Returns device code for authentication.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "connectionName": {
                            "type": "string",
                            "description": "REQUIRED: Connection name from ~/.m365-connections.json (e.g., 'Contoso')",
                        },
                        "module": {
                            "type": "string",
                            "enum": ["exo", "pnp", "azure", "powerplatform", "teams"],
                            "description": "Module to authenticate: exo (Exchange), pnp (SharePoint), azure, powerplatform, teams (MicrosoftTeams)",
                            "default": "exo",
                        },
                        "account": {
                            "type": "string",
                            "description": "For Azure: which account to select when prompted (default: '1'). Use '2', '3', etc. if you have multiple Azure accounts.",
                            "default": "1",
                        },
                    },
                    "required": ["connectionName"],
                },
            },
            {
                "name": "pwsh_status",
                "description": "Check authentication status for a connection/module combination.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "connectionName": {
                            "type": "string",
                            "description": "REQUIRED: Connection name (e.g., 'Contoso')",
                        },
                        "module": {
                            "type": "string",
                            "enum": ["exo", "pnp", "azure", "powerplatform", "teams"],
                            "default": "exo",
                        },
                    },
                    "required": ["connectionName"],
                },
            },
            {
                "name": "pwsh_run",
                "description": "Execute a PowerShell command in an authenticated session.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "connectionName": {
                            "type": "string",
                            "description": "REQUIRED: Connection name (e.g., 'Contoso')",
                        },
                        "module": {
                            "type": "string",
                            "enum": ["exo", "pnp", "azure", "powerplatform", "teams"],
                            "default": "exo",
                        },
                        "command": {
                            "type": "string",
                            "description": "PowerShell command to execute (e.g., 'Get-Mailbox -ResultSize 10')",
                        },
                    },
                    "required": ["connectionName", "command"],
                },
            },
            {
                "name": "pwsh_sessions",
                "description": "List all active PowerShell sessions and their status.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "pwsh_list_connections",
                "description": "List all connections configured for pwsh-manager from ~/.m365-connections.json.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "pwsh_disconnect",
                "description": "Disconnect a specific session. REQUIRES explicit user confirmation to prevent accidental disconnects.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "connectionName": {
                            "type": "string",
                            "description": "REQUIRED: Connection name (e.g., 'Contoso')",
                        },
                        "module": {
                            "type": "string",
                            "enum": ["exo", "pnp", "azure", "powerplatform", "teams"],
                            "default": "exo",
                        },
                        "confirmation": {
                            "type": "string",
                            "description": "REQUIRED: Must be exactly 'DISCONNECT' to confirm this destructive action",
                        },
                    },
                    "required": ["connectionName", "confirmation"],
                },
            },
            {
                "name": "pwsh_session_history",
                "description": "View session history/logs to analyze patterns, durations, and identify issues.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "tenant": {
                            "type": "string",
                            "description": "Filter by tenant domain (optional)",
                        },
                        "module": {
                            "type": "string",
                            "enum": ["exo", "pnp", "azure", "powerplatform", "teams"],
                            "description": "Filter by module (optional)",
                        },
                        "event": {
                            "type": "string",
                            "enum": ["auth_pending", "authenticated", "auth_failed", "session_disconnected", "session_killed"],
                            "description": "Filter by event type (optional)",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max entries to return (default: 50)",
                            "default": 50,
                        },
                    },
                },
            },
        ]
    }


def handle_call_tool(name: str, arguments: dict) -> dict:
    """Execute a tool call."""
    start_time = time.time()
    conn_name = arguments.get("connectionName", "")
    error_msg = None
    result_summary = None

    try:
        result = _handle_call_tool_impl(name, arguments)
        # Extract result summary for logging
        if result.get("content"):
            text = result["content"][0].get("text", "")[:100]
            if result.get("isError"):
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
            mcp_name="pwsh-manager",
            tool_name=name,
            arguments=arguments,
            connection_name=conn_name or None,
            conversation_id=get_session_id(),
            result=result_summary,
            error=error_msg,
            duration_ms=duration_ms
        )


def _handle_call_tool_impl(name: str, arguments: dict) -> dict:
    """Execute a tool call (implementation)."""
    # Handle tools that don't require connectionName first
    if name == "pwsh_list_connections":
        connections = load_connections()
        available = [(n, c) for n, c in connections.items() if MCP_NAME in c.get("mcps", [])]

        if not available:
            return {"content": [{"type": "text", "text": json.dumps({
                "error": "No connections configured for pwsh-manager MCP",
                "hint": "Add connections to ~/.m365-connections.json with 'pwsh-manager' in mcps array"
            }, indent=2)}]}

        results = []
        for conn_name, conn in available:
            results.append({
                "name": conn_name,
                "tenant": conn.get("tenant", ""),
                "description": conn.get("description", ""),
            })
        return {"content": [{"type": "text", "text": json.dumps(results, indent=2)}]}

    if name == "pwsh_sessions":
        result = api_call("/sessions", include_conversation=False)

        if not result.get("sessions"):
            return {"content": [{"type": "text", "text": "No active sessions"}]}

        lines = ["Sessions:", "-" * 60]
        for s in result["sessions"]:
            status = "✓" if s["connected"] else ("⏳" if s["auth_pending"] else "✗")
            conv = s.get("conversation_id", "?")[:8] if s.get("conversation_id") else "?"
            stuck_info = f" [STUCK: {s['stuck_reason']}]" if s.get("stuck") else ""
            lines.append(f"{status} {s['tenant']} ({s['module']}) - conv: {conv} - Last: {s['last_used'][:19]}{stuck_info}")

        lines.append("-" * 60)
        lines.append(f"Current conversation: {get_session_id()}")
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    # All other tools require connectionName
    conn_name = arguments.get("connectionName", "")
    module = arguments.get("module", "exo")

    if not conn_name:
        available = list_available_connections()
        return {"content": [{"type": "text", "text": json.dumps({
            "error": "connectionName is REQUIRED",
            "available": available,
            "hint": "Every command must specify which connection to use"
        }, indent=2)}], "isError": True}

    conn = get_connection(conn_name)
    if not conn:
        available = list_available_connections()
        return {"content": [{"type": "text", "text": json.dumps({
            "error": f"Connection '{conn_name}' not found or not configured for pwsh-manager MCP",
            "available": available
        }, indent=2)}], "isError": True}

    tenant = conn.get("tenant", "")

    # Build request data - include SharePoint tenant for PnP
    def build_request(extra: dict = None) -> dict:
        data = {"tenant": tenant, "module": module}
        if module == "pnp":
            data["sharepoint_tenant"] = get_sharepoint_tenant(tenant)
        if extra:
            data.update(extra)
        return data

    if name == "pwsh_login":
        account = arguments.get("account", "1")
        result = api_call("/login", build_request({"account": account}))

        # Log session lifecycle
        if result.get("auth_pending"):
            log_session_event("auth_pending", tenant, module, get_session_id(), {"device_code": result.get("device_code")})
        elif result.get("success"):
            log_session_event("authenticated", tenant, module, get_session_id())
        else:
            log_session_event("auth_failed", tenant, module, get_session_id(), {"error": result.get("error", "Unknown")})

        # Format device code prominently if present
        if result.get("device_code"):
            text = f"**DEVICE CODE: {result['device_code']}**\nGo to: {result.get('auth_url', 'https://microsoft.com/devicelogin')}\n\n"
            text += f"Connection: {conn_name}\nTenant: {tenant}\nModule: {module}\n"
            text += f"Conversation: {get_session_id()}\n\n"
            if result.get("auth_pending"):
                text += "Authentication pending. Complete device code flow, then check status."
            else:
                text += "Connected"
            return {"content": [{"type": "text", "text": text}]}

        if result.get("success"):
            return {"content": [{"type": "text", "text": f"Connected to {conn_name} ({tenant}) - {module}"}]}
        return {"content": [{"type": "text", "text": f"Login failed: {result.get('error', result.get('result', 'Unknown error'))}"}], "isError": True}

    elif name == "pwsh_status":
        result = api_call("/status", build_request())

        # Log state transitions
        if result.get("connected") and result.get("was_pending"):
            # Auth just completed
            log_session_event("authenticated", tenant, module, get_session_id(), {
                "auth_duration_seconds": result.get("auth_duration_seconds")
            })

        if result.get("connected"):
            status = f"✓ {conn_name} ({tenant}) - {module}: Connected"
            status += f"\nConversation: {get_session_id()}"
        elif result.get("auth_pending"):
            status = f"⏳ {conn_name} ({tenant}) - {module}: Authentication pending (complete device code flow)"
            status += f"\nConversation: {get_session_id()}"
        else:
            status = f"✗ {conn_name} ({tenant}) - {module}: Not connected"

        return {"content": [{"type": "text", "text": status}]}

    elif name == "pwsh_run":
        command = arguments.get("command", "")
        if not command:
            return {"content": [{"type": "text", "text": "Error: command is required"}], "isError": True}

        result = api_call("/run", build_request({"command": command}))

        if result.get("success"):
            return {"content": [{"type": "text", "text": result.get("result", "OK")}]}
        return {"content": [{"type": "text", "text": f"Error: {result.get('error', result.get('result', 'Command failed'))}"}], "isError": True}

    elif name == "pwsh_disconnect":
        confirmation = arguments.get("confirmation", "")
        if confirmation != "DISCONNECT":
            return {"content": [{"type": "text", "text": json.dumps({
                "error": "Disconnect requires explicit user confirmation",
                "required": "Set confirmation='DISCONNECT' to proceed",
                "reason": "This is a destructive action that terminates an authenticated session"
            }, indent=2)}], "isError": True}

        result = api_call("/disconnect", build_request())

        if result.get("success"):
            log_session_event("session_disconnected", tenant, module, get_session_id())
            return {"content": [{"type": "text", "text": f"Disconnected {conn_name} ({tenant}) - {module}"}]}
        return {"content": [{"type": "text", "text": f"Error: {result.get('error', 'Disconnect failed')}"}], "isError": True}

    elif name == "pwsh_session_history":
        history = get_session_history(
            tenant=arguments.get("tenant"),
            module=arguments.get("module"),
            event=arguments.get("event"),
            limit=arguments.get("limit", 50)
        )

        if not history:
            return {"content": [{"type": "text", "text": "No session history found"}]}

        # Format as a readable summary
        lines = [f"Session History ({len(history)} entries):", "-" * 60]
        for entry in history[-20:]:  # Show last 20
            ts = entry.get("logged_at", "")[:19]  # Trim to second precision
            event = entry.get("event", "unknown")
            tenant_str = entry.get("tenant", "?")
            module_str = entry.get("module", "?")
            conv = entry.get("conversation_id", "?")[:8]
            details = entry.get("details", {})

            line = f"[{ts}] {event:20} {tenant_str}:{module_str} (conv: {conv})"
            if details:
                detail_str = ", ".join(f"{k}={v}" for k, v in details.items() if v)
                if detail_str:
                    line += f" - {detail_str}"
            lines.append(line)

        lines.append("-" * 60)
        lines.append(f"Current conversation: {get_session_id()}")
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}


def main():
    """Main MCP server loop."""
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break

            request = json.loads(line)
            method = request.get("method", "")
            params = request.get("params", {})
            request_id = request.get("id")

            result = None

            if method == "initialize":
                result = handle_initialize(params)
            elif method == "notifications/initialized":
                continue  # No response needed
            elif method == "tools/list":
                result = handle_list_tools()
            elif method == "tools/call":
                tool_name = params.get("name", "")
                arguments = params.get("arguments", {})
                result = handle_call_tool(tool_name, arguments)
            else:
                result = {"error": {"code": -32601, "message": f"Unknown method: {method}"}}

            if request_id is not None:
                response = {"jsonrpc": "2.0", "id": request_id, "result": result}
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()

        except json.JSONDecodeError:
            continue
        except Exception as e:
            if request_id is not None:
                error_response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32603, "message": str(e)},
                }
                sys.stdout.write(json.dumps(error_response) + "\n")
                sys.stdout.flush()


if __name__ == "__main__":
    main()
