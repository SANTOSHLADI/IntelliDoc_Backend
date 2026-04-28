"""
Azure Function App - IDP (Intelligent Document Processing)

Routes:
  POST /api/presignedurl       -> Generate SAS URL for Blob upload
  POST /api/process            -> Process a document (OCR + AI extraction)
  GET  /api/getdocument/{id}   -> Retrieve document metadata from Cosmos DB
  POST /api/ocr                -> Invoice OCR extraction
  POST /api/search             -> Semantic search over indexed documents
  POST /api/index              -> Manually index a document
  POST /api/upload             -> Direct file upload + process
  POST /api/chat               -> Conversational Q&A about a processed document  ← NEW
  GET  /api/health             -> Health check for all services
"""

import azure.functions as func
import json
import logging

from src.blob_storage.sas_url import handle_sas_url_request
from src.document_processing.model_inference import process_document_with_model
from src.document_processing.utils import download_file_from_blob, extract_content
from src.cosmos_db.store_metadata import store_metadata_in_cosmos
from src.cosmos_db.retrieve_metadata import retrieve_latest_data_from_cosmos
from src.document_processing.ocr_inference import process_document_ocr
from src.ai_search.search_client import search_documents, index_document
from src.config_loader import CONFIG
from src.api_management.apim_policy import get_caller_identity, log_apim_request
from src.monitoring.azure_monitor import log_document_processed, log_event, health_check_handler
from src.event_grid.event_grid_trigger import register_event_grid_trigger

logger = logging.getLogger("azure.idp")
logger.setLevel(logging.INFO)

# ──────────────────────────────────────────────
# Auth level: ANONYMOUS so frontend does not
# need to send a ?code=... key.
# Authentication is handled by APIM (x-functions-key header).
# ──────────────────────────────────────────────
app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# ──────────────────────────────────────────────
# CORS — allow requests from the Azure Static Web App
# ──────────────────────────────────────────────
STATIC_WEB_APP_URL = "https://ashy-rock-069670d0f.1.azurestaticapps.net"

CORS_HEADERS = {
    "Access-Control-Allow-Origin": STATIC_WEB_APP_URL,
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, x-functions-key",
}


def cors_response(body: str, status_code: int = 200) -> func.HttpResponse:
    """Helper — returns a JSON response with CORS headers attached."""
    return func.HttpResponse(
        body,
        status_code=status_code,
        mimetype="application/json",
        headers=CORS_HEADERS,
    )


def options_response() -> func.HttpResponse:
    """Handle CORS preflight OPTIONS requests."""
    return func.HttpResponse(
        "",
        status_code=204,
        headers=CORS_HEADERS,
    )


# ──────────────────────────────────────────────
# 1. PRESIGNED / SAS URL  (POST /api/presignedurl)
# ──────────────────────────────────────────────
@app.route(route="presignedurl", methods=["POST", "OPTIONS"])
def generate_sas_url(req: func.HttpRequest) -> func.HttpResponse:
    """Generate a SAS URL for direct Blob Storage upload."""
    if req.method == "OPTIONS":
        return options_response()

    try:
        body = req.get_json()
    except ValueError:
        return cors_response(json.dumps({"error": "Invalid JSON body"}), 400)

    result = handle_sas_url_request(body)
    return cors_response(
        json.dumps(result.get("body", {})),
        result.get("statusCode", 500),
    )


