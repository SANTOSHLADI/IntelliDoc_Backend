"""
Azure Policy & VNet — Infrastructure Security Definitions
==========================================================

In your architecture diagram:
  - VNet (Virtual Network) wraps all services inside a private network boundary
  - Azure Policy enforces compliance rules across all resources
  - Entra ID governs identity

These are INFRASTRUCTURE-level controls — they live in Azure Portal / ARM templates,
NOT in Python code. This file documents what policies and VNet rules to configure.

How to apply:
  Option A: Azure Portal (click-through) — steps below
  Option B: Run this script (az CLI commands) — run_policy_setup() function
"""

import subprocess
import json
import os
import logging

logger = logging.getLogger("azure.idp.infra")

# ── Configuration — fill these in before running ──────────────────────
RESOURCE_GROUP   = os.environ.get("RESOURCE_GROUP", "idp-rg")
SUBSCRIPTION_ID  = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
LOCATION         = os.environ.get("AZURE_LOCATION", "eastus")
VNET_NAME        = "idp-vnet"
FUNCTION_SUBNET  = "idp-func-subnet"
STORAGE_SUBNET   = "idp-storage-subnet"
COSMOS_SUBNET    = "idp-cosmos-subnet"


# ══════════════════════════════════════════════════════════════════════
# VNET SETUP
# Creates a private network so all services talk to each other
# over private IP addresses — not over the public internet
# ══════════════════════════════════════════════════════════════════════

VNET_COMMANDS = [
    # 1. Create the Virtual Network with address space 10.0.0.0/16
    f"az network vnet create "
    f"--name {VNET_NAME} "
    f"--resource-group {RESOURCE_GROUP} "
    f"--location {LOCATION} "
    f"--address-prefix 10.0.0.0/16",

    # 2. Subnet for Function App (10.0.1.0/24)
    f"az network vnet subnet create "
    f"--name {FUNCTION_SUBNET} "
    f"--resource-group {RESOURCE_GROUP} "
    f"--vnet-name {VNET_NAME} "
    f"--address-prefix 10.0.1.0/24 "
    f"--delegations Microsoft.Web/serverFarms",

    # 3. Subnet for Storage / Blob (10.0.2.0/24)
    f"az network vnet subnet create "
    f"--name {STORAGE_SUBNET} "
    f"--resource-group {RESOURCE_GROUP} "
    f"--vnet-name {VNET_NAME} "
    f"--address-prefix 10.0.2.0/24 "
    f"--service-endpoints Microsoft.Storage",

    # 4. Subnet for Cosmos DB (10.0.3.0/24)
    f"az network vnet subnet create "
    f"--name {COSMOS_SUBNET} "
    f"--resource-group {RESOURCE_GROUP} "
    f"--vnet-name {VNET_NAME} "
    f"--address-prefix 10.0.3.0/24 "
    f"--service-endpoints Microsoft.AzureCosmosDB",

    # 5. Integrate Function App with VNet
    f"az functionapp vnet-integration add "
    f"--name idp-func-2024 "
    f"--resource-group {RESOURCE_GROUP} "
    f"--vnet {VNET_NAME} "
    f"--subnet {FUNCTION_SUBNET}",

    # 6. Restrict Blob Storage to only allow traffic from VNet
    f"az storage account network-rule add "
    f"--account-name idpstorage2024 "
    f"--resource-group {RESOURCE_GROUP} "
    f"--vnet-name {VNET_NAME} "
    f"--subnet {STORAGE_SUBNET}",

    # 7. Set Blob Storage default action to Deny (block all except VNet)
    f"az storage account update "
    f"--name idpstorage2024 "
    f"--resource-group {RESOURCE_GROUP} "
    f"--default-action Deny",

    # 8. Restrict Cosmos DB to VNet
    f"az cosmosdb network-rule add "
    f"--name idp-cosmos-2024 "
    f"--resource-group {RESOURCE_GROUP} "
    f"--virtual-network {VNET_NAME} "
    f"--subnet {COSMOS_SUBNET}",
]


# ══════════════════════════════════════════════════════════════════════
# AZURE POLICY DEFINITIONS
# Policies enforce rules — e.g. "all resources must have tags"
# or "storage must use HTTPS" or "no public IP on VMs"
# ══════════════════════════════════════════════════════════════════════

