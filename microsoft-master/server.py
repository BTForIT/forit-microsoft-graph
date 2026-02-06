#!/usr/bin/env python3
"""
mm MCP - Unified Microsoft 365 management.

Wraps the M365 Session Pool API for MSP multi-tenant operations.
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
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Shared logger
sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from mcp_logger import log_tool_call
except ImportError:
    def log_tool_call(*args, **kwargs): pass

# Session pool endpoint
SESSION_POOL_URL = os.getenv("MM_SESSION_POOL_URL", "http://localhost:5200")

# Connection registry (READ-ONLY)
CONNECTIONS_FILE = Path.home() / ".m365-connections.json"


def load_registry() -> dict:
    """Load connection registry. Read-only - never writes."""
    try:
        return json.loads(CONNECTIONS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"connections": {}}


server = Server("mm")


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


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="run",
            description="Execute a PowerShell command. Omit all params to list connections. Provide connection+module+command to execute.",
            inputSchema={
                "type": "object",
                "properties": {
                    "connection": {
                        "type": "string",
                        "description": "Connection name (e.g., 'ForIT-GA')",
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
        result = _call_tool_impl(name, arguments)
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


def _call_tool_impl(name: str, arguments: dict):
    if name != "run":
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    connection = arguments.get("connection")
    module = arguments.get("module")
    command = arguments.get("command")

    # No params = list connections from registry (read-only)
    if not connection and not module and not command:
        registry = load_registry()
        connections = registry.get("connections", {})

        output = "**Available Connections:**\n"
        for conn_name, config in connections.items():
            expected = config.get("expectedEmail", "")
            email_hint = f" [{expected}]" if expected else ""
            output += f"- **{conn_name}**: {config.get('tenant', 'unknown')}{email_hint}\n"
            output += f"  {config.get('description', '')}\n"

        return [TextContent(type="text", text=output)]

    # Validate required params
    if not all([connection, module, command]):
        return [TextContent(type="text", text="Error: connection, module, and command are all required")]

    # Validate connection exists in registry BEFORE touching session pool
    registry = load_registry()
    conn_config = registry.get("connections", {}).get(connection)
    if not conn_config:
        available = list(registry.get("connections", {}).keys())
        return [TextContent(type="text", text=f"Error: Connection '{connection}' not found in registry.\nAvailable: {', '.join(available)}\n\nConnections must be pre-created in ~/.m365-connections.json")]

    # Execute command via session pool
    result = call_pool("/run", "POST", {
        "connection": connection,
        "module": module,
        "command": command,
        "caller_id": "mm-mcp",
    })

    status = result.get("status")

    if status == "auth_required":
        device_code = result.get("device_code", "")
        expected_email = conn_config.get("expectedEmail", "")
        if expected_email:
            sign_in_hint = f"\n>>> SIGN IN AS: {expected_email} <<<"
        else:
            tenant = conn_config.get("tenant", "")
            sign_in_hint = f"\n>>> Sign in with your @{tenant} account <<<" if tenant else ""
        return [TextContent(
            type="text",
            text=f"**DEVICE CODE: {device_code}**\nGo to: https://microsoft.com/devicelogin\n{sign_in_hint}\n\nConnection: {connection}\nModule: {module}\n\nAfter authenticating, retry the command."
        )]

    if status == "auth_in_progress":
        return [TextContent(type="text", text="Auth in progress by another caller. Retry in a few seconds.")]

    if status == "error":
        return [TextContent(type="text", text=f"Error: {result.get('error', 'Unknown error')}")]

    if status == "success":
        output = result.get("output", "")
        # Strip ANSI codes for cleaner output
        output = re.sub(r'\x1b\[[0-9;]*m', '', output)
        output = re.sub(r'\x1b\[\?[0-9]+[hl]', '', output)

        # Check for email mismatch if session pool returned identity
        authenticated_as = result.get("authenticated_as")
        if authenticated_as:
            expected_email = conn_config.get("expectedEmail", "")
            if expected_email and expected_email.lower() != authenticated_as.lower():
                warning = f"WARNING: Wrong account! Expected {expected_email}, got {authenticated_as}\n\n"
                output = warning + output

        return [TextContent(type="text", text=output.strip() if output.strip() else "(no output)")]

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
