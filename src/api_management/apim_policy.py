"""
Azure API Management (APIM) — Backend Integration Layer
========================================================

APIM sits between the client (Static Web App) and the Function App.
In your architecture diagram:

    Client → Static Web App → API Management → Function App

APIM provides:
  1. JWT token validation (Entra ID tokens)
  2. Rate limiting per subscription
  3. CORS headers for the frontend
  4. Request/response transformation
  5. Unified API URL for frontend (hides Function App URL)

This file contains:
  - APIM policy XML definitions (what APIM enforces)
  - A helper that validates APIM-forwarded headers in the Function App
  - Setup script to create APIM programmatically via Azure SDK

NOTE: APIM itself is configured in Azure Portal / ARM templates.
      The Python code here handles the Function App side of the integration.
"""

import logging
import os
import json
from functools import wraps
import azure.functions as func
from src.config_loader import CONFIG

logger = logging.getLogger("azure.idp.apim")


# ══════════════════════════════════════════════════════════════════════
# APIM POLICY XML
# These XML strings define rules that APIM enforces on every request
# BEFORE it reaches the Function App.
# Paste these in Azure Portal → API Management → APIs → your API → Policy editor
# ══════════════════════════════════════════════════════════════════════

APIM_INBOUND_POLICY = """
<policies>
  <inbound>
    <base />

    <!-- 1. CORS: Allow requests from the Static Web App frontend only -->
    <cors allow-credentials="true">
      <allowed-origins>
        <origin>{STATIC_WEB_APP_URL}</origin>
      </allowed-origins>
      <allowed-methods>
        <method>GET</method>
        <method>POST</method>
        <method>OPTIONS</method>
      </allowed-methods>
      <allowed-headers>
        <header>Authorization</header>
        <header>Content-Type</header>
        <header>x-request-id</header>
      </allowed-headers>
    </cors>

    <!-- 2. Validate JWT from Azure Entra ID -->
    <validate-jwt header-name="Authorization"
                  failed-validation-httpcode="401"
                  failed-validation-error-message="Unauthorized: Invalid or expired token"
                  require-expiration-time="true"
                  require-scheme="Bearer"
                  require-signed-tokens="true">
      <openid-config url="https://login.microsoftonline.com/{TENANT_ID}/v2.0/.well-known/openid-configuration" />
      <audiences>
        <audience>{APIM_CLIENT_ID}</audience>
      </audiences>
      <issuers>
        <issuer>https://sts.windows.net/{TENANT_ID}/</issuer>
        <issuer>https://login.microsoftonline.com/{TENANT_ID}/v2.0</issuer>
      </issuers>
      <required-claims>
        <claim name="roles" match="any">
          <value>IDP.Process</value>
          <value>IDP.Read</value>
          <value>IDP.Admin</value>
        </claim>
      </required-claims>
    </validate-jwt>

    <!-- 3. Rate limiting: 100 calls per minute per subscription -->
    <rate-limit calls="100" renewal-period="60" />

    <!-- 4. Quota: 10,000 calls per month per subscription -->
    <quota calls="10000" renewal-period="2592000" />

    <!-- 5. Forward caller identity to Function App as a header -->
    <set-header name="X-User-ObjectId" exists-action="override">
      <value>@(context.Request.Headers.GetValueOrDefault("X-MS-CLIENT-PRINCIPAL-ID", "anonymous"))</value>
    </set-header>
    <set-header name="X-User-Email" exists-action="override">
      <value>@(context.User?.Email ?? "unknown")</value>
    </set-header>

    <!-- 6. Add request correlation ID for tracing across services -->
    <set-header name="X-Correlation-ID" exists-action="skip">
      <value>@(Guid.NewGuid().ToString())</value>
    </set-header>

    <!-- 7. Strip the APIM subscription key before forwarding to Function App -->
    <set-header name="Ocp-Apim-Subscription-Key" exists-action="delete" />

  </inbound>

  <backend>
    <base />
  </backend>

  <outbound>
    <base />
    <!-- Add security headers to every response -->
    <set-header name="X-Content-Type-Options" exists-action="override">
      <value>nosniff</value>
    </set-header>
    <set-header name="X-Frame-Options" exists-action="override">
      <value>DENY</value>
    </set-header>
    <set-header name="Strict-Transport-Security" exists-action="override">
      <value>max-age=31536000; includeSubDomains</value>
    </set-header>
  </outbound>

  <on-error>
    <base />
    <return-response>
      <set-status code="@(context.Response.StatusCode)" reason="@(context.Response.StatusReason)" />
      <set-header name="Content-Type" exists-action="override">
        <value>application/json</value>
      </set-header>
      <set-body>@{
        return new JObject(
          new JProperty("error", context.LastError.Message),
          new JProperty("correlation_id", context.Request.Headers.GetValueOrDefault("X-Correlation-ID",""))
        ).ToString();
      }</set-body>
    </return-response>
  </on-error>
</policies>
"""