# ──────────────────────────────────────────────
# 2. PROCESS DOCUMENT  (POST /api/process)
# ──────────────────────────────────────────────
@app.route(route="process", methods=["POST", "OPTIONS"])
def process_document(req: func.HttpRequest) -> func.HttpResponse:
    """
    Download a file from Blob Storage, auto-classify, run AI extraction, store result.
    Body:
      {
        "file_key":        "uploads/myfile.pdf",
        "document_type":   "(optional) skip auto-classification if provided",
        "User_instruction":"optional extra instruction",
        "output_language": "Hindi"   (optional, default English — NEW)
      }
    """
    if req.method == "OPTIONS":
        return options_response()

    from src.document_processing.classifier import classify_document

    try:
        event = req.get_json()
    except ValueError:
        return cors_response(json.dumps({"error": "Invalid JSON body"}), 400)

    file_key = event.get("file_key")
    if not file_key:
        return cors_response(json.dumps({"error": "file_key is required"}), 400)

    file_name        = file_key.split("/")[-1]
    user_instruction = event.get("User_instruction", None)
    requested_type   = event.get("document_type")

    # --- Download from Blob ---
    try:
        local_path = download_file_from_blob(file_key)
    except FileNotFoundError as e:
        return cors_response(
            json.dumps({"error": "File not found in Blob Storage", "details": str(e)}), 404
        )
    except Exception as e:
        return cors_response(
            json.dumps({"error": "Failed to download file", "details": str(e)}), 500
        )

    # --- Extract content ---
    try:
        file_content = extract_content(local_path)
    except ValueError as e:
        return cors_response(
            json.dumps({"error": "Failed to extract content", "details": str(e)}), 400
        )

    input_type = "image" if isinstance(file_content, list) else "text"

    # --- AI Classification ---
    if requested_type:
        document_type = requested_type
        logger.info(f"Using caller-supplied document_type: {document_type}")
    else:
        document_type = classify_document(file_content, input_type)
        logger.info(f"Auto-classified as: {document_type}")

    # --- AI Model Inference ---
    try:
        model_response = process_document_with_model(
            document_input=file_content,
            document_type=document_type,
            input_type=input_type,
            user_instruction=user_instruction
        )
    except Exception as e:
        return cors_response(
            json.dumps({"error": "Failed to process document with AI model", "details": str(e)}), 500
        )

    response_data = model_response.get("res", {})

    # Only normalize for identity documents
    if document_type not in ("ocr_all_documents", "invoice"):
        response_data = _clean_and_normalize(response_data, document_type)

    if isinstance(response_data, dict):
        response_data["_document_type"] = document_type

    # --- Store in Cosmos DB ---
    if model_response.get("statusCode") == 200:
        try:
            store_metadata_in_cosmos(
                document_type=document_type,
                response_data=response_data,
                file_name=file_name
            )
        except Exception as e:
            logger.error(f"Cosmos DB storage error: {e}")

        # --- Index in Azure AI Search ---
        try:
            index_document(
                document_id=file_name,
                document_type=document_type,
                extracted_data=response_data,
                file_name=file_name,
            )
        except Exception as e:
            logger.warning(f"AI Search indexing error (non-fatal): {e}")

    return cors_response(
        json.dumps(
            response_data if isinstance(response_data, dict) else {"error": "Invalid response"}
        ),
        model_response.get("statusCode", 500),
    )


# ──────────────────────────────────────────────
# 3. GET DOCUMENT  (GET /api/getdocument/{document_id})
# ──────────────────────────────────────────────
@app.route(route="getdocument/{document_id}", methods=["GET", "OPTIONS"])
def get_document(req: func.HttpRequest) -> func.HttpResponse:
    """Retrieve the latest processed metadata for a document from Cosmos DB."""
    if req.method == "OPTIONS":
        return options_response()

    document_id = req.route_params.get("document_id")
    if not document_id:
        return cors_response(json.dumps({"error": "document_id is required"}), 400)

    result = retrieve_latest_data_from_cosmos(document_id)
    return cors_response(
        json.dumps(result.get("body", {})),
        result.get("statusCode", 500),
    )


