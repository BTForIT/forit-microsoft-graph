<p align="center">
  <img src="logo.png" alt="ForIT Logo" width="350">
</p>

# ForIT Microsoft Graph

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A lean MCP (Model Context Protocol) server for raw Microsoft Graph API access with **multi-tenant account management**.

## The Problem

AI assistants using Microsoft Graph face two challenges:

1. **Context bloat** - Existing MCPs expose 30-40 specialized tools, consuming ~12KB of context per conversation
2. **Multi-tenant friction** - Managing multiple Microsoft 365 accounts requires constant switching

## The Solution

**7 tools. ~1KB context. Direct tenant access.**

| Tool | Description |
|------|-------------|
| `login` | Authenticate with Microsoft (device code flow) |
| `logout` | Log out from Microsoft account |
| `verify-login` | Check authentication status |
| `list-accounts` | List all cached Microsoft accounts |
| `select-account` | Switch default account |
| `remove-account` | Remove an account from cache |
| `graph-request` | **Execute any Graph API request** |

### Why This Approach?

Instead of 30+ specialized tools like `list-mail-messages`, `create-calendar-event`, `get-user`, etc., we expose **one flexible tool** that can call any Graph API endpoint. The AI already knows the Graph API - it doesn't need hand-holding.

**Before (Softeria, 37 tools):**
```
list-mail-messages, get-mail-message, send-mail, create-draft-email,
list-calendar-events, create-calendar-event, get-calendar-event,
list-users, get-user, get-current-user, list-calendars...
```

**After (ForIT, 1 tool):**
```json
{ "endpoint": "/me/messages", "queryParams": { "$top": "10" } }
```

### Multi-Tenant Without Switching

The killer feature: **reference any account directly without switching**.

```json
{
  "endpoint": "/me/calendar/events",
  "accountId": "work-tenant-id"
}
```

```json
{
  "endpoint": "/me/calendar/events",
  "accountId": "personal-tenant-id"
}
```

Query both tenants in the same conversation. No `select-account` dance required.

---

## Installation

```bash
npm install -g @foritllc/microsoft-graph
```

## Quick Start

### With Claude Desktop / MCP Client

```json
{
  "mcpServers": {
    "graph": {
      "command": "npx",
      "args": ["-y", "@foritllc/microsoft-graph"]
    }
  }
}
```

### Authentication (Device Code)

**Device code authentication is used**, which is the most reliable method for:
- SSH sessions where browser auth isn't available
- Headless servers
- Remote development environments
- AI agent automation

The device code is displayed prominently in a box format for visibility.

### Login to Multiple Tenants

```bash
# First tenant (device code displayed clearly)
npx @foritllc/microsoft-graph --login

# Second tenant (adds to cache)
npx @foritllc/microsoft-graph --login

# See all accounts
npx @foritllc/microsoft-graph --list-accounts
```

### Multi-Account Requirement

When multiple accounts exist, you **must** specify which account to use:

```json
{
  "endpoint": "/me",
  "accountId": "abc123..."
}
```

Without `accountId`, requests will error showing available accounts. Use `list-accounts` tool to see account IDs.

---

## The `graph-request` Tool

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `endpoint` | string | Graph API path (e.g., `/me`, `/users/{id}`, `/me/messages`) |
| `method` | enum | `GET`, `POST`, `PUT`, `PATCH`, `DELETE` (default: GET) |
| `body` | object | Request body for POST/PUT/PATCH |
| `queryParams` | object | OData params (`$select`, `$filter`, `$top`, `$orderby`, etc.) |
| `headers` | object | Additional HTTP headers |
| `apiVersion` | enum | `v1.0` (default) or `beta` |
| `accountId` | string | Target specific tenant without switching |

### Examples

**Get current user:**
```json
{ "endpoint": "/me" }
```

**List unread emails:**
```json
{
  "endpoint": "/me/messages",
  "queryParams": {
    "$filter": "isRead eq false",
    "$select": "subject,from,receivedDateTime",
    "$top": "10",
    "$orderby": "receivedDateTime desc"
  }
}
```

**Create calendar event:**
```json
{
  "endpoint": "/me/calendar/events",
  "method": "POST",
  "body": {
    "subject": "Team Sync",
    "start": { "dateTime": "2025-01-15T10:00:00", "timeZone": "UTC" },
    "end": { "dateTime": "2025-01-15T11:00:00", "timeZone": "UTC" }
  }
}
```

**Use beta API:**
```json
{
  "endpoint": "/me/insights/used",
  "apiVersion": "beta"
}
```

**Cross-tenant query:**
```json
{
  "endpoint": "/me/drive/root/children",
  "accountId": "client-tenant-abc123"
}
```

---

## Use With PnP CLI

This MCP complements [PnP CLI for Microsoft 365](https://pnp.github.io/cli-microsoft365/):

| Use Case | Tool |
|----------|------|
| Raw Graph API calls | **ForIT Microsoft Graph** |
| SharePoint, Teams, Power Platform | PnP CLI |
| Multi-tenant management | **ForIT Microsoft Graph** |
| Specialized admin commands | PnP CLI |

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `MS365_MCP_CLIENT_ID` | Azure AD app client ID (optional) |
| `MS365_MCP_CLIENT_SECRET` | Client secret for confidential apps |
| `MS365_MCP_TENANT_ID` | Azure AD tenant ID (default: `common`) |

---

## Credits

Fork of [@softeria/ms-365-mcp-server](https://github.com/Softeria/ms-365-mcp-server), rebuilt with a different philosophy: less is more.

## License

MIT - See [LICENSE](LICENSE)
