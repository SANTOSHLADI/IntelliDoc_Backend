"""
Azure Monitor — Structured Logging, Health Check & Metrics
===========================================================

In your architecture diagram, Azure Monitor watches the whole system.
It collects logs, metrics, and alerts from all services.

This file adds:
  1. Structured JSON logging → every log goes to Azure Monitor / Log Analytics
  2. Health check endpoint  → GET /api/health (APIM and load balancers ping this)
  3. Custom metrics helpers → track document processing counts, latency, errors
  4. Alert rule definitions → what conditions should page the on-call team

Azure Monitor automatically captures:
  - All logger.info/error/warning calls from your code
  - Function App execution counts, duration, failures
  - Cosmos DB request units consumed
  - Blob Storage transactions
  - AI Search query counts

This file adds structured logging so logs are easily queryable in Log Analytics.
"""

import json
import time
import logging
import os
from datetime import datetime, timezone
from contextlib import contextmanager

import azure.functions as func
from src.config_loader import CONFIG

logger = logging.getLogger("azure.idp.monitor")


# ══════════════════════════════════════════════════════════════════════
# STRUCTURED LOGGER
# Instead of: logger.info("Processing done")
# Use:        log_event("document_processed", {"doc_type": "invoice", "pages": 5})
# Produces JSON log entries that are easily filtered in Log Analytics
# ══════════════════════════════════════════════════════════════════════

def log_event(event_name: str, properties: dict = None, level: str = "INFO"):
    """
    Emit a structured JSON log event to Azure Monitor / Application Insights.

    In Log Analytics, query with:
        traces
        | where message contains "document_processed"
        | extend props = parse_json(message)
        | project props.doc_type, props.duration_ms, props.pages

    Args:
        event_name: Short identifier, e.g. "document_processed", "extraction_failed"
        properties: Any additional data to include in the log
        level: "INFO", "WARNING", "ERROR"
    """
    payload = {
        "event": event_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service": "azure-idp",
        "environment": os.environ.get("AZURE_FUNCTIONS_ENVIRONMENT", "Production"),
    }
    if properties:
        payload.update(properties)

    log_str = json.dumps(payload)

    if level == "ERROR":
        logger.error(log_str)
    elif level == "WARNING":
        logger.warning(log_str)
    else:
        logger.info(log_str)


def log_document_processed(
    file_name: str,
    document_type: str,
    input_type: str,
    page_count: int,
    duration_ms: float,
    success: bool,
    user_id: str = "unknown",
):
    """
    Standard log entry for every document processing request.
    Creates a consistent log format for reporting and dashboards.
    """
    log_event(
        "document_processed",
        {
            "file_name": file_name,
            "document_type": document_type,
            "input_type": input_type,
            "page_count": page_count,
            "duration_ms": round(duration_ms, 2),
            "success": success,
            "user_id": user_id,
        },
        level="INFO" if success else "ERROR"
    )


def log_ai_call(
    model: str,
    document_type: str,
    tokens_estimate: int,
    duration_ms: float,
    success: bool,
):
    """
    Log every Azure OpenAI API call for cost tracking and performance monitoring.
    In Azure Portal → Application Insights → Usage, you can see AI call costs.
    """
    log_event(
        "ai_model_call",
        {
            "model": model,
            "document_type": document_type,
            "estimated_tokens": tokens_estimate,
            "duration_ms": round(duration_ms, 2),
            "success": success,
            "estimated_cost_usd": round(tokens_estimate * 0.00001, 6),  # approx gpt-4o pricing
        }
    )


def log_error(error: Exception, context: str, properties: dict = None):
    """
    Log an error with full context. These appear in:
    Azure Portal → Function App → Monitor → Logs → exceptions table
    """
    props = {
        "context": context,
        "error_type": type(error).__name__,
        "error_message": str(error),
    }
    if properties:
        props.update(properties)
    log_event("error", props, level="ERROR")


# ══════════════════════════════════════════════════════════════════════
# TIMING CONTEXT MANAGER
# Measures how long a block of code takes and logs it
# ══════════════════════════════════════════════════════════════════════

@contextmanager
def measure_time(operation_name: str, extra_props: dict = None):
    """
    Context manager that measures execution time of a code block.

    Usage:
        with measure_time("blob_download", {"file_key": "uploads/invoice.pdf"}):
            local_path = download_file_from_blob(file_key)

    Logs entry like:
        {"event": "timing", "operation": "blob_download", "duration_ms": 342.5}
    """
    start = time.perf_counter()
    try:
        yield
        duration_ms = (time.perf_counter() - start) * 1000
        props = {"operation": operation_name, "duration_ms": round(duration_ms, 2), "success": True}
        if extra_props:
            props.update(extra_props)
        log_event("timing", props)
    except Exception as e:
        duration_ms = (time.perf_counter() - start) * 1000
        props = {
            "operation": operation_name,
            "duration_ms": round(duration_ms, 2),
            "success": False,
            "error": str(e),
        }
        if extra_props:
            props.update(extra_props)
        log_event("timing", props, level="ERROR")
        raise


# ══════════════════════════════════════════════════════════════════════
# HEALTH CHECK ENDPOINT
# Add this route to function_app.py
# APIM and Azure Front Door ping /api/health to verify the system is up
# ══════════════════════════════════════════════════════════════════════