# Policy 1: Enforce HTTPS-only on all Storage Accounts
POLICY_STORAGE_HTTPS = {
    "properties": {
        "displayName": "IDP - Storage accounts should use HTTPS only",
        "description": "Ensures all Blob Storage traffic uses HTTPS (not HTTP)",
        "mode": "All",
        "policyRule": {
            "if": {
                "allOf": [
                    {"field": "type", "equals": "Microsoft.Storage/storageAccounts"},
                    {"field": "Microsoft.Storage/storageAccounts/supportsHttpsTrafficOnly", "equals": "false"}
                ]
            },
            "then": {"effect": "deny"}
        }
    }
}

# Policy 2: Require tags on all resources (for cost tracking)
POLICY_REQUIRE_TAGS = {
    "properties": {
        "displayName": "IDP - Require tags on all resources",
        "description": "All resources must have Project, Environment, and Owner tags",
        "mode": "All",
        "parameters": {
            "tagName": {"type": "String", "metadata": {"description": "Required tag name"}},
        },
        "policyRule": {
            "if": {
                "field": "[concat('tags[', parameters('tagName'), ']')]",
                "exists": "false"
            },
            "then": {"effect": "deny"}
        }
    }
}

# Policy 3: Deny public network access on Cosmos DB
POLICY_COSMOS_NO_PUBLIC = {
    "properties": {
        "displayName": "IDP - Cosmos DB must disable public network access",
        "description": "Cosmos DB must only be accessible via VNet / private endpoint",
        "mode": "All",
        "policyRule": {
            "if": {
                "allOf": [
                    {"field": "type", "equals": "Microsoft.DocumentDB/databaseAccounts"},
                    {"field": "Microsoft.DocumentDB/databaseAccounts/publicNetworkAccess", "equals": "Enabled"}
                ]
            },
            "then": {"effect": "audit"}  # 'audit' logs violations, 'deny' blocks them
        }
    }
}

# Policy 4: Function App must use Python 3.11+
POLICY_FUNCTION_PYTHON = {
    "properties": {
        "displayName": "IDP - Function App must use Python 3.11",
        "description": "Enforces Python version for security and support compliance",
        "mode": "All",
        "policyRule": {
            "if": {
                "allOf": [
                    {"field": "type", "equals": "Microsoft.Web/sites"},
                    {"field": "kind", "contains": "functionapp"},
                    {
                        "not": {
                            "field": "Microsoft.Web/sites/siteConfig.pythonVersion",
                            "equals": "3.11"
                        }
                    }
                ]
            },
            "then": {"effect": "audit"}
        }
    }
}

AZURE_POLICY_COMMANDS = [
    # Create and assign Policy 1 — HTTPS on Storage
    f"az policy definition create "
    f"--name 'idp-storage-https' "
    f"--rules '{json.dumps(POLICY_STORAGE_HTTPS['properties']['policyRule'])}' "
    f"--display-name 'IDP Storage HTTPS Only' "
    f"--mode All",

    f"az policy assignment create "
    f"--name 'idp-storage-https-assignment' "
    f"--policy 'idp-storage-https' "
    f"--resource-group {RESOURCE_GROUP}",

    # Create and assign Policy 2 — Cosmos DB no public access
    f"az policy definition create "
    f"--name 'idp-cosmos-no-public' "
    f"--rules '{json.dumps(POLICY_COSMOS_NO_PUBLIC['properties']['policyRule'])}' "
    f"--display-name 'IDP Cosmos DB No Public Access' "
    f"--mode All",

    f"az policy assignment create "
    f"--name 'idp-cosmos-no-public-assignment' "
    f"--policy 'idp-cosmos-no-public' "
    f"--resource-group {RESOURCE_GROUP}",
]


# ══════════════════════════════════════════════════════════════════════
# RUNNER FUNCTIONS
# ══════════════════════════════════════════════════════════════════════

def run_vnet_setup(dry_run: bool = True):
    """
    Run all VNet setup commands.
    Set dry_run=False to actually execute them.

    Usage:
        python infra/azure_policy_vnet.py --vnet
    """
    print("\n=== VNet Setup Commands ===\n")
    for cmd in VNET_COMMANDS:
        print(f"$ {cmd}\n")
        if not dry_run:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode == 0:
                print(f"  ✅ Success")
            else:
                print(f"  ❌ Error: {result.stderr}")