# ──────────────────────────────────────────────
# 4. OCR ENDPOINT  (POST /api/ocr)
# ──────────────────────────────────────────────
@app.route(route="ocr", methods=["POST", "OPTIONS"])
def ocr_document(req: func.HttpRequest) -> func.HttpResponse:
    """
    Invoice / general OCR endpoint.
    Body:
      {
        "file_key":        "uploads/invoice.pdf",
        "document_type":   "invoice | ocr_all_documents",
        "User_instruction":"optional",
        "output_language": "Tamil"   (optional — NEW)
      }
    """
    if req.method == "OPTIONS":
        return options_response()

    try:
        event = req.get_json()
    except ValueError:
        return cors_response(json.dumps({"error": "Invalid JSON body"}), 400)

    file_key = event.get("file_key")
    if not file_key:
        return cors_response(json.dumps({"error": "Missing file_key"}), 400)

    doc_type         = event.get("document_type", "invoice")
    user_instruction = event.get("User_instruction", None)

    local_path = download_file_from_blob(file_key)
    if not local_path:
        return cors_response(
            json.dumps({"error": f"Failed to download: {file_key}"}), 404
        )

    force_image = doc_type.lower() == "ocr_all_documents"
    import os as _os
    max_pages = int(_os.environ.get("MAX_PAGES", "10"))
    if force_image:
        from src.document_processing.utils import _extract_pdf_images_as_base64, _extract_image_content
        if local_path.lower().endswith((".png", ".jpg", ".jpeg")):
            file_content = _extract_image_content(local_path)
        else:
            file_content = _extract_pdf_images_as_base64(local_path, max_pages=max_pages)
    else:
        file_content = extract_content(local_path, force_images=False)
    input_type = "image" if isinstance(file_content, list) or force_image else "text"

    model_response = process_document_ocr(
        document_input=file_content,
        input_type=input_type,
        doc_type=doc_type,
        user_instruction=user_instruction,
        file_key=file_key
    )

    output    = model_response.get("body", {}).get("output", "")
    file_name = file_key.split("/")[-1]

    # Index into Azure AI Search
    try:
        index_data = output if isinstance(output, dict) else {"content": str(output)}
        index_document(
            document_id=file_name,
            document_type=doc_type,
            extracted_data=index_data,
            file_name=file_name,
        )
    except Exception as e:
        logger.warning(f"AI Search indexing error (non-fatal): {e}")

    # Store in Cosmos DB
    try:
        store_metadata_in_cosmos(
            document_type=doc_type,
            response_data=index_data,
            file_name=file_name,
        )
    except Exception as e:
        logger.warning(f"Cosmos DB storage error (non-fatal): {e}")

    return cors_response(json.dumps({"status": "success", "output": output}))


# ──────────────────────────────────────────────
# 5. SEMANTIC SEARCH  (POST /api/search)
# ──────────────────────────────────────────────
@app.route(route="search", methods=["POST", "OPTIONS"])
def search(req: func.HttpRequest) -> func.HttpResponse:
    """
    Semantic / keyword search over indexed documents via Azure AI Search.
    Body:
      {
        "query":         "invoice from Acme Corp January 2024",
        "document_type": "invoice",   // optional filter
        "top":           5,           // optional, default 5
        "file_name":     "myfile.pdf" // optional scope to one file
      }
    """
    if req.method == "OPTIONS":
        return options_response()

    try:
        body = req.get_json()
    except ValueError:
        return cors_response(json.dumps({"error": "Invalid JSON body"}), 400)

    query = body.get("query")
    if not query:
        return cors_response(json.dumps({"error": "query is required"}), 400)

    result = search_documents(
        query=query,
        document_type=None,
        top=body.get("top", 5),
        use_semantic=False,
        file_name=body.get("file_name"),
    )
    return cors_response(
        json.dumps(result.get("body", {})),
        result.get("statusCode", 500),
    )


# ──────────────────────────────────────────────
# 6. INDEX DOCUMENT  (POST /api/index)
# ──────────────────────────────────────────────
@app.route(route="index", methods=["POST", "OPTIONS"])
def index_doc(req: func.HttpRequest) -> func.HttpResponse:
    """
    Manually index a document's extracted data into Azure AI Search.
    Body:
      {
        "document_id":    "invoice.pdf",
        "document_type":  "invoice",
        "extracted_data": { ... },
        "file_name":      "invoice.pdf"
      }
    """
    if req.method == "OPTIONS":
        return options_response()

    try:
        body = req.get_json()
    except ValueError:
        return cors_response(json.dumps({"error": "Invalid JSON body"}), 400)

    result = index_document(
        document_id=body.get("document_id", ""),
        document_type=body.get("document_type", ""),
        extracted_data=body.get("extracted_data", {}),
        file_name=body.get("file_name"),
    )
    return cors_response(
        json.dumps(result.get("body", {})),
        result.get("statusCode", 500),
    )


