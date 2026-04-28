"""
Azure Event Grid Integration
============================

In your architecture diagram:
    Blob Storage → Event Grid → Function App → (AI extraction pipeline)

Event Grid replaces the manual "POST /api/process" call.
When a file is uploaded to Blob Storage, Event Grid automatically fires an
event that triggers the Function App — no manual trigger needed.

Flow:
  1. Client uploads file to Blob Storage (via SAS URL)
  2. Blob Storage fires a BlobCreated event to Event Grid
  3. Event Grid delivers the event to this Function App endpoint
  4. Function App automatically starts AI extraction
  5. Result saved to Cosmos DB + indexed in AI Search

This file adds:
  - event_grid_trigger() — the Event Grid triggered function
  - parse_blob_event()   — extracts file info from the event payload
  - infer_document_type()— guesses document type from filename/folder
"""

import json
import logging
import re
import azure.functions as func

from src.document_processing.model_inference import process_document_with_model
from src.document_processing.utils import download_file_from_blob, extract_content
from src.cosmos_db.store_metadata import store_metadata_in_cosmos
from src.ai_search.search_client import index_document
from src.config_loader import CONFIG

logger = logging.getLogger("azure.idp.eventgrid")


# ══════════════════════════════════════════════════════════════════════
# EVENT GRID TRIGGER FUNCTION
# Add this to function_app.py  alongside the HTTP routes
# ══════════════════════════════════════════════════════════════════════

def register_event_grid_trigger(app: func.FunctionApp):
    """
    Registers the Event Grid trigger on the Function App.
    Call this from function_app.py:

        from src.event_grid.event_grid_trigger import register_event_grid_trigger
        register_event_grid_trigger(app)

    Azure Portal setup:
      1. Go to your Storage Account → Events → + Event Subscription
      2. Name: idp-blob-created
      3. Event types: Microsoft.Storage.BlobCreated
      4. Filter: Subject begins with /blobServices/default/containers/idp-documents/blobs/uploads/
      5. Endpoint type: Azure Function
      6. Endpoint: select your Function App → event_grid_blob_handler
    """

    @app.event_grid_trigger(arg_name="event")
    def event_grid_blob_handler(event: func.EventGridEvent):
        """
        Triggered automatically when a file is uploaded to Blob Storage.
        Runs the full AI extraction pipeline without any manual API call.
        """
        try:
            logger.info(f"Event Grid event received: {event.event_type} for {event.subject}")

            # Only process BlobCreated events
            if event.event_type != "Microsoft.Storage.BlobCreated":
                logger.info(f"Skipping event type: {event.event_type}")
                return

            # Parse the blob info from the event
            blob_info = parse_blob_event(event)
            if not blob_info:
                logger.warning("Could not parse blob info from event — skipping")
                return

            file_key = blob_info["blob_path"]       # e.g. "uploads/invoice.pdf"
            file_name = blob_info["file_name"]       # e.g. "invoice.pdf"
            file_size = blob_info["content_length"]  # bytes

            logger.info(f"Auto-processing blob: {file_key} ({file_size} bytes)")

            # Skip non-document files (thumbnails, temp files, system files)
            if not _is_processable_file(file_name):
                logger.info(f"Skipping non-document file: {file_name}")
                return

            # Infer document type from filename or folder structure
            document_type = infer_document_type(file_key)
            logger.info(f"Inferred document type: {document_type} for {file_name}")

            # Download file from Blob Storage to /tmp/
            local_path = download_file_from_blob(file_key)

            # Extract content (type-agnostic)
            file_content = extract_content(local_path)
            input_type = "image" if isinstance(file_content, list) else "text"

            # AI classification — try filename hint first, fall back to content-based
            filename_hint = infer_document_type(file_key)
            if filename_hint != CONFIG["cosmos"]["default_document_type"]:
                # Filename gave a confident hint — use it, skip AI classification call
                document_type = filename_hint
                logger.info(f"Filename-inferred type: {document_type}")
            else:
                from src.document_processing.classifier import classify_document
                document_type = classify_document(file_content, input_type)
                logger.info(f"AI-classified type: {document_type}")

            # Run AI extraction
            model_response = process_document_with_model(
                document_input=file_content,
                document_type=document_type,
                input_type=input_type,
            )

            if model_response.get("statusCode") == 200:
                # Save to Cosmos DB
                store_metadata_in_cosmos(
                    document_type=document_type,
                    response_data=model_response,
                    file_name=file_name,
                )

                # Index in Azure AI Search
                index_document(
                    document_id=file_name,
                    document_type=document_type,
                    extracted_data=model_response.get("res", {}),
                    file_name=file_name,
                )

                logger.info(f"Event Grid: Successfully processed {file_name} as {document_type}")
            else:
                logger.error(
                    f"Event Grid: AI extraction failed for {file_name}: "
                    f"{model_response.get('error', 'unknown error')}"
                )

        except Exception as e:
            logger.error(f"Event Grid handler error: {e}", exc_info=True)
            # Do NOT re-raise — Event Grid will retry if we raise an exception
            # Swallow the error here to avoid infinite retry loops


