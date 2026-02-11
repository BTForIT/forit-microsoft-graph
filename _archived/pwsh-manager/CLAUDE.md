# pwsh-manager AI Instructions

## Connection Model

Uses shared `~/.m365-connections.json` registry. Sessions are identified by `tenant:module` internally (e.g., `contoso.com:exo`), but you always use **connectionName** (e.g., `Contoso`).

### Critical Rules

1. **connectionName is REQUIRED** - Every command must specify which connection to use
2. **No "active" or "default" connection concept** - Never assume which connection
3. **No switching between sessions** - Parallel sessions coexist independently
4. **Session = authenticated connection** - Not "selected" or "focused"

### Correct Usage

```
# Each call specifies connectionName explicitly
pwsh_run connectionName=Contoso module=exo command="Get-Mailbox"
pwsh_run connectionName=Personal module=azure command="Get-AzSubscription"

# Both work independently - no switching needed
```

### Wrong Patterns (Never Do This)

- "Set active connection to X"
- "Switch to the Contoso connection"
- "Use the current session"
- "The active session is..."
- "The default connection is..."

### Terminology

| Use This | Not This |
|----------|----------|
| session | active session |
| connected | active |
| connectionName + module | current connection |
| specify connectionName | switch to connection |

## Multi-Tenant

Multiple sessions can exist simultaneously. The `pwsh_sessions` tool lists all of them - none is "primary" or "default". Use `pwsh_list_connections` to see available connections.

## Authentication

- `pwsh_login` creates a session for a specific connectionName:module
- Device code flow authenticates to that specific session
- Auth state is per-session, not global

## Registry

Connections are configured in `~/.m365-connections.json`. Only connections with `"pwsh-manager"` in their `mcps` array are available.
