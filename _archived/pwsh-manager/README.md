# PowerShell Session Manager

Persistent PowerShell sessions for Microsoft 365 and Azure, exposed via MCP (Model Context Protocol).

## Problem

PowerShell modules like `ExchangeOnlineManagement` store auth tokens in memory. When the PowerShell process exits, you must re-authenticate. This is painful when working with AI assistants that spawn new processes for each command.

## Solution

A Docker container running a long-lived daemon that:
- Maintains persistent PowerShell sessions per tenant/module
- Exposes an HTTP API for executing commands
- Handles device code authentication flow
- Sessions survive until container restarts (~90 day token lifetime)

## Supported Modules

| Module | Service | Use For |
|--------|---------|---------|
| `exo` | Exchange Online | Mailboxes, transport rules, compliance |
| `pnp` | PnP PowerShell | SharePoint, Teams, site administration |
| `azure` | Azure PowerShell | Azure resources, subscriptions |
| `powerplatform` | Power Platform | Power Apps, Power Automate environments |

## Quick Start

### 1. Start the Docker container

```bash
cd mcp-servers/pwsh-manager
docker-compose up -d
```

### 2. Register with your MCP client

**For MCPJungle:**
```bash
mcpjungle add ./mcpjungle-config.json
```

**For Claude Desktop** (`~/.config/claude/claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "pwsh-manager": {
      "command": "python3",
      "args": ["/path/to/mcp_server.py"],
      "env": {
        "PWSH_MANAGER_URL": "http://localhost:5100"
      }
    }
  }
}
```

### 3. Authenticate

```
> pwsh_login tenant=contoso.com module=exo

**DEVICE CODE: ABC123XYZ**
Go to: https://microsoft.com/devicelogin
```

### 4. Run commands

```
> pwsh_run tenant=contoso.com module=exo command="Get-Mailbox -ResultSize 10"
```

## API Reference

### HTTP Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/modules` | GET | List supported modules |
| `/sessions` | GET | List sessions |
| `/login` | POST | Initiate authentication |
| `/status` | POST | Check connection status |
| `/run` | POST | Execute PowerShell command |
| `/disconnect` | POST | Disconnect a session |

### MCP Tools

| Tool | Description |
|------|-------------|
| `pwsh_login` | Authenticate to a service (returns device code) |
| `pwsh_status` | Check if authenticated |
| `pwsh_run` | Execute PowerShell command |
| `pwsh_sessions` | List all sessions |
| `pwsh_disconnect` | Disconnect a session |

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PWSH_MANAGER_URL` | `http://localhost:5100` | Session manager URL (MCP client) |
| `PWSH_MANAGER_PORT` | `5100` | HTTP API port (Docker) |
| `PWSH_MANAGER_LOG_LEVEL` | `INFO` | Logging level |
| `PWSH_MANAGER_TOKEN_DIR` | `/data/tokens` | Token cache directory |

### Tenant Aliases

Edit `mcp_server.py` to add convenient aliases:

```python
TENANT_ALIASES = {
    "contoso": "contoso.com",
    "personal": "yourdomain.com",
}
```

Then use: `pwsh_run tenant=contoso command="Get-Mailbox"`

### SharePoint Tenant Mapping

If your SharePoint URL differs from your tenant domain prefix, add a mapping:

```python
# Maps tenant domain -> SharePoint tenant prefix
SHAREPOINT_TENANTS = {
    "contoso.com": "contoso",  # Uses contoso.sharepoint.com
}
```

This is needed when your organization's SharePoint URL (e.g., `contoso.sharepoint.com`)
doesn't match the tenant domain prefix (e.g., `contoso.com` → would default to `contoso.sharepoint.com`).

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Docker Container (pwsh-manager)                │
│  ┌───────────────────────────────────────────┐  │
│  │  Session Manager Daemon (Python/Flask)    │  │
│  │  HTTP API :5100                           │  │
│  │                                           │  │
│  │  Sessions:                                │  │
│  │   ├─ tenant1:exo  → pwsh process 1       │  │
│  │   ├─ tenant1:pnp  → pwsh process 2       │  │
│  │   └─ tenant2:exo  → pwsh process 3       │  │
│  └───────────────────────────────────────────┘  │
│  Volume: /data/tokens                           │
└─────────────────────────────────────────────────┘
                     │ HTTP
                     ▼
┌─────────────────────────────────────────────────┐
│  MCP Server (mcp_server.py)                     │
│  Translates MCP ↔ HTTP                          │
└─────────────────────────────────────────────────┘
                     │ stdio
                     ▼
              Claude / AI Agent
```

## Multi-Tenant Support

Each `tenant:module` combination gets its own PowerShell process. You can have multiple tenants authenticated simultaneously:

```
pwsh_login tenant=contoso.com module=exo
pwsh_login tenant=fabrikam.com module=exo
pwsh_login tenant=contoso.com module=pnp

pwsh_sessions
# ✓ contoso.com (exo) - Connected
# ✓ fabrikam.com (exo) - Connected
# ✓ contoso.com (pnp) - Connected
```

## Development

### Run locally (without Docker)

```bash
# Install dependencies
pip install flask gunicorn requests

# Start session manager
PWSH_MANAGER_DEBUG=true python session_manager.py

# In another terminal, test
curl http://localhost:5100/health
curl -X POST http://localhost:5100/login -H "Content-Type: application/json" -d '{"tenant":"contoso.com","module":"exo"}'
```

### Build Docker image

```bash
docker build -t pwsh-manager .
docker run -p 5100:5100 pwsh-manager
```

## Troubleshooting

### "Cannot connect to pwsh-manager"
- Check Docker is running: `docker ps`
- Check container logs: `docker logs pwsh-manager`

### "Not authenticated"
- Run `pwsh_login` first
- Complete device code flow at https://microsoft.com/devicelogin
- Check status with `pwsh_status`

### Session expired
- Tokens last ~90 days
- Re-run `pwsh_login` to refresh

## License

MIT