# ──────────────────────────────────────────────
# 7. DIRECT FILE UPLOAD  (POST /api/upload)
# ──────────────────────────────────────────────
@app.route(route="upload", methods=["POST", "OPTIONS"])
def upload_document(req: func.HttpRequest) -> func.HttpResponse:
    """
    Upload a file directly via form-data and process it.
    Form fields:
      file          - the document file (required)
      document_type - invoice | identity_card | bank_statement | loan_application (optional)
    """
    if req.method == "OPTIONS":
        return options_response()

    import uuid
    from azure.storage.blob import BlobServiceClient

    file = req.files.get("file")
    if not file:
        return cors_response(
            json.dumps({"error": "No file provided. Use form-data with key 'file'"}), 400
        )

    document_type = req.form.get("document_type", CONFIG["cosmos"]["default_document_type"])
    filename  = file.filename or f"upload-{uuid.uuid4()}.pdf"
    blob_name = f"uploads/{filename}"

    try:
        file_bytes = file.read()
    except Exception as e:
        logger.error(f"Failed to read uploaded file: {e}")
        return cors_response(json.dumps({"error": f"Failed to read file: {str(e)}"}), 400)

    try:
        blob_client = BlobServiceClient.from_connection_string(
            CONFIG["blob"]["connection_string"]
        ).get_blob_client(container=CONFIG["blob"]["container_name"], blob=blob_name)
        blob_client.upload_blob(file_bytes, overwrite=True)
        logger.info(f"Blob uploaded successfully: {blob_name}")
    except Exception as e:
        logger.error(f"Blob upload failed: {e}")
        return cors_response(json.dumps({"error": f"Upload failed: {str(e)}"}), 500)

    try:
        import tempfile, os
        suffix   = os.path.splitext(filename)[1].lower() or ".pdf"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        document_images = extract_content(tmp_path, document_type)
        os.unlink(tmp_path)

        if not document_images:
            return cors_response(
                json.dumps({"error": "Could not extract content from file"}), 422
            )

        result = process_document_with_model(
            document_input=document_images,
            document_type=document_type,
            input_type="image" if isinstance(document_images, list) else "text"
        )

        if result.get("statusCode", 500) != 200:
            logger.error(f"Processing failed: {result}")
            return cors_response(
                json.dumps({"error": result.get("error", "Processing failed"), "blob": blob_name}),
                result.get("statusCode", 500),
            )

        extracted_data = result.get("res", {})

    except Exception as e:
        logger.error(f"Post-upload processing failed: {e}")
        return cors_response(
            json.dumps({"error": f"Processing failed: {str(e)}", "blob": blob_name}), 500
        )

    try:
        store_metadata_in_cosmos(document_type, extracted_data, filename)
        document_id = extracted_data.get("id", filename)
        logger.info(f"Stored in Cosmos DB: {document_id}")
    except Exception as e:
        logger.error(f"Cosmos DB storage failed: {e}")
        return cors_response(
            json.dumps({"warning": "Processed but not stored", "data": extracted_data})
        )

    return cors_response(
        json.dumps({"document_id": document_id, "blob": blob_name, "data": extracted_data})
    )