def health_check_handler(req: func.HttpRequest) -> func.HttpResponse:
    """
    Health check endpoint: GET /api/health

    Returns 200 if all dependencies are reachable, 503 if any are down.
    Used by:
      - Azure API Management (to route traffic away from unhealthy instances)
      - Azure Monitor (to trigger alerts when health check fails)
      - Your operations team (to verify deployment succeeded)

    Register in function_app.py:
        from src.monitoring.azure_monitor import health_check_handler

        @app.route(route="health", methods=["GET"])
        def health(req: func.HttpRequest) -> func.HttpResponse:
            return health_check_handler(req)
    """
    checks = {}
    all_healthy = True

    # 1. Check Cosmos DB connectivity
    try:
        from azure.cosmos import CosmosClient
        client = CosmosClient(
            url=CONFIG["cosmos"]["endpoint"],
            credential=CONFIG["cosmos"]["key"],
        )
        db = client.get_database_client(CONFIG["cosmos"]["database_name"])
        db.read()
        checks["cosmos_db"] = {"status": "healthy"}
    except Exception as e:
        checks["cosmos_db"] = {"status": "unhealthy", "error": str(e)}
        all_healthy = False
        logger.error(f"Health check: Cosmos DB unreachable: {e}")

    # 2. Check Azure Blob Storage
    try:
        from azure.storage.blob import BlobServiceClient
        blob_client = BlobServiceClient.from_connection_string(
            CONFIG["blob"]["connection_string"]
        )
        blob_client.get_service_properties()
        checks["blob_storage"] = {"status": "healthy"}
    except Exception as e:
        checks["blob_storage"] = {"status": "unhealthy", "error": str(e)}
        all_healthy = False
        logger.error(f"Health check: Blob Storage unreachable: {e}")

    # 3. Check Azure AI Search
    try:
        import requests as req_lib
        search_url = f"{CONFIG['ai_search']['endpoint']}/indexes?api-version={CONFIG['ai_search']['api_version']}"
        resp = req_lib.get(
            search_url,
            headers={"api-key": CONFIG["ai_search"]["api_key"]},
            timeout=5,
        )
        if resp.status_code == 200:
            checks["ai_search"] = {"status": "healthy"}
        else:
            checks["ai_search"] = {"status": "unhealthy", "http_status": resp.status_code}
            all_healthy = False
    except Exception as e:
        checks["ai_search"] = {"status": "unhealthy", "error": str(e)}
        all_healthy = False

    # 4. Check Azure OpenAI (lightweight — just check endpoint is reachable)
    try:
        import requests as req_lib
        oai_url = f"{CONFIG['azure_openai']['endpoint']}openai/models?api-version={CONFIG['azure_openai']['api_version']}"
        resp = req_lib.get(
            oai_url,
            headers={"api-key": CONFIG["azure_openai"]["api_key"]},
            timeout=5,
        )
        if resp.status_code in (200, 401):  # 401 still means endpoint is up
            checks["azure_openai"] = {"status": "healthy"}
        else:
            checks["azure_openai"] = {"status": "unhealthy", "http_status": resp.status_code}
            all_healthy = False
    except Exception as e:
        checks["azure_openai"] = {"status": "unhealthy", "error": str(e)}
        all_healthy = False

    status_code = 200 if all_healthy else 503
    response_body = {
        "status": "healthy" if all_healthy else "degraded",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": os.environ.get("WEBSITE_DEPLOYMENT_ID", "unknown"),
        "checks": checks,
    }

    log_event(
        "health_check",
        {"overall": response_body["status"], "checks": checks},
        level="INFO" if all_healthy else "WARNING"
    )

    return func.HttpResponse(
        json.dumps(response_body, indent=2),
        status_code=status_code,
        mimetype="application/json"
    )


# ══════════════════════════════════════════════════════════════════════
# LOG ANALYTICS QUERIES (paste these in Azure Portal → Log Analytics)
# ══════════════════════════════════════════════════════════════════════

LOG_ANALYTICS_QUERIES = {
    "documents_processed_today": """
        traces
        | where timestamp > ago(24h)
        | where message contains "document_processed"
        | extend props = parse_json(message)
        | summarize count() by tostring(props.document_type)
        | order by count_ desc
    """,

    "average_processing_time": """
        traces
        | where message contains "timing"
        | extend props = parse_json(message)
        | where props.operation == "ai_model_call"
        | summarize avg_ms=avg(todouble(props.duration_ms)) by tostring(props.document_type)
    """,

    "failed_extractions": """
        exceptions
        | where timestamp > ago(24h)
        | where outerMessage contains "extraction"
        | project timestamp, outerMessage, details
        | order by timestamp desc
    """,

    "api_errors_by_route": """
        traces
        | where message contains "apim_request"
        | extend props = parse_json(message)
        | where props.status >= 400
        | summarize count() by tostring(props.route), toint(props.status)
        | order by count_ desc
    """,

    "cost_estimate_by_day": """
        traces
        | where message contains "ai_model_call"
        | extend props = parse_json(message)
        | extend cost = todouble(props.estimated_cost_usd)
        | summarize daily_cost_usd=sum(cost) by bin(timestamp, 1d)
        | order by timestamp desc
    """,
}
