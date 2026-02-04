#!/usr/bin/env python3
"""
Centralized MCP Logger

Provides persistent logging across all M365 MCP servers for:
- Tool calls with timing
- Session lifecycle events (start, auth_pending, authenticated, stuck, killed)
- Conversation/session tracking for orphan detection

Log file: ~/.m365-mcp/logs/mcp-activity.jsonl
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

# Log directory
LOG_DIR = Path.home() / ".m365-mcp" / "logs"
LOG_FILE = LOG_DIR / "mcp-activity.jsonl"
SESSION_LOG_FILE = LOG_DIR / "pwsh-sessions.jsonl"

# Ensure log directory exists
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _write_log(filepath: Path, entry: dict):
    """Append a JSON line to log file."""
    entry["logged_at"] = datetime.now().isoformat()
    try:
        with open(filepath, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        # Fail silently - logging should never break the MCP
        sys.stderr.write(f"[mcp_logger] Failed to write log: {e}\n")


def log_tool_call(
    mcp_name: str,
    tool_name: str,
    arguments: Dict[str, Any],
    connection_name: Optional[str] = None,
    conversation_id: Optional[str] = None,
    result: Optional[str] = None,
    error: Optional[str] = None,
    duration_ms: Optional[int] = None,
):
    """Log an MCP tool call."""
    entry = {
        "type": "tool_call",
        "mcp": mcp_name,
        "tool": tool_name,
        "connection": connection_name,
        "conversation_id": conversation_id,
        "arguments": {k: v for k, v in arguments.items() if k != "command"},  # Exclude full commands
        "command_preview": arguments.get("command", "")[:100] if "command" in arguments else None,
        "duration_ms": duration_ms,
        "success": error is None,
        "error": error,
    }
    _write_log(LOG_FILE, entry)


def log_session_event(
    event: str,
    tenant: str,
    module: str,
    conversation_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    duration_seconds: Optional[float] = None,
):
    """
    Log a PowerShell session lifecycle event.

    Events:
    - session_start: New session created
    - auth_pending: Device code auth initiated
    - authenticated: Auth completed successfully
    - auth_timeout: Auth took too long
    - command_run: Command executed
    - session_idle: Session marked idle
    - session_stuck: Session detected as stuck
    - session_killed: Session force-killed
    - session_disconnected: Session gracefully disconnected
    """
    entry = {
        "type": "session_event",
        "event": event,
        "tenant": tenant,
        "module": module,
        "conversation_id": conversation_id,
        "details": details or {},
        "duration_seconds": duration_seconds,
    }
    _write_log(SESSION_LOG_FILE, entry)


def get_session_history(
    tenant: Optional[str] = None,
    module: Optional[str] = None,
    conversation_id: Optional[str] = None,
    event: Optional[str] = None,
    limit: int = 100,
) -> list:
    """Read session history with optional filters."""
    results = []
    try:
        if not SESSION_LOG_FILE.exists():
            return []
        with open(SESSION_LOG_FILE, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    if tenant and entry.get("tenant") != tenant:
                        continue
                    if module and entry.get("module") != module:
                        continue
                    if conversation_id and entry.get("conversation_id") != conversation_id:
                        continue
                    if event and entry.get("event") != event:
                        continue
                    results.append(entry)
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    # Return most recent entries
    return results[-limit:]


def get_orphan_sessions(active_conversations: list) -> list:
    """
    Find sessions that were started by conversations that are no longer active.

    Args:
        active_conversations: List of currently active conversation IDs

    Returns:
        List of session entries that may be orphaned
    """
    results = []
    try:
        if not SESSION_LOG_FILE.exists():
            return []

        # Track last event per session
        sessions = {}  # (tenant, module) -> last entry
        with open(SESSION_LOG_FILE, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    key = (entry.get("tenant"), entry.get("module"))
                    sessions[key] = entry
                except json.JSONDecodeError:
                    continue

        # Find sessions whose last event was not a termination and whose
        # conversation_id is not in the active list
        for key, entry in sessions.items():
            if entry.get("event") in ("session_killed", "session_disconnected"):
                continue  # Session already terminated
            conv_id = entry.get("conversation_id")
            if conv_id and conv_id not in active_conversations:
                results.append(entry)
    except Exception:
        pass
    return results