# ──────────────────────────────────────────────
# 8. DOCUMENT CHAT  (POST /api/chat)   ← NEW
# ──────────────────────────────────────────────
@app.route(route="chat", methods=["POST", "OPTIONS"])
def chat_with_document(req: func.HttpRequest) -> func.HttpResponse:
    """
    Conversational Q&A about a previously processed document.

    The backend:
      1. Fetches the extracted document content from Cosmos DB (documents container)
      2. Builds a GPT-4o prompt: system instruction + document context + history + question
      3. Calls GPT-4o (temperature 0.2 for factual, grounded answers)
      4. Saves updated conversation to Cosmos DB chat-history container
      5. Returns the answer + updated history to the frontend

    Body:
      {
        "file_name":       "invoice.pdf",          (required)
        "question":        "What is the total?",   (required)
        "history":         [                        (optional)
          {"role": "user",      "content": "..."},
          {"role": "assistant", "content": "..."}
        ],
        "output_language": "Hindi"                 (optional, default "English")
      }

    Returns:
      { "answer": "...", "history": [...updated history...] }
    """
    if req.method == "OPTIONS":
        return options_response()

    try:
        body = req.get_json()
    except ValueError:
        return cors_response(json.dumps({"error": "Invalid JSON body"}), 400)

    file_name       = (body.get("file_name") or "").strip()
    question        = (body.get("question") or "").strip()
    history         = body.get("history", [])          # list of {role, content}
    output_language = body.get("output_language", "English")

    if not file_name:
        return cors_response(json.dumps({"error": "file_name is required"}), 400)
    if not question:
        return cors_response(json.dumps({"error": "question is required"}), 400)

    # ── Step 1: Fetch extracted document content from Cosmos DB ──────────────
    try:
        cosmos_result = retrieve_latest_data_from_cosmos(file_name)
        if cosmos_result.get("statusCode") != 200:
            return cors_response(
                json.dumps({
                    "error": (
                        f"Document '{file_name}' not found in the database. "
                        "Please process the document first before chatting about it."
                    )
                }),
                404,
            )
        doc_record     = cosmos_result["body"]
        # Extracted data lives in ResponseData field
        extracted_data = doc_record.get("ResponseData") or doc_record
        document_context = json.dumps(extracted_data, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Cosmos DB fetch error in /api/chat for '{file_name}': {e}")
        return cors_response(
            json.dumps({"error": f"Failed to retrieve document data: {str(e)}"}), 500
        )

    # ── Step 2: Build GPT-4o prompt ──────────────────────────────────────────
    language_instruction = (
        f"Always respond in {output_language} language."
        if output_language and output_language.lower() != "english"
        else "Respond in English."
    )

    system_prompt = (
        "You are an intelligent document analysis assistant.\n"
        "Your job is to answer questions about the document whose extracted data is provided below.\n\n"
        "STRICT RULES:\n"
        "1. Only answer based on the provided document content. Do NOT make up or guess information.\n"
        "2. If the answer is not present in the document, clearly say so.\n"
        "3. Be concise and precise. Use bullet points or short tables when helpful.\n"
        "4. For numerical questions, show the exact values from the document.\n"
        "5. For follow-up questions that reference earlier messages (e.g. 'What about March?'), "
        "   use the conversation history to understand what is being asked.\n"
        f"6. {language_instruction}\n\n"
        "--- EXTRACTED DOCUMENT CONTENT ---\n"
        f"{document_context}\n"
        "--- END OF DOCUMENT CONTENT ---"
    )

    # Build messages array: system prompt + conversation history + new question
    messages = [{"role": "system", "content": system_prompt}]
    for msg in history:
        role    = msg.get("role", "user")
        content = msg.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})

    # ── Step 3: Call GPT-4o ──────────────────────────────────────────────────
    try:
        from openai import AzureOpenAI
        openai_client = AzureOpenAI(
            api_key=CONFIG["azure_openai"]["api_key"],
            azure_endpoint=CONFIG["azure_openai"]["endpoint"],
            api_version=CONFIG["azure_openai"]["api_version"],
        )
        completion = openai_client.chat.completions.create(
            model=CONFIG["azure_openai"]["deployment_name"],
            messages=messages,
            max_tokens=1024,
            temperature=0.2,   # low temperature = factual, grounded answers
        )
        answer = completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"OpenAI chat completion error for '{file_name}': {e}")
        return cors_response(
            json.dumps({"error": f"AI model error: {str(e)}"}), 500
        )

    # ── Step 4: Save conversation to Cosmos DB chat-history container ────────
    try:
        from azure.cosmos import CosmosClient
        from datetime import datetime, timezone, timedelta

        cosmos_client = CosmosClient(
            url=CONFIG["cosmos"]["endpoint"],
            credential=CONFIG["cosmos"]["key"],
        )
        db = cosmos_client.get_database_client(CONFIG["cosmos"]["database_name"])
        chat_container = db.get_container_client(
            CONFIG["cosmos"].get("chat_container_name", "chat-history")
        )

        # IST = UTC + 5:30
        now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)

        updated_history = list(history) + [
            {"role": "user",      "content": question},
            {"role": "assistant", "content": answer},
        ]

        chat_container.upsert_item({
            "id":           f"{file_name}_chat",
            "file_key":     file_name,          # partition key
            "history":      updated_history,
            "last_updated": now_ist.strftime("%Y-%m-%d %H:%M:%S"),
        })
        logger.info(f"Chat history saved for '{file_name}'")
    except Exception as e:
        # Non-fatal — the answer was generated; just log and continue
        logger.warning(f"Chat history save error for '{file_name}' (non-fatal): {e}")

    # ── Step 5: Return answer + updated history to frontend ──────────────────
    updated_history_response = list(history) + [
        {"role": "user",      "content": question},
        {"role": "assistant", "content": answer},
    ]

    return cors_response(json.dumps({
        "answer":  answer,
        "history": updated_history_response,
    }))