def run_policy_setup(dry_run: bool = True):
    """
    Run all Azure Policy creation commands.
    Set dry_run=False to actually execute them.

    Usage:
        python infra/azure_policy_vnet.py --policy
    """
    print("\n=== Azure Policy Commands ===\n")
    for cmd in AZURE_POLICY_COMMANDS:
        print(f"$ {cmd}\n")
        if not dry_run:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode == 0:
                print(f"  ✅ Success")
            else:
                print(f"  ❌ Error: {result.stderr}")


def print_portal_steps():
    """Print the Azure Portal click steps for VNet and Policy setup."""
    steps = """
╔══════════════════════════════════════════════════════════════════╗
║  AZURE PORTAL STEPS — VNet Setup                                 ║
╚══════════════════════════════════════════════════════════════════╝

STEP 1: Create VNet
  Portal → Search "Virtual Networks" → + Create
  Name: idp-vnet | Region: East US | Address space: 10.0.0.0/16

STEP 2: Add Subnets
  VNet → Subnets → + Subnet
    - idp-func-subnet    10.0.1.0/24  (for Function App)
    - idp-storage-subnet 10.0.2.0/24  (for Blob Storage)
    - idp-cosmos-subnet  10.0.3.0/24  (for Cosmos DB)

STEP 3: Integrate Function App with VNet
  Function App → Networking → VNet Integration → + Add VNet
  Select: idp-vnet / idp-func-subnet

STEP 4: Restrict Blob Storage to VNet
  Storage Account → Networking → Firewalls and virtual networks
  → Selected networks → + Add existing virtual network
  → Select idp-vnet / idp-storage-subnet → Save

STEP 5: Restrict Cosmos DB to VNet
  Cosmos DB → Networking → Virtual networks → + Add
  → Select idp-vnet / idp-cosmos-subnet → Save

╔══════════════════════════════════════════════════════════════════╗
║  AZURE PORTAL STEPS — Policy Setup                               ║
╚══════════════════════════════════════════════════════════════════╝

STEP 6: Assign Built-in Policies
  Search "Policy" in Portal → Assignments → + Assign policy
  
  Assign these built-in policies to your Resource Group (idp-rg):
  
  a) "Storage accounts should use customer-managed key for encryption"
  b) "Secure transfer to storage accounts should be enabled"
  c) "Azure Cosmos DB accounts should have firewall rules"
  d) "Function apps should use latest Python version"
  e) "API Management services should use a virtual network"

STEP 7: Set Required Tags Policy
  Policy → Definitions → + Policy definition
  Paste the POLICY_REQUIRE_TAGS JSON from this file
  Assign to Resource Group idp-rg with parameters:
    tagName = "Project"    (value: IDP)
    tagName = "Owner"      (value: your team)
    tagName = "CostCenter" (value: your cost center)

╔══════════════════════════════════════════════════════════════════╗
║  ENTRA ID SETUP (for Authentication)                             ║
╚══════════════════════════════════════════════════════════════════╝

STEP 8: Create App Registration for the API
  Entra ID → App registrations → + New registration
  Name: IDP-API | Supported account types: Single tenant
  → Register

STEP 9: Add App Roles
  App Registration → App roles → + Create app role
  Add these roles:
    - IDP.Process  (for users who can upload and process)
    - IDP.Read     (for users who can only view results)
    - IDP.Admin    (full access)
    - IDP.OCROnly  (OCR endpoint only)

STEP 10: Create App Registration for the Frontend (Static Web App)
  Entra ID → App registrations → + New registration
  Name: IDP-Frontend | Add redirect URI: your Static Web App URL
  → Register

STEP 11: Grant Frontend access to API
  Frontend App Registration → API permissions → + Add permission
  → My APIs → IDP-API → Select roles needed → Grant admin consent
"""
    print(steps)


if __name__ == "__main__":
    import sys
    if "--vnet" in sys.argv:
        dry_run = "--execute" not in sys.argv
        run_vnet_setup(dry_run=dry_run)
    elif "--policy" in sys.argv:
        dry_run = "--execute" not in sys.argv
        run_policy_setup(dry_run=dry_run)
    else:
        print_portal_steps()
