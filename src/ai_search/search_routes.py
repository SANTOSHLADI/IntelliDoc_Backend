"""
Additional Azure Function routes for AI Search.
Add these routes to function_app.py (or keep as a separate file and import).

These replace any vector/semantic search functionality that would have
been in AWS AI Foundry.
"""

import azure.functions as func
import json
from src.ai_search.search_client import search_documents, index_document

# ──────────────────────────────────────────────
# SEARCH  (POST /api/search)
# ──────────────────────────────────────────────
# To add to function_app.py:
#
# @app.route(route="search", methods=["POST"])
# def search(req: func.HttpRequest) -> func.HttpResponse:
#     body = req.get_json()
#     query = body.get("query")
#     doc_type = body.get("document_type", None)
#     top = body.get("top", 5)
#
#     if not query:
#         return func.HttpResponse(
#             json.dumps({"error": "query is required"}),
#             status_code=400, mimetype="application/json"
#         )
#
#     result = search_documents(query=query, document_type=doc_type, top=top)
#     return func.HttpResponse(
#         json.dumps(result.get("body", {})),
#         status_code=result.get("statusCode", 500),
#         mimetype="application/json"
#     )


# ──────────────────────────────────────────────
# INDEX DOCUMENT  (POST /api/index)
# ──────────────────────────────────────────────
# To add to function_app.py:
#
# @app.route(route="index", methods=["POST"])
# def index(req: func.HttpRequest) -> func.HttpResponse:
#     body = req.get_json()
#     result = index_document(
#         document_id=body.get("document_id"),
#         document_type=body.get("document_type"),
#         extracted_data=body.get("extracted_data", {}),
#         file_name=body.get("file_name"),
#     )
#     return func.HttpResponse(
#         json.dumps(result.get("body", {})),
#         status_code=result.get("statusCode", 500),
#         mimetype="application/json"
#     )