# ──────────────────────────────────────────────
# 9. HEALTH CHECK  (GET /api/health)
# ──────────────────────────────────────────────
@app.route(route="health", methods=["GET", "OPTIONS"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    """Returns 200 + status of all connected services."""
    if req.method == "OPTIONS":
        return options_response()
    return health_check_handler(req)


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
_BLOCKED_KEYS = {
    "income", "loan_amount", "collateral", "employment", "fraud_indicators",
    "monthly_income", "existing_loans", "collateral_details", "employer_name",
    "loan_type", "loan_tenure_months", "purpose_of_loan", "co_applicant_name",
    "guarantor_name", "employment_type", "loan_amount_requested",
}

_ALLOWED_KEYS: dict[str, set] = {
    "aadhaar":  {"name", "gender", "date_of_birth", "aadhaar_number", "address", "_document_type"},
    "pan":      {"name", "father_name", "dob", "pan_number", "_document_type"},
    "passport": {
        "surname", "given_names", "nationality", "dob", "gender",
        "passport_number", "issue_date", "expiry_date",
        "place_of_birth", "issuing_authority", "_document_type",
    },
    "voter_id": {
        "name", "father_or_husband_name", "dob", "gender",
        "epic_number", "address", "part_number", "_document_type",
    },
    "driving_license": {
        "name", "dob", "dl_number", "issue_date", "expiry_date",
        "address", "vehicle_classes", "issuing_rto", "_document_type",
    },
}


def _clean_and_normalize(data, doc_type: str = None):
    """Remove null, empty, placeholder values and enforce per-type key whitelist."""
    if isinstance(data, dict):
        allowed = _ALLOWED_KEYS.get(doc_type) if doc_type else None
        cleaned = {}
        for k, v in data.items():
            if k in _BLOCKED_KEYS:
                continue
            if allowed and k not in allowed:
                continue
            if v is None:
                continue
            if v in ["Not Available", "NA", "N/A", "", "null", "None"]:
                continue
            if isinstance(v, str) and v.strip().lower() in ["fe-male", "female"]:
                v = "Female"
            cleaned_v = _clean_and_normalize(v, doc_type)
            if cleaned_v in ({}, [], ""):
                continue
            cleaned[k] = cleaned_v
        return cleaned
    elif isinstance(data, list):
        return [_clean_and_normalize(i, doc_type) for i in data if i is not None]
    return data


# ──────────────────────────────────────────────
# EVENT GRID AUTO-TRIGGER
# ──────────────────────────────────────────────
register_event_grid_trigger(app)
