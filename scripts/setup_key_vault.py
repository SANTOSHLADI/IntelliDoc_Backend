"""
scripts/setup_key_vault.py
==========================
Run this ONCE after creating Key Vault in Azure Portal.
It reads your current environment variables and stores them as secrets in Key Vault.

Usage:
    1. Set all environment variables in your terminal first
    2. Set AZURE_KEY_VAULT_URL environment variable
    3. Run: python scripts/setup_key_vault.py

This script is safe to run multiple times — it updates existing secrets.
"""

import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("keyvault-setup")


def main():
    vault_url = os.environ.get("AZURE_KEY_VAULT_URL", "")
    if not vault_url:
        print("\nERROR: AZURE_KEY_VAULT_URL environment variable not set.")
        print("Set it like this:")
        print("  Windows: set AZURE_KEY_VAULT_URL=https://idp-keyvault.vault.azure.net/")
        print("  Mac/Linux: export AZURE_KEY_VAULT_URL=https://idp-keyvault.vault.azure.net/")
        sys.exit(1)

    print(f"\nConnecting to Key Vault: {vault_url}")
    print("Using Azure CLI credentials (run 'az login' first if needed)\n")

    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
        credential = DefaultAzureCredential()
        client = SecretClient(vault_url=vault_url, credential=credential)
        # Test connection
        list(client.list_properties_of_secrets())
        print("Connected to Key Vault successfully\n")
    except Exception as e:
        print(f"\nERROR: Cannot connect to Key Vault: {e}")
        print("Make sure you have run 'az login' and have Key Vault access.")
        sys.exit(1)

    # Map: Key Vault secret name → environment variable name
    secrets_to_upload = {
        # Storage
        "storage-connection-string":    "AZURE_STORAGE_CONNECTION_STRING",
        # Cosmos DB
        "cosmos-db-endpoint":           "COSMOS_DB_ENDPOINT",
        "cosmos-db-key":                "COSMOS_DB_KEY",
        # Azure OpenAI
        "openai-endpoint":              "AZURE_OPENAI_ENDPOINT",
        "openai-api-key":               "AZURE_OPENAI_API_KEY",
        # Azure AI Search
        "search-endpoint":              "AZURE_SEARCH_ENDPOINT",
        "search-api-key":               "AZURE_SEARCH_API_KEY",
        # Document Intelligence
        "doc-intelligence-endpoint":    "DOC_INTELLIGENCE_ENDPOINT",
        "doc-intelligence-key":         "DOC_INTELLIGENCE_KEY",
        # Entra ID
        "entra-client-secret":          "ENTRA_CLIENT_SECRET",
        # APIM
        "apim-backend-secret":          "APIM_BACKEND_SECRET",
    }

    success_count = 0
    skip_count = 0
    fail_count = 0

    for secret_name, env_var in secrets_to_upload.items():
        value = os.environ.get(env_var, "")

        if not value:
            print(f"  SKIP  {secret_name:<40} ({env_var} not set in environment)")
            skip_count += 1
            continue

        try:
            client.set_secret(secret_name, value)
            # Show only first 10 chars of value for security
            preview = value[:10] + "..." if len(value) > 10 else value
            print(f"  OK    {secret_name:<40} = {preview}")
            success_count += 1
        except Exception as e:
            print(f"  FAIL  {secret_name:<40} ERROR: {e}")
            fail_count += 1

    print(f"\n{'='*60}")
    print(f"Results: {success_count} stored, {skip_count} skipped, {fail_count} failed")

    if success_count > 0:
        print("\nNext step: Update Function App environment variables to reference Key Vault.")
        print("In Azure Portal → Function App → Environment variables, change secret values to:")
        print("  @Microsoft.KeyVault(SecretUri=https://idp-keyvault.vault.azure.net/secrets/SECRET-NAME/)")
        print("\nOr run the Function App Key Vault reference update script.")

    if skip_count > 0:
        print(f"\nNote: {skip_count} secrets were skipped because their env vars were not set.")
        print("Set them and run this script again to upload the missing secrets.")


if __name__ == "__main__":
    main()