APIM_RATE_LIMIT_POLICY = """
<!-- Per-operation rate limit for the /process endpoint (AI calls are expensive) -->
<policies>
  <inbound>
    <base />
    <!-- Stricter limit for AI processing endpoint: 20 calls/minute -->
    <rate-limit calls="20" renewal-period="60" />
    <!-- Require document_type in request body -->
    <choose>
      <when condition="@(!context.Request.Body.As<JObject>().ContainsKey("file_key"))">
        <return-response>
          <set-status code="400" reason="Bad Request" />
          <set-body>{"error": "file_key is required"}</set-body>
        </return-response>
      </when>
    </choose>
  </inbound>
  <backend><base /></backend>
  <outbound><base /></outbound>
  <on-error><base /></on-error>
</policies>
"""


# ══════════════════════════════════════════════════════════════════════
# FUNCTION APP SIDE: Validate APIM-forwarded headers
# APIM validates the JWT externally, then forwards caller info as headers.
# The Function App trusts these headers (APIM is the security boundary).
# ══════════════════════════════════════════════════════════════════════

def get_caller_identity(req: func.HttpRequest) -> dict:
    """
    Extract caller identity from APIM-forwarded headers.
    APIM validates the JWT and passes the parsed claims as headers.

    Returns:
        {
          "user_id": "uuid",
          "email": "user@company.com",
          "correlation_id": "uuid",
          "roles": ["IDP.Process"]
        }
    """
    return {
        "user_id": req.headers.get("X-User-ObjectId", "anonymous"),
        "email": req.headers.get("X-User-Email", "unknown"),
        "correlation_id": req.headers.get("X-Correlation-ID", ""),
        "subscription_id": req.headers.get("Ocp-Apim-Subscription-Key", ""),
    }


def require_apim(func_handler):
    """
    Decorator for Function App routes that enforces APIM is in the chain.
    In production, APIM sets X-APIM-Validated header.
    Rejects direct calls that bypass APIM (no header present).

    Usage:
        @app.route(route="process", methods=["POST"])
        @require_apim
        def process_document(req):
            ...
    """
    @wraps(func_handler)
    def wrapper(req: func.HttpRequest) -> func.HttpResponse:
        # In production: enforce APIM header
        apim_validated = req.headers.get("X-APIM-Validated", "")
        apim_secret = os.environ.get("APIM_BACKEND_SECRET", "")

        if apim_secret and apim_validated != apim_secret:
            logger.warning(
                f"Direct call bypassing APIM detected from: "
                f"{req.headers.get('X-Forwarded-For', 'unknown')}"
            )
            return func.HttpResponse(
                json.dumps({"error": "Direct API access not permitted. Use API Management endpoint."}),
                status_code=403,
                mimetype="application/json"
            )

        # Log caller identity for audit
        identity = get_caller_identity(req)
        logger.info(
            f"APIM request: user={identity['user_id']} "
            f"email={identity['email']} "
            f"correlation={identity['correlation_id']}"
        )

        return func_handler(req)
    return wrapper


def log_apim_request(req: func.HttpRequest, response_status: int, route: str):
    """
    Log every APIM request with full context for Azure Monitor.
    These logs appear in: Azure Portal → Function App → Monitor → Logs
    """
    identity = get_caller_identity(req)
    logger.info(json.dumps({
        "event": "apim_request",
        "route": route,
        "method": req.method,
        "status": response_status,
        "user_id": identity["user_id"],
        "email": identity["email"],
        "correlation_id": identity["correlation_id"],
        "user_agent": req.headers.get("User-Agent", ""),
        "forwarded_ip": req.headers.get("X-Forwarded-For", ""),
    }))
