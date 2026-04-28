"""
Azure Key Vault — Secure Secret Management
==========================================

WHY KEY VAULT:
  Currently all secrets (Cosmos DB key, OpenAI key, Search key, Storage connection string)
  are stored as plain text in Function App Environment Variables.
  
  Anyone with access to Azure Portal → Function App → Environment variables can SEE them.
  
  Key Vault stores secrets ENCRYPTED. Function App fetches them at runtime using
  Managed Identity — NO password needed to access Key Vault. 
  No human can read the raw secret values unless explicitly given access.

HOW IT WORKS IN YOUR ARCHITECTURE:
  Before:
    Function App → reads COSMOS_DB_KEY from Environment Variables (plain text visible)
  
  After:
    Function App → Managed Identity → Key Vault → fetches COSMOS_DB_KEY (encrypted, audited)

  In local.settings.json, environment variables reference Key Vault like this:
    "COSMOS_DB_KEY": "@Microsoft.KeyVault(SecretUri=https://idp-keyvault.vault.azure.net/secrets/cosmos-db-key/)"

  Azure automatically resolves the @Microsoft.KeyVault() reference at runtime.
  The actual secret value NEVER appears in plain text anywhere.
"""

import os
import logging
from functools import lru_cache
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.keyvault.secrets import SecretClient
from azure.core.exceptions import ResourceNotFoundError, HttpResponseError

logger = logging.getLogger("azure.idp.keyvault")

# Key Vault URL from environment variable
# Set this in Azure Portal → Function App → Environment variables
# Value: https://idp-keyvault.vault.azure.net/
VAULT_URL = os.environ.get("AZURE_KEY_VAULT_URL", "")


# ══════════════════════════════════════════════════════════════════════
# KEY VAULT CLIENT
# Uses Managed Identity — no username/password needed
# ══════════════════════════════════════════════════════════════════════

@lru_cache(maxsize=1)
def _get_vault_client() -> SecretClient | None:
    """
    Create and cache a Key Vault client using Managed Identity.
    
    Managed Identity = Azure automatically gives the Function App
    an identity token. No credentials stored anywhere in code.
    
    Returns None if Key Vault URL is not configured
    (falls back to environment variables).
    """
    if not VAULT_URL:
        logger.warning(
            "AZURE_KEY_VAULT_URL not set — "
            "falling back to plain environment variables"
        )
        return None

    try:
        # DefaultAzureCredential tries multiple auth methods in order:
        # 1. Managed Identity (production — works on Azure)
        # 2. Azure CLI (local development — works on your laptop)
        # 3. VS Code credentials (local development)
        credential = DefaultAzureCredential()
        client = SecretClient(vault_url=VAULT_URL, credential=credential)
        logger.info(f"Key Vault client connected: {VAULT_URL}")
        return client
    except Exception as e:
        logger.error(f"Failed to connect to Key Vault: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════
# GET SECRET
# ══════════════════════════════════════════════════════════════════════

def get_secret(secret_name: str, fallback_env_var: str = None) -> str:
    """
    Fetch a secret from Azure Key Vault.
    Falls back to environment variable if Key Vault is not available.

    Args:
        secret_name:     Name of the secret in Key Vault (e.g. "cosmos-db-key")
        fallback_env_var: Environment variable name to use if Key Vault fails
                          (e.g. "COSMOS_DB_KEY")

    Returns:
        Secret value as string, or empty string if not found

    Key Vault secret naming rules:
        - Only letters, numbers, and hyphens
        - No underscores (use hyphens instead)
        - cosmos-db-key  NOT  COSMOS_DB_KEY
    """
    client = _get_vault_client()

    if client:
        try:
            secret = client.get_secret(secret_name)
            logger.info(f"Secret '{secret_name}' fetched from Key Vault")
            return secret.value
        except ResourceNotFoundError:
            logger.warning(
                f"Secret '{secret_name}' not found in Key Vault. "
                f"Falling back to env var: {fallback_env_var}"
            )
        except HttpResponseError as e:
            logger.error(
                f"Key Vault HTTP error for '{secret_name}': {e}. "
                f"Falling back to env var: {fallback_env_var}"
            )
        except Exception as e:
            logger.error(
                f"Key Vault error for '{secret_name}': {e}. "
                f"Falling back to env var: {fallback_env_var}"
            )

    # Fallback to environment variable
    if fallback_env_var:
        value = os.environ.get(fallback_env_var, "")
        if value:
            logger.info(f"Using env var '{fallback_env_var}' as fallback")
        return value

    return ""


def set_secret(secret_name: str, secret_value: str) -> bool:
    """
    Store a new secret in Azure Key Vault.
    Used by setup scripts to populate Key Vault initially.

    Args:
        secret_name:  Name for the secret (use hyphens not underscores)
        secret_value: The actual secret value to store

    Returns:
        True if stored successfully, False otherwise
    """
    client = _get_vault_client()
    if not client:
        logger.error("Cannot set secret — Key Vault client not available")
        return False

    try:
        client.set_secret(secret_name, secret_value)
        logger.info(f"Secret '{secret_name}' stored in Key Vault")
        return True
    except Exception as e:
        logger.error(f"Failed to store secret '{secret_name}': {e}")
        return False


def list_secrets() -> list:
    """List all secret names in Key Vault (not values — just names)."""
    client = _get_vault_client()
    if not client:
        return []
    try:
        return [s.name for s in client.list_properties_of_secrets()]
    except Exception as e:
        logger.error(f"Failed to list secrets: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════
# ALL SECRET NAMES IN KEY VAULT
# These are the exact names to use when storing secrets in Key Vault
# ══════════════════════════════════════════════════════════════════════

class SecretNames:
    """
    Central registry of all secret names stored in Key Vault.
    Use hyphens not underscores (Key Vault requirement).
    """
    # Storage
    STORAGE_CONNECTION_STRING    = "storage-connection-string"

    # Cosmos DB
    COSMOS_DB_KEY                = "cosmos-db-key"
    COSMOS_DB_ENDPOINT           = "cosmos-db-endpoint"

    # Azure OpenAI
    OPENAI_API_KEY               = "openai-api-key"
    OPENAI_ENDPOINT              = "openai-endpoint"

    # Azure AI Search
    SEARCH_API_KEY               = "search-api-key"
    SEARCH_ENDPOINT              = "search-endpoint"

    # Document Intelligence
    DOC_INTELLIGENCE_KEY         = "doc-intelligence-key"
    DOC_INTELLIGENCE_ENDPOINT    = "doc-intelligence-endpoint"

    # Entra ID
    ENTRA_CLIENT_SECRET          = "entra-client-secret"

    # APIM
    APIM_BACKEND_SECRET          = "apim-backend-secret"
