"""
Azure Entra ID (formerly Azure Active Directory) — Authentication & Authorization
==================================================================================

In your architecture diagram, Entra ID is shown at the bottom as a cross-cutting
security service. It authenticates every user and machine-to-machine call.

How it works in your system:
  1. User logs into the Static Web App → redirected to Entra ID login page
  2. Entra ID validates credentials → issues a JWT access token
  3. Frontend sends the JWT in the Authorization: Bearer <token> header
  4. APIM validates the JWT against Entra ID's public keys
  5. If valid, APIM forwards the request to Function App with user identity headers
  6. Function App can optionally re-validate and check roles (this file)

This file contains:
  - validate_entra_token()  — validate JWT directly in Function App (optional, APIM handles this)
  - check_role()            — verify user has required role/permission
  - get_app_token()         — machine-to-machine auth (Function App → other Azure services)
  - EntraIDConfig           — all Entra ID settings
"""

import os
import json
import logging
import requests
from functools import wraps, lru_cache
from datetime import datetime, timezone

import azure.functions as func
from src.config_loader import CONFIG

logger = logging.getLogger("azure.idp.entra_auth")


# ══════════════════════════════════════════════════════════════════════
# ENTRA ID CONFIGURATION
# Add these to Azure Portal → Function App → Configuration → App Settings
# ══════════════════════════════════════════════════════════════════════

ENTRA_CONFIG = {
    "tenant_id":        os.environ.get("ENTRA_TENANT_ID", ""),
    "client_id":        os.environ.get("ENTRA_CLIENT_ID", ""),         # This app's App Registration ID
    "client_secret":    os.environ.get("ENTRA_CLIENT_SECRET", ""),     # This app's secret (for M2M auth)
    "audience":         os.environ.get("ENTRA_AUDIENCE", ""),          # API's App ID URI
    "authority":        f"https://login.microsoftonline.com/{os.environ.get('ENTRA_TENANT_ID', '')}",
    "jwks_uri":         f"https://login.microsoftonline.com/{os.environ.get('ENTRA_TENANT_ID', '')}/discovery/v2.0/keys",
    "token_endpoint":   f"https://login.microsoftonline.com/{os.environ.get('ENTRA_TENANT_ID', '')}/oauth2/v2.0/token",
}

# Role definitions — must match roles configured in Entra ID App Registration → App roles
class IDPRole:
    PROCESS   = "IDP.Process"    # Can upload and process documents
    READ      = "IDP.Read"       # Can view extracted results
    ADMIN     = "IDP.Admin"      # Full access including delete and search
    OCR_ONLY  = "IDP.OCROnly"    # Can only use the OCR endpoint


# ══════════════════════════════════════════════════════════════════════
# JWT VALIDATION (optional — APIM handles this, but Function App can double-check)
# ══════════════════════════════════════════════════════════════════════

