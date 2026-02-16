# Microsoft Master (MM)

Unified Microsoft 365 MCP server for AI assistants. One server, two tools, all of M365.

## Prerequisites

| Requirement | Why |
|-------------|-----|
| **Python 3.10+** | MCP server runtime (`mm/server.py`) |
| **Docker** | Session pool runs PowerShell modules in containers |
| **An Azure AD (Entra ID) app registration** | Both tools authenticate via device code flow against your app |
| **MCPJungle** (or any MCP host) | Hosts the `mm` MCP server for your AI assistant |

## Quick Start

### 1. Create an Azure AD app registration

Every tenant you want to manage needs an app registration. This is how MM authenticates — there are no shared/default apps.

**Option A: Azure Portal (recommended for first-time setup)**

1. Go to [Azure Portal > App Registrations > New registration](https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/CreateApplicationBlade/quickStartType~/null/isMSAApp~/false)
2. Name it something like `MM-CLI` (or whatever you want)
3. Set **Supported account types** to "Accounts in this organizational directory only"
4. Leave **Redirect URI** blank (device code flow doesn't need one)
5. Click **Register**
6. Copy the **Application (client) ID** — this is your `appId`
7. Copy the **Directory (tenant) ID** — this is your `tenantId`
8. Go to **API permissions > Add a permission > Microsoft Graph > Delegated permissions** and add:

| Permission | For |
|------------|-----|
| `Mail.ReadWrite` | Email (read, send, manage) |
| `Calendars.ReadWrite` | Calendar operations |
| `Files.ReadWrite.All` | OneDrive / SharePoint files |
| `Sites.ReadWrite.All` | SharePoint sites |
| `User.Read` | Basic profile |
| `User.ReadBasic.All` | Look up other users |
| `Contacts.ReadWrite` | Contacts |
| `Tasks.ReadWrite` | To Do / Planner tasks |
| `Notes.ReadWrite.All` | OneNote |
| `Chat.ReadWrite` | Teams chat |
| `Team.ReadBasic.All` | Teams team info |
| `Channel.ReadBasic.All` | Teams channels |
| `ChannelMessage.Send` | Send Teams messages |

9. Click **Grant admin consent** (requires admin role)

> **No client secret needed.** MM uses device code flow (public client), not client credentials.

**Option B: CLI for Microsoft 365**

```bash
npm install -g @pnp/cli-microsoft365
m365 setup   # Creates the app registration interactively
```

See [docs/M365-CLI-SETUP.md](docs/M365-CLI-SETUP.md) for details.

### 2. Configure the connection registry

The connection registry tells MM which tenants exist and how to reach them.

```bash
# Create your first connection interactively
./mm-connections add Contoso-GA
```

This creates/updates `~/.m365-connections.json`. You can also create it manually:

```json
{
  "connections": {
    "Contoso-GA": {
      "appId": "your-app-client-id",
      "tenant": "contoso.com",
      "tenantId": "your-tenant-guid",
      "expectedEmail": "admin@contoso.com",
      "description": "Contoso Global Admin",
      "mcps": ["mm"]
    }
  }
}
```

**Connection fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `appId` | Yes | Application (client) ID from your app registration |
| `tenant` | Yes | Domain (`contoso.com`) or `tenantId` |
| `tenantId` | Yes | Directory (tenant) ID GUID |
| `expectedEmail` | Recommended | Expected sign-in email — MM warns if you auth as the wrong account |
| `description` | Yes | Human-readable label |
| `mcps` | Yes | Which MCP servers can use this connection (`["mm"]`) |
| `skipSignatureStrip` | No | Set `true` to skip email signature stripping (default: false) |

**Connection naming convention:**
- **GA** — Global Admin (tenant admin operations)
- **Individual** — Your user account on a work tenant
- **Personal** — Your own personal tenant

**Managing connections:**
```bash
./mm-connections list                          # List all connections
./mm-connections add Contoso-GA                # Add interactively
./mm-connections edit Contoso-GA appId abc-123  # Set a field
./mm-connections duplicate Contoso-GA Contoso-Individual  # Copy
./mm-connections remove Old-Connection         # Delete
```

### 3. Install Python dependencies

```bash
cd mm
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. Start the session pool

The session pool runs PowerShell modules (Exchange, SharePoint, Azure, Teams) in Docker containers.

```bash
cd session-pool
docker compose -p m365-session-pool -f docker-compose.unified.yml up -d
```

Verify it's running:
```bash
curl http://localhost:5200/health | jq
```

### 5. Register with your MCP host

```bash
cp mm/mcpjungle-config.example.json mm/mcpjungle-config.json
# Edit mm/mcpjungle-config.json — update paths to match your system
mcpjungle register --conf mm/mcpjungle-config.json
```

The example config expects a venv at `mm/.venv/bin/python`. Update the `command` and `args` paths to match your setup.

### 6. Use it

```bash
# List connections
mcpjungle invoke mm run '{}'

# PowerShell (Exchange)
mcpjungle invoke mm run '{"connection":"Contoso-GA","module":"exo","command":"Get-Mailbox -ResultSize 1"}'

# Graph API
mcpjungle invoke mm graph_request '{"connection":"Contoso-GA","endpoint":"/me"}'

# Power Automate (Flow API)
mcpjungle invoke mm graph_request '{"connection":"Contoso-GA","endpoint":"/providers/Microsoft.ProcessSimple/environments","resource":"flow"}'
```

**Auth is automatic.** If a connection isn't authenticated, the tool returns a device code:
```
DEVICE CODE: XXXXXXXX
Go to: https://microsoft.com/devicelogin
```
Complete the sign-in, then retry the command. No pre-auth step needed.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    AI Assistant                          │
└─────────────────────┬───────────────────────────────────┘
                      │ MCP Protocol (stdio)
                      ▼
               ┌─────────────┐
               │  mm/server  │  Python MCP server
               │             │  - graph_request (MSAL → Graph API)
               │             │  - run (HTTP → session pool)
               └──────┬──────┘
                      │ HTTP :5200
                      ▼
          ┌───────────────────────┐
          │   session-pool/       │  Docker container(s)
          │   session_pool.py     │  - PowerShell processes per module
          │                       │  - Native device code auth
          │   Modules:            │  - Session persistence
          │   exo, pnp, azure,    │  - Command guardrails
          │   teams               │  - Comprehensive logging
          └───────────────────────┘
                      │
          ┌───────────┴───────────┐
          │ ~/.m365-connections   │  Connection registry (READ-ONLY)
          │ ~/.mm-graph-tokens/   │  Graph MSAL token cache
          │ ~/.m365-logs/         │  Persistent logs
          │ ~/.m365-state/        │  Session state persistence
          └───────────────────────┘
```

## Tools

### `mm__run` — PowerShell via Session Pool

Execute PowerShell commands through persistent Docker-hosted sessions.

| Parameter | Description |
|-----------|-------------|
| `connection` | Connection name from registry |
| `module` | `exo` (Exchange), `pnp` (SharePoint), `azure`, `teams` |
| `command` | PowerShell command to execute |
| `confirmed` | Set `true` to bypass send guards (see [Send Guards](#send-guards)) |

Omit all parameters to list available connections.

### `mm__graph_request` — Microsoft Graph REST API

Direct HTTP requests to Microsoft Graph (or Flow API) via MSAL tokens.

| Parameter | Description |
|-----------|-------------|
| `connection` | Connection name |
| `endpoint` | API path (e.g., `/me/messages`) |
| `method` | `GET`, `POST`, `PATCH`, `PUT`, `DELETE` (default: GET) |
| `body` | Request body for POST/PATCH/PUT |
| `resource` | `graph` (default) or `flow` for Power Automate |
| `confirmed` | Set `true` to bypass send guards (see [Send Guards](#send-guards)) |

### Send Guards

Email and Teams message sends are **blocked by default**. When an AI assistant tries to send an email or Teams message, MM intercepts the request and returns a formatted draft preview instead. The assistant must re-call with `confirmed: true` to actually send.

**Guarded Graph endpoints:**
- `POST .../sendMail`, `.../reply`, `.../replyAll`, `.../forward`, `.../send`
- `POST /teams/{id}/channels/{id}/messages`, `/chats/{id}/messages`

**Guarded PowerShell commands:**
- `Send-MailMessage`, `Send-MgUserMail`
- `New-MgChatMessage`, `New-MgTeamChannelMessage`, `Submit-PnPTeamsChannelMessage`

## Session Pool

The session pool manages PowerShell processes with native device code authentication.

### Deployment Modes

| Mode | File | Use Case | RAM |
|------|------|----------|-----|
| **Unified** | `docker-compose.unified.yml` | Dev, small servers | ~2-4GB total |
| **Isolated** | `docker-compose.isolated.yml` | Production, multi-user | ~512MB per connection |

### Features

- **Session persistence** — Authenticated sessions survive container restarts. Session metadata saved to `~/.m365-state/`, PowerShell token caches persisted via Docker volumes. Azure sessions restore from cached tokens; EXO/Teams require re-auth (in-process only).
- **Command guardrails** — Blocks dangerous operations: `Install-Module` (container integrity), `New-AzRoleAssignment` (access escalation), raw OAuth requests, app registration modifications. Warned but allowed: `Remove-Az*`, mail forwarding rules.
- **Azure context isolation** — In unified mode, `Disable-AzContextAutosave` + `Select-AzContext` by expectedEmail prevents cross-tenant context contamination when multiple Azure sessions share `~/.Azure`.
- **Comprehensive logging** — Dual output: stdout (docker logs) + persistent files (`~/.m365-logs/`). Every command's output content logged. AADSTS and auth error patterns flagged at WARNING level.
- **Keepalive** — Background thread pings authenticated sessions every 5 minutes to prevent token expiry. Stale sessions (auth_pending > 15 min) automatically reaped.
- **Metrics** — `/metrics` endpoint with request counts, error rates, response times, session states.

### Session Pool API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/status` | GET | All session states |
| `/connections` | GET | List registry connections |
| `/run` | POST | Execute a command |
| `/reset` | POST | Reset a connection's sessions |
| `/metrics` | GET | Performance metrics |

### Host Directories

| Path | Purpose |
|------|---------|
| `~/.m365-connections.json` | Connection registry (read-only) |
| `~/.m365-logs/` | Persistent log files |
| `~/.m365-state/` | Session state for restart persistence |
| `~/.mm-graph-tokens/` | MSAL token cache for Graph API |

## Monitoring

```bash
# Container health
docker ps --format "{{.Names}}\t{{.Status}}" | grep m365

# Live logs
docker logs -f m365-pool

# Persistent logs (survive container restarts)
tail -f ~/.m365-logs/session-pool-unified.log

# Session status
curl http://localhost:5200/status | jq

# Metrics
curl http://localhost:5200/metrics | jq
```

## History

This repo originally contained four separate MCP servers (graph, pnp, pwsh-manager, registry), consolidated in February 2026 into the single `mm` server. The old servers are preserved in `_archived/` for reference.

## License

MIT
