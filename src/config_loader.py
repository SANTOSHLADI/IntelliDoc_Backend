"""
Config loader for Azure IDP.
NOW READS SECRETS FROM AZURE KEY VAULT — not plain environment variables.
"""

import os
import logging

logger = logging.getLogger("azure.idp.config")

try:
    from src.key_vault.vault_client import get_secret, SecretNames
    _KV_AVAILABLE = True
except ImportError:
    logger.warning("Key Vault module not available — using env vars only")
    _KV_AVAILABLE = False
    def get_secret(name, fallback_env_var=None):
        return os.environ.get(fallback_env_var, "") if fallback_env_var else ""
    class SecretNames:
        STORAGE_CONNECTION_STRING = ""
        COSMOS_DB_KEY = ""
        COSMOS_DB_ENDPOINT = ""
        OPENAI_API_KEY = ""
        OPENAI_ENDPOINT = ""
        SEARCH_API_KEY = ""
        SEARCH_ENDPOINT = ""
        DOC_INTELLIGENCE_KEY = ""
        DOC_INTELLIGENCE_ENDPOINT = ""
        ENTRA_CLIENT_SECRET = ""
        APIM_BACKEND_SECRET = ""

def _secret(kv_name, env_name, default=""):
    val = get_secret(kv_name, fallback_env_var=env_name)
    return val if val else default

