import type { AccountInfo, Configuration } from '@azure/msal-node';
import { PublicClientApplication } from '@azure/msal-node';
import logger from './logger.js';
import fs, { existsSync, readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import path from 'path';

// Ok so this is a hack to lazily import keytar only when needed
// since --http mode may not need it at all, and keytar can be a pain to install (looking at you alpine)
let keytar: typeof import('keytar') | null = null;
async function getKeytar() {
  if (keytar === undefined) {
    return null;
  }
  if (keytar === null) {
    try {
      keytar = await import('keytar');
      return keytar;
    } catch (error) {
      logger.info('keytar not available, using file-based credential storage');
      keytar = undefined as any;
      return null;
    }
  }
  return keytar;
}

interface EndpointConfig {
  pathPattern: string;
  method: string;
  toolName: string;
  scopes?: string[];
  workScopes?: string[];
  llmTip?: string;
}

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const endpointsData = JSON.parse(
  readFileSync(path.join(__dirname, 'endpoints.json'), 'utf8')
) as EndpointConfig[];

const endpoints = {
  default: endpointsData,
};

const SERVICE_NAME = 'ms-365-mcp-server';
const TOKEN_CACHE_ACCOUNT = 'msal-token-cache';
const SELECTED_ACCOUNT_KEY = 'selected-account';
const ACCOUNT_METADATA_KEY = 'account-metadata';
const FALLBACK_DIR = path.dirname(fileURLToPath(import.meta.url));
const FALLBACK_PATH = path.join(FALLBACK_DIR, '..', '.token-cache.json');
const SELECTED_ACCOUNT_PATH = path.join(FALLBACK_DIR, '..', '.selected-account.json');
const ACCOUNT_METADATA_PATH = path.join(FALLBACK_DIR, '..', '.account-metadata.json');

// Maps accountId -> { appId, tenantId } for multi-tenant support
interface AccountMetadata {
  appId: string;
  tenantId: string;
}

const DEFAULT_CONFIG: Configuration = {
  auth: {
    clientId: process.env.MS365_MCP_CLIENT_ID || '084a3e9f-a9f4-43f7-89f9-d229cf97853e',
    authority: `https://login.microsoftonline.com/${process.env.MS365_MCP_TENANT_ID || 'common'}`,
  },
};

interface ScopeHierarchy {
  [key: string]: string[];
}

const SCOPE_HIERARCHY: ScopeHierarchy = {
  'Mail.ReadWrite': ['Mail.Read'],
  'Calendars.ReadWrite': ['Calendars.Read'],
  'Files.ReadWrite': ['Files.Read'],
  'Tasks.ReadWrite': ['Tasks.Read'],
  'Contacts.ReadWrite': ['Contacts.Read'],
};

function buildScopesFromEndpoints(
  includeWorkAccountScopes: boolean = false,
  enabledToolsPattern?: string
): string[] {
  const scopesSet = new Set<string>();

  // Create regex for tool filtering if pattern is provided
  let enabledToolsRegex: RegExp | undefined;
  if (enabledToolsPattern) {
    try {
      enabledToolsRegex = new RegExp(enabledToolsPattern, 'i');
      logger.info(`Building scopes with tool filter pattern: ${enabledToolsPattern}`);
    } catch (error) {
      logger.error(
        `Invalid tool filter regex pattern: ${enabledToolsPattern}. Building scopes without filter.`
      );
    }
  }

  endpoints.default.forEach((endpoint) => {
    // Skip endpoints that don't match the tool filter
    if (enabledToolsRegex && !enabledToolsRegex.test(endpoint.toolName)) {
      return;
    }

    // Skip endpoints that only have workScopes if not in work mode
    if (!includeWorkAccountScopes && !endpoint.scopes && endpoint.workScopes) {
      return;
    }

    // Add regular scopes
    if (endpoint.scopes && Array.isArray(endpoint.scopes)) {
      endpoint.scopes.forEach((scope) => scopesSet.add(scope));
    }

    // Add workScopes if in work mode
    if (includeWorkAccountScopes && endpoint.workScopes && Array.isArray(endpoint.workScopes)) {
      endpoint.workScopes.forEach((scope) => scopesSet.add(scope));
    }
  });

  Object.entries(SCOPE_HIERARCHY).forEach(([higherScope, lowerScopes]) => {
    if (lowerScopes.every((scope) => scopesSet.has(scope))) {
      lowerScopes.forEach((scope) => scopesSet.delete(scope));
      scopesSet.add(higherScope);
    }
  });

  const scopes = Array.from(scopesSet);
  if (enabledToolsPattern) {
    logger.info(`Built ${scopes.length} scopes for filtered tools: ${scopes.join(', ')}`);
  }

  return scopes;
}

interface LoginTestResult {
  success: boolean;
  message: string;
  userData?: {
    displayName: string;
    userPrincipalName: string;
  };
}

class AuthManager {
  private config: Configuration;
  private scopes: string[];
  private msalApp: PublicClientApplication;
  private accessToken: string | null;
  private tokenExpiry: number | null;
  private oauthToken: string | null;
  private isOAuthMode: boolean;
  private selectedAccountId: string | null;
  private accountMetadata: Map<string, AccountMetadata> = new Map();

  constructor(
    config: Configuration = DEFAULT_CONFIG,
    scopes: string[] = buildScopesFromEndpoints()
  ) {
    logger.info(`And scopes are ${scopes.join(', ')}`, scopes);
    this.config = config;
    this.scopes = scopes;
    this.msalApp = new PublicClientApplication(this.config);
    this.accessToken = null;
    this.tokenExpiry = null;
    this.selectedAccountId = null;

    const oauthTokenFromEnv = process.env.MS365_MCP_OAUTH_TOKEN;
    this.oauthToken = oauthTokenFromEnv ?? null;
    this.isOAuthMode = oauthTokenFromEnv != null;
  }

  async loadTokenCache(): Promise<void> {
    try {
      let cacheData: string | undefined;

      try {
        const kt = await getKeytar();
        if (kt) {
          const cachedData = await kt.getPassword(SERVICE_NAME, TOKEN_CACHE_ACCOUNT);
          if (cachedData) {
            cacheData = cachedData;
          }
        }
      } catch (keytarError) {
        logger.warn(
          `Keychain access failed, falling back to file storage: ${(keytarError as Error).message}`
        );
      }

      if (!cacheData && existsSync(FALLBACK_PATH)) {
        cacheData = readFileSync(FALLBACK_PATH, 'utf8');
      }

      if (cacheData) {
        this.msalApp.getTokenCache().deserialize(cacheData);
      }

      // Load selected account
      await this.loadSelectedAccount();
      // Load account metadata (appId/tenantId per account)
      await this.loadAccountMetadata();
    } catch (error) {
      logger.error(`Error loading token cache: ${(error as Error).message}`);
    }
  }

  private async loadAccountMetadata(): Promise<void> {
    try {
      let metadataStr: string | undefined;

      try {
        const kt = await getKeytar();
        if (kt) {
          const cachedData = await kt.getPassword(SERVICE_NAME, ACCOUNT_METADATA_KEY);
          if (cachedData) {
            metadataStr = cachedData;
          }
        }
      } catch (keytarError) {
        logger.warn(`Keychain access failed for account metadata: ${(keytarError as Error).message}`);
      }

      if (!metadataStr && existsSync(ACCOUNT_METADATA_PATH)) {
        metadataStr = readFileSync(ACCOUNT_METADATA_PATH, 'utf8');
      }

      if (metadataStr) {
        const parsed = JSON.parse(metadataStr) as Record<string, AccountMetadata>;
        this.accountMetadata = new Map(Object.entries(parsed));
        logger.info(`Loaded metadata for ${this.accountMetadata.size} accounts`);
      }
    } catch (error) {
      logger.error(`Error loading account metadata: ${(error as Error).message}`);
    }
  }

  private async saveAccountMetadata(): Promise<void> {
    try {
      const metadataObj = Object.fromEntries(this.accountMetadata);
      const metadataStr = JSON.stringify(metadataObj);

      try {
        const kt = await getKeytar();
        if (kt) {
          await kt.setPassword(SERVICE_NAME, ACCOUNT_METADATA_KEY, metadataStr);
        } else {
          fs.writeFileSync(ACCOUNT_METADATA_PATH, metadataStr, { mode: 0o600 });
        }
      } catch (keytarError) {
        logger.warn(`Keychain save failed for account metadata: ${(keytarError as Error).message}`);
        fs.writeFileSync(ACCOUNT_METADATA_PATH, metadataStr, { mode: 0o600 });
      }
    } catch (error) {
      logger.error(`Error saving account metadata: ${(error as Error).message}`);
    }
  }

  private async loadSelectedAccount(): Promise<void> {
    try {
      let selectedAccountData: string | undefined;

      try {
        const kt = await getKeytar();
        if (kt) {
          const cachedData = await kt.getPassword(SERVICE_NAME, SELECTED_ACCOUNT_KEY);
          if (cachedData) {
            selectedAccountData = cachedData;
          }
        }
      } catch (keytarError) {
        logger.warn(
          `Keychain access failed for selected account, falling back to file storage: ${(keytarError as Error).message}`
        );
      }

      if (!selectedAccountData && existsSync(SELECTED_ACCOUNT_PATH)) {
        selectedAccountData = readFileSync(SELECTED_ACCOUNT_PATH, 'utf8');
      }

      if (selectedAccountData) {
        const parsed = JSON.parse(selectedAccountData);
        this.selectedAccountId = parsed.accountId;
        logger.info(`Loaded selected account: ${this.selectedAccountId}`);
      }
    } catch (error) {
      logger.error(`Error loading selected account: ${(error as Error).message}`);
    }
  }

  async saveTokenCache(): Promise<void> {
    try {
      const cacheData = this.msalApp.getTokenCache().serialize();

      try {
        const kt = await getKeytar();
        if (kt) {
          await kt.setPassword(SERVICE_NAME, TOKEN_CACHE_ACCOUNT, cacheData);
        } else {
          fs.writeFileSync(FALLBACK_PATH, cacheData, { mode: 0o600 });
        }
      } catch (keytarError) {
        logger.warn(
          `Keychain save failed, falling back to file storage: ${(keytarError as Error).message}`
        );

        fs.writeFileSync(FALLBACK_PATH, cacheData, { mode: 0o600 });
      }
    } catch (error) {
      logger.error(`Error saving token cache: ${(error as Error).message}`);
    }
  }

  private async saveSelectedAccount(): Promise<void> {
    try {
      const selectedAccountData = JSON.stringify({ accountId: this.selectedAccountId });

      try {
        const kt = await getKeytar();
        if (kt) {
          await kt.setPassword(SERVICE_NAME, SELECTED_ACCOUNT_KEY, selectedAccountData);
        } else {
          fs.writeFileSync(SELECTED_ACCOUNT_PATH, selectedAccountData, { mode: 0o600 });
        }
      } catch (keytarError) {
        logger.warn(
          `Keychain save failed for selected account, falling back to file storage: ${(keytarError as Error).message}`
        );

        fs.writeFileSync(SELECTED_ACCOUNT_PATH, selectedAccountData, { mode: 0o600 });
      }
    } catch (error) {
      logger.error(`Error saving selected account: ${(error as Error).message}`);
    }
  }

  async setOAuthToken(token: string): Promise<void> {
    this.oauthToken = token;
    this.isOAuthMode = true;
  }

  async getToken(forceRefresh = false): Promise<string | null> {
    if (this.isOAuthMode && this.oauthToken) {
      return this.oauthToken;
    }

    if (this.accessToken && this.tokenExpiry && this.tokenExpiry > Date.now() && !forceRefresh) {
      return this.accessToken;
    }

    const currentAccount = await this.getCurrentAccount();

    if (currentAccount) {
      // Get the appId/tenantId that was used to authenticate this account
      const metadata = this.accountMetadata.get(currentAccount.homeAccountId);
      let msalApp = this.msalApp;

      if (metadata) {
        // Create MSAL app with the correct appId/tenantId for this account
        const customConfig: Configuration = {
          auth: {
            clientId: metadata.appId,
            authority: `https://login.microsoftonline.com/${metadata.tenantId}`,
          },
        };
        msalApp = new PublicClientApplication(customConfig);
        // Load the token cache
        const cacheData = this.msalApp.getTokenCache().serialize();
        msalApp.getTokenCache().deserialize(cacheData);
        logger.info(`Using custom app for token refresh: appId=${metadata.appId}, tenant=${metadata.tenantId}`);
      }

      const silentRequest = {
        account: currentAccount,
        scopes: this.scopes,
      };

      try {
        const response = await msalApp.acquireTokenSilent(silentRequest);
        this.accessToken = response.accessToken;
        this.tokenExpiry = response.expiresOn ? new Date(response.expiresOn).getTime() : null;
        return this.accessToken;
      } catch {
        logger.error('Silent token acquisition failed');
        throw new Error('Silent token acquisition failed');
      }
    }

    throw new Error('No valid token found');
  }

  async getCurrentAccount(): Promise<AccountInfo | null> {
    const accounts = await this.msalApp.getTokenCache().getAllAccounts();

    if (accounts.length === 0) {
      return null;
    }

    // If a specific account is selected, find it
    if (this.selectedAccountId) {
      const selectedAccount = accounts.find(
        (account: AccountInfo) => account.homeAccountId === this.selectedAccountId
      );
      if (selectedAccount) {
        return selectedAccount;
      }
      logger.warn(
        `Selected account ${this.selectedAccountId} not found, falling back to first account`
      );
    }

    // Fall back to first account (backward compatibility)
    return accounts[0];
  }

  async acquireTokenByDeviceCode(
    hack?: (message: string) => void,
    options?: { appId?: string; tenantId?: string }
  ): Promise<string | null> {
    // Use custom app/tenant if provided, otherwise use defaults
    let msalApp = this.msalApp;
    if (options?.appId || options?.tenantId) {
      const customConfig: Configuration = {
        auth: {
          clientId: options.appId || this.config.auth.clientId,
          authority: `https://login.microsoftonline.com/${options.tenantId || 'common'}`,
        },
      };
      msalApp = new PublicClientApplication(customConfig);
      // Load existing token cache into custom app
      try {
        const cacheData = this.msalApp.getTokenCache().serialize();
        msalApp.getTokenCache().deserialize(cacheData);
      } catch {
        // Ignore cache load errors for custom app
      }
    }

    const deviceCodeRequest = {
      scopes: this.scopes,
      deviceCodeCallback: (response: { message: string }) => {
        // Extract device code from message
        const codeMatch = response.message.match(/code\s+([A-Z0-9]+)\s+to authenticate/i);
        const deviceCode = codeMatch ? codeMatch[1] : 'CHECK MESSAGE BELOW';

        const clearText = `
████████████████████████████████████████████████████████████
██                                                        ██
██   DEVICE CODE: ${deviceCode.padEnd(12)}                       ██
██                                                        ██
██   Go to: https://microsoft.com/devicelogin            ██
██                                                        ██
████████████████████████████████████████████████████████████

${response.message}

After login run the "verify login" command
`;
        if (hack) {
          hack(clearText);
        } else {
          console.log(clearText);
        }
        logger.info('Device code login initiated');
      },
    };

    try {
      logger.info('Requesting device code...');
      logger.info(`Requesting scopes: ${this.scopes.join(', ')}`);
      logger.info(`Using app: ${options?.appId || this.config.auth.clientId}, tenant: ${options?.tenantId || 'default'}`);
      const response = await msalApp.acquireTokenByDeviceCode(deviceCodeRequest);
      logger.info(`Granted scopes: ${response?.scopes?.join(', ') || 'none'}`);
      logger.info('Device code login successful');
      this.accessToken = response?.accessToken || null;
      this.tokenExpiry = response?.expiresOn ? new Date(response.expiresOn).getTime() : null;

      // If using custom app, copy tokens back to main cache
      if (options?.appId || options?.tenantId) {
        const newCacheData = msalApp.getTokenCache().serialize();
        this.msalApp.getTokenCache().deserialize(newCacheData);
      }

      // Save account metadata (appId/tenantId) for this account
      if (response?.account) {
        const accountId = response.account.homeAccountId;
        const appIdUsed = options?.appId || this.config.auth.clientId;
        const tenantIdUsed = options?.tenantId || 'common';
        this.accountMetadata.set(accountId, { appId: appIdUsed, tenantId: tenantIdUsed });
        await this.saveAccountMetadata();
        logger.info(`Saved metadata for account ${response.account.username}: appId=${appIdUsed}, tenant=${tenantIdUsed}`);
      }

      // Set the newly authenticated account as selected if no account is currently selected
      if (!this.selectedAccountId && response?.account) {
        this.selectedAccountId = response.account.homeAccountId;
        await this.saveSelectedAccount();
        logger.info(`Auto-selected new account: ${response.account.username}`);
      }

      await this.saveTokenCache();
      return this.accessToken;
    } catch (error) {
      logger.error(`Error in device code flow: ${(error as Error).message}`);
      throw error;
    }
  }

  async testLogin(): Promise<LoginTestResult> {
    try {
      logger.info('Testing login...');
      const token = await this.getToken();

      if (!token) {
        logger.error('Login test failed - no token received');
        return {
          success: false,
          message: 'Login failed - no token received',
        };
      }

      logger.info('Token retrieved successfully, testing Graph API access...');

      try {
        const response = await fetch('https://graph.microsoft.com/v1.0/me', {
          headers: {
            Authorization: `Bearer ${token}`,
          },
        });

        if (response.ok) {
          const userData = await response.json();
          logger.info('Graph API user data fetch successful');
          return {
            success: true,
            message: 'Login successful',
            userData: {
              displayName: userData.displayName,
              userPrincipalName: userData.userPrincipalName,
            },
          };
        } else {
          const errorText = await response.text();
          logger.error(`Graph API user data fetch failed: ${response.status} - ${errorText}`);
          return {
            success: false,
            message: `Login successful but Graph API access failed: ${response.status}`,
          };
        }
      } catch (graphError) {
        logger.error(`Error fetching user data: ${(graphError as Error).message}`);
        return {
          success: false,
          message: `Login successful but Graph API access failed: ${(graphError as Error).message}`,
        };
      }
    } catch (error) {
      logger.error(`Login test failed: ${(error as Error).message}`);
      return {
        success: false,
        message: `Login failed: ${(error as Error).message}`,
      };
    }
  }

  async logout(): Promise<boolean> {
    try {
      const accounts = await this.msalApp.getTokenCache().getAllAccounts();
      for (const account of accounts) {
        await this.msalApp.getTokenCache().removeAccount(account);
      }
      this.accessToken = null;
      this.tokenExpiry = null;
      this.selectedAccountId = null;

      try {
        const kt = await getKeytar();
        if (kt) {
          await kt.deletePassword(SERVICE_NAME, TOKEN_CACHE_ACCOUNT);
          await kt.deletePassword(SERVICE_NAME, SELECTED_ACCOUNT_KEY);
        }
      } catch (keytarError) {
        logger.warn(`Keychain deletion failed: ${(keytarError as Error).message}`);
      }

      if (fs.existsSync(FALLBACK_PATH)) {
        fs.unlinkSync(FALLBACK_PATH);
      }

      if (fs.existsSync(SELECTED_ACCOUNT_PATH)) {
        fs.unlinkSync(SELECTED_ACCOUNT_PATH);
      }

      return true;
    } catch (error) {
      logger.error(`Error during logout: ${(error as Error).message}`);
      throw error;
    }
  }

  // Multi-account support methods
  async listAccounts(): Promise<AccountInfo[]> {
    return await this.msalApp.getTokenCache().getAllAccounts();
  }

  async selectAccount(accountId: string): Promise<boolean> {
    const accounts = await this.listAccounts();
    const account = accounts.find((acc: AccountInfo) => acc.homeAccountId === accountId);

    if (!account) {
      logger.error(`Account with ID ${accountId} not found`);
      return false;
    }

    this.selectedAccountId = accountId;
    await this.saveSelectedAccount();

    // Clear cached tokens to force refresh with new account
    this.accessToken = null;
    this.tokenExpiry = null;

    logger.info(`Selected account: ${account.username} (${accountId})`);
    return true;
  }

  async removeAccount(accountId: string): Promise<boolean> {
    const accounts = await this.listAccounts();
    const account = accounts.find((acc: AccountInfo) => acc.homeAccountId === accountId);

    if (!account) {
      logger.error(`Account with ID ${accountId} not found`);
      return false;
    }

    try {
      await this.msalApp.getTokenCache().removeAccount(account);

      // If this was the selected account, clear the selection
      if (this.selectedAccountId === accountId) {
        this.selectedAccountId = null;
        await this.saveSelectedAccount();
        this.accessToken = null;
        this.tokenExpiry = null;
      }

      logger.info(`Removed account: ${account.username} (${accountId})`);
      return true;
    } catch (error) {
      logger.error(`Failed to remove account ${accountId}: ${(error as Error).message}`);
      return false;
    }
  }

  getSelectedAccountId(): string | null {
    return this.selectedAccountId;
  }

  /**
   * Get a token for a specific account without switching the selected account.
   * Uses the correct appId/tenantId that was used when the account was authenticated.
   */
  async getTokenForAccount(accountId: string): Promise<string | null> {
    const accounts = await this.listAccounts();
    const account = accounts.find((acc: AccountInfo) => acc.homeAccountId === accountId);

    if (!account) {
      logger.error(`Account with ID ${accountId} not found`);
      return null;
    }

    // Get the appId/tenantId that was used to authenticate this account
    const metadata = this.accountMetadata.get(accountId);
    let msalApp = this.msalApp;

    if (metadata) {
      // Create MSAL app with the correct appId/tenantId for this account
      const customConfig: Configuration = {
        auth: {
          clientId: metadata.appId,
          authority: `https://login.microsoftonline.com/${metadata.tenantId}`,
        },
      };
      msalApp = new PublicClientApplication(customConfig);
      // Load the token cache
      const cacheData = this.msalApp.getTokenCache().serialize();
      msalApp.getTokenCache().deserialize(cacheData);
      logger.info(`Using custom app for account ${account.username}: appId=${metadata.appId}, tenant=${metadata.tenantId}`);
    }

    const silentRequest = {
      account: account,
      scopes: this.scopes,
    };

    try {
      const response = await msalApp.acquireTokenSilent(silentRequest);
      return response.accessToken;
    } catch (error) {
      logger.error(`Failed to get token for account ${accountId}: ${(error as Error).message}`);
      return null;
    }
  }

  /**
   * Get account metadata (appId, tenantId) for a specific account
   */
  getAccountMetadata(accountId: string): AccountMetadata | undefined {
    return this.accountMetadata.get(accountId);
  }
}

export default AuthManager;
export { buildScopesFromEndpoints };
