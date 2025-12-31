import { z } from 'zod';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import AuthManager from './auth.js';
import GraphClient from './graph-client.js';

export function registerAuthTools(server: McpServer, authManager: AuthManager, graphClient?: GraphClient): void {
  server.tool(
    'login',
    'Authenticate with Microsoft using device code flow',
    {
      force: z.boolean().default(false).describe('Force a new login even if already logged in'),
    },
    async ({ force }) => {
      try {
        if (!force) {
          const loginStatus = await authManager.testLogin();
          if (loginStatus.success) {
            return {
              content: [
                {
                  type: 'text',
                  text: JSON.stringify({
                    status: 'Already logged in',
                    ...loginStatus,
                  }),
                },
              ],
            };
          }
        }

        const text = await new Promise<string>((resolve, reject) => {
          authManager.acquireTokenByDeviceCode(resolve).catch(reject);
        });
        return {
          content: [
            {
              type: 'text',
              text: JSON.stringify({
                error: 'device_code_required',
                message: text.trim(),
              }),
            },
          ],
        };
      } catch (error) {
        return {
          content: [
            {
              type: 'text',
              text: JSON.stringify({ error: `Authentication failed: ${(error as Error).message}` }),
            },
          ],
        };
      }
    }
  );

  server.tool('logout', 'Log out from Microsoft account', {}, async () => {
    try {
      await authManager.logout();
      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({ message: 'Logged out successfully' }),
          },
        ],
      };
    } catch {
      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({ error: 'Logout failed' }),
          },
        ],
      };
    }
  });

  server.tool('verify-login', 'Check current Microsoft authentication status', {}, async () => {
    const testResult = await authManager.testLogin();

    return {
      content: [
        {
          type: 'text',
          text: JSON.stringify(testResult),
        },
      ],
    };
  });

  server.tool('list-accounts', 'List all available Microsoft accounts', {}, async () => {
    try {
      const accounts = await authManager.listAccounts();
      const selectedAccountId = authManager.getSelectedAccountId();
      const result = accounts.map((account) => ({
        id: account.homeAccountId,
        username: account.username,
        name: account.name,
        selected: account.homeAccountId === selectedAccountId,
      }));

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({ accounts: result }),
          },
        ],
      };
    } catch (error) {
      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({ error: `Failed to list accounts: ${(error as Error).message}` }),
          },
        ],
      };
    }
  });

  server.tool(
    'select-account',
    'Select a specific Microsoft account to use',
    {
      accountId: z.string().describe('The account ID to select'),
    },
    async ({ accountId }) => {
      try {
        const success = await authManager.selectAccount(accountId);
        if (success) {
          return {
            content: [
              {
                type: 'text',
                text: JSON.stringify({ message: `Selected account: ${accountId}` }),
              },
            ],
          };
        } else {
          return {
            content: [
              {
                type: 'text',
                text: JSON.stringify({ error: `Account not found: ${accountId}` }),
              },
            ],
          };
        }
      } catch (error) {
        return {
          content: [
            {
              type: 'text',
              text: JSON.stringify({
                error: `Failed to select account: ${(error as Error).message}`,
              }),
            },
          ],
        };
      }
    }
  );

  server.tool(
    'remove-account',
    'Remove a Microsoft account from the cache',
    {
      accountId: z.string().describe('The account ID to remove'),
    },
    async ({ accountId }) => {
      try {
        const success = await authManager.removeAccount(accountId);
        if (success) {
          return {
            content: [
              {
                type: 'text',
                text: JSON.stringify({ message: `Removed account: ${accountId}` }),
              },
            ],
          };
        } else {
          return {
            content: [
              {
                type: 'text',
                text: JSON.stringify({ error: `Account not found: ${accountId}` }),
              },
            ],
          };
        }
      } catch (error) {
        return {
          content: [
            {
              type: 'text',
              text: JSON.stringify({
                error: `Failed to remove account: ${(error as Error).message}`,
              }),
            },
          ],
        };
      }
    }
  );

  // Only register graph-request if graphClient is provided
  if (graphClient) {
    server.tool(
      'graph-request',
      `Execute a raw Microsoft Graph API request. Supports any Graph API endpoint. Can target specific account without switching.

Documentation:
- Graph API Reference: https://learn.microsoft.com/en-us/graph/api/overview
- Common endpoints: /me, /users, /groups, /me/messages, /me/calendar/events, /me/drive
- OData query params: $select, $filter, $top, $orderby, $expand, $count, $search
- Permissions reference: https://learn.microsoft.com/en-us/graph/permissions-reference`,
      {
        endpoint: z.string().describe('The Graph API endpoint path (e.g., "/me", "/users", "/me/messages")'),
        method: z.enum(['GET', 'POST', 'PUT', 'PATCH', 'DELETE']).default('GET').describe('HTTP method'),
        body: z.any().optional().describe('Request body for POST/PUT/PATCH requests (JSON object)'),
        queryParams: z.record(z.string()).optional().describe('Query parameters (e.g., {"$select": "displayName", "$top": "10"})'),
        headers: z.record(z.string()).optional().describe('Additional headers to include'),
        apiVersion: z.enum(['v1.0', 'beta']).default('v1.0').describe('Graph API version'),
        accountId: z.string().optional().describe('Target a specific account by ID without switching. Use list-accounts to see available IDs.'),
      },
      async ({ endpoint, method, body, queryParams, headers, apiVersion, accountId }) => {
        try {
          // REQUIRE accountId when multiple accounts exist
          const accounts = await authManager.listAccounts();
          if (accounts.length > 1 && !accountId) {
            const available = accounts.map(a => `"${a.username}" (${a.homeAccountId.substring(0, 8)}...)`).join(', ');
            return {
              content: [
                {
                  type: 'text',
                  text: JSON.stringify({
                    error: `Multiple accounts exist. You MUST specify accountId.`,
                    available: accounts.map(a => ({ username: a.username, id: a.homeAccountId })),
                    hint: `Available: ${available}`,
                  }),
                },
              ],
              isError: true,
            };
          }

          // Get token for specific account if requested
          let accessToken: string | undefined;
          if (accountId) {
            const token = await authManager.getTokenForAccount(accountId);
            if (!token) {
              return {
                content: [
                  {
                    type: 'text',
                    text: JSON.stringify({
                      error: `Failed to get token for account: ${accountId}. Use list-accounts to see available accounts.`,
                    }),
                  },
                ],
                isError: true,
              };
            }
            accessToken = token;
          }

          // Build the full path with query params
          let path = endpoint.startsWith('/') ? endpoint : `/${endpoint}`;

          // Use beta API if requested
          if (apiVersion === 'beta') {
            path = path.replace(/^\//, '/beta/').replace('/beta//', '/beta/');
          }

          if (queryParams && Object.keys(queryParams).length > 0) {
            const params = new URLSearchParams(queryParams).toString();
            path = `${path}${path.includes('?') ? '&' : '?'}${params}`;
          }

          const options: any = {
            method: method || 'GET',
            headers: headers || {},
          };

          if (body && ['POST', 'PUT', 'PATCH'].includes(method || 'GET')) {
            options.body = typeof body === 'string' ? body : JSON.stringify(body);
          }

          // Add access token if targeting specific account
          if (accessToken) {
            options.accessToken = accessToken;
          }

          const result = await graphClient.graphRequest(path, options);
          return result;
        } catch (error) {
          return {
            content: [
              {
                type: 'text',
                text: JSON.stringify({
                  error: `Graph API request failed: ${(error as Error).message}`,
                }),
              },
            ],
            isError: true,
          };
        }
      }
    );
  }
}