CONFIG = {
    "key_vault": {
        "url": os.environ.get("AZURE_KEY_VAULT_URL", ""),
    },
    "blob": {
        "connection_string": _secret(SecretNames.STORAGE_CONNECTION_STRING, "AZURE_STORAGE_CONNECTION_STRING"),
        "container_name":    os.environ.get("BLOB_CONTAINER_NAME",  "idp-documents"),
        "upload_folder":     os.environ.get("BLOB_UPLOAD_FOLDER",   "uploads"),
        "sas_expiry_minutes":int(os.environ.get("SAS_EXPIRY_MINUTES","30")),
        "allowed_file_types":[".pdf", ".docx", ".png", ".jpeg", ".jpg"],
    },
    "cosmos": {
        "endpoint":             _secret(SecretNames.COSMOS_DB_ENDPOINT, "COSMOS_DB_ENDPOINT"),
        "key":                  _secret(SecretNames.COSMOS_DB_KEY,      "COSMOS_DB_KEY"),
        "database_name":        os.environ.get("COSMOS_DB_DATABASE",    "idp-database"),
        "container_name":       os.environ.get("COSMOS_DB_CONTAINER",   "documents"),
        "chat_container_name":  os.environ.get("COSMOS_CHAT_CONTAINER", "chat-history"),
        "default_document_type":os.environ.get("DEFAULT_DOC_TYPE",      "invoice"),
        "region":               os.environ.get("COSMOS_REGION",         "East US"),
    },
    "ai_search": {
        "endpoint":       _secret(SecretNames.SEARCH_ENDPOINT, "AZURE_SEARCH_ENDPOINT"),
        "api_key":        _secret(SecretNames.SEARCH_API_KEY,  "AZURE_SEARCH_API_KEY"),
        "index_name":     os.environ.get("AZURE_SEARCH_INDEX",           "idp-documents"),
        "semantic_config":os.environ.get("AZURE_SEARCH_SEMANTIC_CONFIG", "default"),
        "api_version":    "2023-11-01",
    },
    "azure_openai": {
        "endpoint":         _secret(SecretNames.OPENAI_ENDPOINT, "AZURE_OPENAI_ENDPOINT"),
        "api_key":          _secret(SecretNames.OPENAI_API_KEY,  "AZURE_OPENAI_API_KEY"),
        "api_version":      os.environ.get("AZURE_OPENAI_API_VERSION",       "2024-02-01"),
        "deployment_name":  os.environ.get("AZURE_OPENAI_DEPLOYMENT",        "gpt-4o"),
        "vision_deployment":os.environ.get("AZURE_OPENAI_VISION_DEPLOYMENT", "gpt-4o"),
        "max_tokens":       int(os.environ.get("OPENAI_MAX_TOKENS",   "4096")),
        "temperature":      float(os.environ.get("OPENAI_TEMPERATURE","0")),
        "top_p":            float(os.environ.get("OPENAI_TOP_P",      "0.9")),
    },
    "document_intelligence": {
        "endpoint":    _secret(SecretNames.DOC_INTELLIGENCE_ENDPOINT, "DOC_INTELLIGENCE_ENDPOINT"),
        "key":         _secret(SecretNames.DOC_INTELLIGENCE_KEY,      "DOC_INTELLIGENCE_KEY"),
        "api_version": "2024-02-29-preview",
    },
    "apim": {
        "backend_secret":    _secret(SecretNames.APIM_BACKEND_SECRET, "APIM_BACKEND_SECRET"),
        "gateway_url":       os.environ.get("APIM_GATEWAY_URL",    ""),
        "static_web_app_url":os.environ.get("STATIC_WEB_APP_URL",  ""),
    },
    "entra": {
        "client_secret": _secret(SecretNames.ENTRA_CLIENT_SECRET, "ENTRA_CLIENT_SECRET"),
        "tenant_id":     os.environ.get("ENTRA_TENANT_ID",  ""),
        "client_id":     os.environ.get("ENTRA_CLIENT_ID",  ""),
        "audience":      os.environ.get("ENTRA_AUDIENCE",   ""),
    },
    "document_upload": {
        "maximum_pages":      int(os.environ.get("MAX_PAGES",      "10")),
        "maximum_pages_bank": int(os.environ.get("MAX_PAGES_BANK", "20")),
        "image_size":         int(os.environ.get("IMAGE_SIZE_MB",  "5")),
        "image_quality":      int(os.environ.get("IMAGE_QUALITY",  "85")),
        "zoom_x":             float(os.environ.get("ZOOM_X",       "2.0")),
        "zoom_y":             float(os.environ.get("ZOOM_Y",       "2.0")),
        "file_type":          [".pdf", ".docx", ".png", ".jpeg", ".jpg"],
    },
    "image_compression": {
        "quality_decrement": int(os.environ.get("IMG_QUALITY_DECREMENT","10")),
        "min_quality":       int(os.environ.get("IMG_MIN_QUALITY",      "30")),
        "size_multiplier":   1024 * 1024,
    },
    "image_prompt": {
        "system_message": (
            "You are an expert AI assistant for document OCR and data extraction. "
            "Extract all relevant fields from the document image. "
            "Return the result as valid JSON wrapped in <response>YOUR_JSON</response> tags."
        ),
        "ocr_all_doc_prompt": (
            "Extract ALL text and data from this document image. "
            "Preserve structure, tables, and key-value pairs. "
            "Return a comprehensive JSON representation wrapped in <response>YOUR_JSON</response> tags."
        ),
    },
    "identity_card": {
        "identity_card_prompts": (
            "Extract all fields from this identity document. "
            "Return JSON: name, dob, id_number, address, gender, issue_date, expiry_date. "
            "Wrap in ```json ... ``` blocks."
        ),
        "identity_typeof_card": (
            "Identify the type of this identity document. "
            'Return JSON: {"document_type": "aadhaar|pan|passport|voter_id|driving_license"}'
        ),
        "aadhaar": "Extract Aadhaar fields: name, dob, gender, aadhaar_number, address. Wrap in ```json``` blocks.",
        "pan":     "Extract PAN fields: name, father_name, dob, pan_number. Wrap in ```json``` blocks.",
        "passport":"Extract Passport fields: surname, given_names, nationality, dob, passport_number, issue_date, expiry_date. Wrap in ```json``` blocks.",
    },
    "bank_statement": {
        "bank_statement_prompts": (
            "Analyze this bank statement and extract: account_holder_name, account_number, "
            "bank_name, statement_period, opening_balance, closing_balance, "
            "total_credits, total_debits, transactions. "
            "Return wrapped in <response>YOUR_JSON</response> tags."
        ),
        "bank_statement_images_extraction": "Extract all text from this bank statement image preserving table structure.",
    },
    "loan_application": {
        "loan_application_prompt": (
            "Analyze this loan application and extract: applicant details, income, "
            "loan amount, collateral, employment, fraud indicators. "
            "Return JSON wrapped in <response>YOUR_JSON</response> tags."
        ),
    },
    "cdsl_report": {
        "cdsl_report_prompts": (
            "Extract from this CDSL report: BO ID, client name, holdings, ISIN, quantity, market value. "
            "Return JSON wrapped in <response>YOUR_JSON</response> tags."
        ),
    },
    "marks_card": {
        "marks_card_prompts": (
            "Extract from this mark sheet: student_name, roll_number, institution_name, "
            "board_or_university, exam_name, month_year_of_exam, subjects (list with marks), "
            "total_marks_obtained, total_max_marks, percentage, result. "
            "Return JSON wrapped in <response>YOUR_JSON</response> tags."
        ),
    },
}