# ══════════════════════════════════════════════════════════════════════
# EVENT PARSING
# ══════════════════════════════════════════════════════════════════════

def parse_blob_event(event: func.EventGridEvent) -> dict | None:
    """
    Extracts blob storage details from an Event Grid BlobCreated event.

    Event Grid sends events in this format:
    {
      "subject": "/blobServices/default/containers/idp-documents/blobs/uploads/invoice.pdf",
      "data": {
        "url": "https://idpstorage.blob.core.windows.net/idp-documents/uploads/invoice.pdf",
        "contentType": "application/pdf",
        "contentLength": 102400
      }
    }
    """
    try:
        data = event.get_json()  # The 'data' field of the event

        blob_url = data.get("url", "")
        content_type = data.get("contentType", "")
        content_length = data.get("contentLength", 0)

        # Extract blob path from subject
        # subject = "/blobServices/default/containers/idp-documents/blobs/uploads/invoice.pdf"
        subject = event.subject
        blob_path_match = re.search(r"/blobs/(.+)$", subject)
        if not blob_path_match:
            logger.error(f"Could not extract blob path from subject: {subject}")
            return None

        blob_path = blob_path_match.group(1)      # "uploads/invoice.pdf"
        file_name = blob_path.split("/")[-1]       # "invoice.pdf"

        return {
            "blob_path": blob_path,
            "file_name": file_name,
            "blob_url": blob_url,
            "content_type": content_type,
            "content_length": content_length,
        }

    except Exception as e:
        logger.error(f"Error parsing Event Grid blob event: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════
# DOCUMENT TYPE INFERENCE
# Guesses the document type from the filename or folder path
# so Event Grid auto-trigger doesn't need the frontend to specify it
# ══════════════════════════════════════════════════════════════════════

# Keyword patterns to detect document type from filename
_TYPE_PATTERNS = {
    "identity_card": [
        r"aadhaar", r"aadhar", r"pan_card", r"pancard", r"passport",
        r"voter_id", r"voterid", r"driving_lic", r"dl_", r"identity",
    ],
    "bank_statement": [
        r"bank_stmt", r"bank_statement", r"statement", r"hdfc", r"sbi",
        r"icici", r"axis_bank", r"kotak", r"account_statement",
    ],
    "loan_application": [
        r"loan", r"loan_app", r"application_form", r"credit_form",
        r"home_loan", r"personal_loan",
    ],
    "cdsl_report": [
        r"cdsl", r"demat", r"portfolio", r"holdings",
    ],
    "invoice": [
        r"invoice", r"bill", r"receipt", r"tax_invoice", r"gst",
    ],
}


def infer_document_type(file_key: str) -> str:
    """
    Infer document type from the blob path/filename.
    Falls back to CONFIG default if no pattern matches.

    Examples:
        "uploads/aadhaar_john.pdf"        → "identity_card"
        "uploads/bank_statement_june.pdf" → "bank_statement"
        "uploads/invoice_acme_2024.pdf"   → "invoice"
        "uploads/myfile.pdf"              → CONFIG default ("invoice")
    """
    name_lower = file_key.lower()

    for doc_type, patterns in _TYPE_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, name_lower):
                return doc_type

    # Also check folder-based routing
    # e.g. uploads/identity_cards/aadhaar.pdf → "identity_card"
    if "/identity" in name_lower or "/id_cards" in name_lower:
        return "identity_card"
    if "/bank" in name_lower or "/statements" in name_lower:
        return "bank_statement"
    if "/loans" in name_lower or "/applications" in name_lower:
        return "loan_application"
    if "/invoices" in name_lower or "/bills" in name_lower:
        return "invoice"

    # Default fallback
    return CONFIG["cosmos"]["default_document_type"]


def _is_processable_file(file_name: str) -> bool:
    """
    Returns True only for supported document file types.
    Skips thumbnail images, temp files, or any non-document file.
    """
    allowed = {".pdf", ".docx", ".png", ".jpg", ".jpeg"}
    skip_prefixes = {"~", ".", "tmp_", "thumb_"}

    name_lower = file_name.lower()

    # Check extension
    ext = "." + name_lower.rsplit(".", 1)[-1] if "." in name_lower else ""
    if ext not in allowed:
        return False

    # Skip temp/system files
    for prefix in skip_prefixes:
        if file_name.startswith(prefix):
            return False

    return True
