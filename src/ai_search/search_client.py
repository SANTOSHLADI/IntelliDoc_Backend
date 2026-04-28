"""
Azure AI Search integration for IDP.
- Semantic search disabled (Free tier does not support it)
- Uses simple keyword search (BM25) which works on all tiers
"""

import json
import logging
from datetime import datetime, timezone

import requests
from src.config_loader import CONFIG

logger = logging.getLogger("azure.idp.ai_search")

_ENDPOINT = CONFIG["ai_search"]["endpoint"]
_API_KEY  = CONFIG["ai_search"]["api_key"]
_INDEX    = CONFIG["ai_search"]["index_name"]
_API_VER  = "2023-11-01"

_HEADERS = {
    "Content-Type": "application/json",
    "api-key": _API_KEY,
}


def index_document(document_id, document_type, extracted_data, file_name=None):
    try:
        now = datetime.now(timezone.utc).isoformat()
        content_text = _flatten_to_text(extracted_data)
        search_doc = {
            "id": _safe_id(document_id),
            "documentId": document_id,
            "documentType": document_type,
            "fileName": file_name or document_id,
            "content": content_text,
            "metadata": json.dumps(extracted_data),
            "indexedAt": now,
        }
        url = f"{_ENDPOINT}/indexes/{_INDEX}/docs/index?api-version={_API_VER}"
        payload = {"value": [{"@search.action": "mergeOrUpload", **search_doc}]}
        response = requests.post(url, headers=_HEADERS, json=payload, timeout=30)
        response.raise_for_status()
        logger.info(f"Indexed document '{document_id}'")
        return {"statusCode": 200, "body": {"message": "Document indexed successfully"}}
    except requests.HTTPError as e:
        logger.error(f"AI Search index error: {e.response.text}")
        return {"statusCode": e.response.status_code, "body": {"error": e.response.text}}
    except Exception as e:
        logger.error(f"Unexpected AI Search index error: {e}")
        return {"statusCode": 500, "body": {"error": str(e)}}


def search_documents(query, document_type=None, top=5, use_semantic=False, file_name=None):
    try:
        url = f"{_ENDPOINT}/indexes/{_INDEX}/docs/search?api-version={_API_VER}"
        payload = {
            "search": query,
            "queryType": "simple",
            "top": top,
        }
        filters = []
        if document_type:
            filters.append(f"documentType eq '{document_type}'")
        if file_name:
            safe = file_name.replace("'", "''")
            filters.append(f"fileName eq '{safe}'")
        if filters:
            payload["filter"] = " and ".join(filters)
        response = requests.post(url, headers=_HEADERS, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        results = []
        for r in data.get("value", []):
            content = r.get("content", "")
            highlights = r.get("@search.highlights", {}).get("content", [])
            results.append({
                "fileName": r.get("fileName"),
                "documentType": r.get("documentType"),
                "documentId": r.get("documentId"),
                "score": round(r.get("@search.score", 0), 3),
                "indexedAt": r.get("indexedAt"),
                "preview": highlights[0] if highlights else (
                    content[:2000] + "..." if len(content) > 2000 else content
                ),
            })
        logger.info(f"Search returned {len(results)} results for '{query}'")
        return {"statusCode": 200, "body": {"results": results, "count": len(results)}}
    except requests.HTTPError as e:
        logger.error(f"AI Search query error: {e.response.text}")
        return {"statusCode": e.response.status_code, "body": {"error": e.response.text}}
    except Exception as e:
        logger.error(f"Unexpected AI Search query error: {e}")
        return {"statusCode": 500, "body": {"error": str(e)}}


def delete_document(document_id):
    try:
        url = f"{_ENDPOINT}/indexes/{_INDEX}/docs/index?api-version={_API_VER}"
        payload = {"value": [{"@search.action": "delete", "id": _safe_id(document_id)}]}
        response = requests.post(url, headers=_HEADERS, json=payload, timeout=30)
        response.raise_for_status()
        return {"statusCode": 200, "body": {"message": "Deleted"}}
    except Exception as e:
        return {"statusCode": 500, "body": {"error": str(e)}}


def _safe_id(doc_id):
    return doc_id.replace("/", "_").replace(".", "_").replace(" ", "_")


def _flatten_to_text(data, prefix="", _depth=0):
    """Flatten dict/list to searchable key: value pairs separated by |"""
    parts = []
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                # For nested objects, recurse but keep depth limited
                nested = _flatten_to_text(v, prefix=f"{k}: ", _depth=_depth+1)
                if nested:
                    parts.append(nested)
            else:
                # For scalar values, store as "key: value" directly
                clean_prefix = prefix.split(": ")[-1] if ": " in prefix else prefix
                label = f"{clean_prefix}{k}" if clean_prefix else k
                parts.append(f"{label}: {v}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                parts.append(_flatten_to_text(item, prefix=prefix, _depth=_depth+1))
            else:
                parts.append(f"{prefix}{item}")
    else:
        parts.append(f"{prefix}{data}")
    return " | ".join(p for p in parts if p)