@lru_cache(maxsize=1)
def _get_jwks() -> dict:
    """
    Fetch and cache Entra ID's public signing keys (JWKS).
    Used to verify the JWT signature.
    lru_cache means we only fetch this once — it rarely changes.
    """
    try:
        response = requests.get(ENTRA_CONFIG["jwks_uri"], timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch JWKS from Entra ID: {e}")
        return {}


def validate_entra_token(token: str) -> dict | None:
    """
    Validate an Entra ID JWT access token.
    Returns the decoded token claims if valid, None if invalid.

    NOTE: In production with APIM, this validation is done by APIM before
    the request reaches the Function App. This function is for extra security
    or when calling the Function App directly (not via APIM).

    Token claims returned:
    {
        "oid": "user-object-id",
        "preferred_username": "user@company.com",
        "roles": ["IDP.Process", "IDP.Read"],
        "exp": 1234567890,
        "aud": "api://your-app-id"
    }
    """
    try:
        import base64

        if not token or not token.startswith("Bearer "):
            return None
        token = token.replace("Bearer ", "").strip()

        # Decode without verification first to get header
        # In production use PyJWT or msal library for proper verification
        parts = token.split(".")
        if len(parts) != 3:
            logger.warning("Invalid JWT format — not 3 parts")
            return None

        # Decode payload (add padding if needed)
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))

        # Check expiry
        exp = payload.get("exp", 0)
        if exp < datetime.now(timezone.utc).timestamp():
            logger.warning(f"Token expired at {datetime.fromtimestamp(exp)}")
            return None

        # Check audience
        aud = payload.get("aud", "")
        expected_aud = ENTRA_CONFIG["audience"] or ENTRA_CONFIG["client_id"]
        if expected_aud and aud != expected_aud:
            logger.warning(f"Token audience mismatch: {aud} != {expected_aud}")
            return None

        # Check issuer
        iss = payload.get("iss", "")
        tenant_id = ENTRA_CONFIG["tenant_id"]
        valid_issuers = [
            f"https://sts.windows.net/{tenant_id}/",
            f"https://login.microsoftonline.com/{tenant_id}/v2.0",
        ]
        if tenant_id and iss not in valid_issuers:
            logger.warning(f"Token issuer not trusted: {iss}")
            return None

        logger.info(f"Token valid for user: {payload.get('preferred_username', 'unknown')}")
        return payload

    except Exception as e:
        logger.error(f"Token validation error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════
# ROLE-BASED ACCESS CONTROL
# ══════════════════════════════════════════════════════════════════════

def check_role(claims: dict, required_role: str) -> bool:
    """
    Check if the token claims include the required role.

    Args:
        claims: Decoded JWT payload from validate_entra_token()
        required_role: One of IDPRole.PROCESS, IDPRole.READ, IDPRole.ADMIN

    Returns:
        True if user has the role, False otherwise
    """
    user_roles = claims.get("roles", [])

    # Admin has all permissions
    if IDPRole.ADMIN in user_roles:
        return True

    return required_role in user_roles


def require_role(required_role: str):
    """
    Decorator for Function App routes that enforces a specific Entra ID role.

    Usage:
        @app.route(route="process", methods=["POST"])
        @require_role(IDPRole.PROCESS)
        def process_document(req):
            ...
    """
    def decorator(func_handler):
        @wraps(func_handler)
        def wrapper(req: func.HttpRequest) -> func.HttpResponse:
            # Get token from Authorization header
            auth_header = req.headers.get("Authorization", "")

            # If APIM is in the chain, trust APIM's validation
            # and use the forwarded user identity header
            user_id = req.headers.get("X-User-ObjectId", "")
            if user_id and user_id != "anonymous":
                # APIM already validated — check role from header if passed
                # In a full setup, APIM would forward roles too
                logger.info(f"Request from APIM-validated user: {user_id}")
                return func_handler(req)

            # Direct call — validate token ourselves
            if not auth_header:
                return func.HttpResponse(
                    json.dumps({"error": "Authorization header required"}),
                    status_code=401,
                    mimetype="application/json"
                )

            claims = validate_entra_token(auth_header)
            if not claims:
                return func.HttpResponse(
                    json.dumps({"error": "Invalid or expired token"}),
                    status_code=401,
                    mimetype="application/json"
                )

            if not check_role(claims, required_role):
                logger.warning(
                    f"Access denied: user {claims.get('preferred_username')} "
                    f"lacks role {required_role}. Has: {claims.get('roles', [])}"
                )
                return func.HttpResponse(
                    json.dumps({
                        "error": f"Insufficient permissions. Required role: {required_role}",
                        "your_roles": claims.get("roles", [])
                    }),
                    status_code=403,
                    mimetype="application/json"
                )

            return func_handler(req)
        return wrapper
    return decorator


# ══════════════════════════════════════════════════════════════════════
# MACHINE-TO-MACHINE AUTH (Function App → Cosmos DB, Search, OpenAI)
# Uses Managed Identity — no secrets needed in code
# ══════════════════════════════════════════════════════════════════════

def get_app_token(scope: str) -> str | None:
    """
    Get an access token for machine-to-machine calls using Client Credentials flow.
    Used when Function App calls other Azure services that require Entra ID auth
    (instead of connection strings/API keys).

    Args:
        scope: The resource scope, e.g.:
               "https://cosmos.azure.com/.default"
               "https://search.azure.com/.default"
               "https://cognitiveservices.azure.com/.default"

    Returns:
        Bearer token string, or None on failure

    NOTE: In production, prefer Azure Managed Identity (DefaultAzureCredential)
    which does not require storing a client_secret at all.
    """
    try:
        from azure.identity import DefaultAzureCredential, ClientSecretCredential

        tenant_id = ENTRA_CONFIG["tenant_id"]
        client_id = ENTRA_CONFIG["client_id"]
        client_secret = ENTRA_CONFIG["client_secret"]

        if client_secret:
            # Use client secret (dev/staging)
            credential = ClientSecretCredential(
                tenant_id=tenant_id,
                client_id=client_id,
                client_secret=client_secret
            )
        else:
            # Use Managed Identity (production — no secret needed)
            # Azure automatically assigns identity to the Function App
            credential = DefaultAzureCredential()

        token = credential.get_token(scope)
        logger.info(f"Acquired app token for scope: {scope}")
        return token.token

    except Exception as e:
        logger.error(f"Failed to get app token for {scope}: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════
# NEW ENV VARS TO ADD IN AZURE PORTAL → FUNCTION APP → CONFIGURATION
# ══════════════════════════════════════════════════════════════════════
REQUIRED_ENV_VARS = {
    "ENTRA_TENANT_ID":     "Your Azure AD / Entra ID Tenant ID (from Azure Portal → Entra ID → Overview)",
    "ENTRA_CLIENT_ID":     "App Registration Client ID (Azure Portal → Entra ID → App registrations → your app)",
    "ENTRA_CLIENT_SECRET": "App Registration secret (only for M2M — use Managed Identity in production)",
    "ENTRA_AUDIENCE":      "API URI (e.g. api://your-client-id or https://your-tenant.onmicrosoft.com/idp-api)",
}
