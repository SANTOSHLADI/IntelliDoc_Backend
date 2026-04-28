"""
Azure Cosmos DB metadata storage.
Replaces AWS: src/dynamoDB/store_metadata.py

Key changes:
  - boto3 DynamoDB  →  azure-cosmos CosmosClient
  - Partition key = "document_type" (same data model)
  - DocumentID / Timestamp / PolicyType / ResponseData fields preserved
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from azure.cosmos import CosmosClient, exceptions as cosmos_exceptions
from src.config_loader import CONFIG

logger = logging.getLogger("azure.idp.cosmos_store")


def _get_container():
    """Return the Cosmos DB container client."""
    client = CosmosClient(
        url=CONFIG["cosmos"]["endpoint"],
        credential=CONFIG["cosmos"]["key"],
    )
    db = client.get_database_client(CONFIG["cosmos"]["database_name"])
    return db.get_container_client(CONFIG["cosmos"]["container_name"])


def store_metadata_in_cosmos(
    document_type: str,
    response_data: dict,
    file_name: str = None,
) -> dict:
    """
    Upsert document metadata into Azure Cosmos DB.
    Equivalent to store_metadata_in_dynamodb().

    Item schema (mirrors DynamoDB):
      id            = file_name or document_type   (Cosmos requires 'id' field)
      DocumentID    = file_name or document_type
      PolicyType    = document_type
      Timestamp     = IST datetime string
      ResponseData  = extracted AI response dict
    """
    try:
        container = _get_container()

        # IST = UTC + 5:30
        now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
        timestamp = now_ist.strftime("%Y-%m-%d %H:%M:%S")

        # Unwrap response envelope if present
        if isinstance(response_data, dict) and "res" in response_data:
            response_data = response_data["res"]

        document_id = file_name if file_name else document_type

        item = {
            "id": document_id,               # Cosmos DB required primary key field
            "DocumentID": document_id,
            "PolicyType": document_type,
            "Timestamp": timestamp,
            "ResponseData": response_data,
        }

        container.upsert_item(item)

        logger.info(
            f"Stored metadata: DocumentID={document_id}, "
            f"PolicyType={document_type}, Timestamp={timestamp}"
        )
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Metadata stored successfully",
                "documentId": document_id,
                "policyType": document_type,
                "timestamp": timestamp,
            }),
        }

    except cosmos_exceptions.CosmosHttpResponseError as e:
        logger.error(f"Cosmos DB HTTP error: {e}")
        return {
            "statusCode": e.status_code or 500,
            "body": json.dumps({"error": str(e)}),
        }
    except Exception as e:
        logger.error(f"Unexpected error storing to Cosmos DB: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e), "type": type(e).__name__}),
        }


def store_chat_context(
    query: str,
    model: str,
    file_key: str,
    response: str,
) -> None:
    """
    Store chat/query context for audit and history.
    Replaces store_chat_context() from dynamodb_utils.py.
    """
    try:
        client = CosmosClient(
            url=CONFIG["cosmos"]["endpoint"],
            credential=CONFIG["cosmos"]["key"],
        )
        db = client.get_database_client(CONFIG["cosmos"]["database_name"])
        container = db.get_container_client(CONFIG["cosmos"]["chat_container_name"])

        now_utc = datetime.now(timezone.utc)
        item = {
            "id": f"{file_key}-{now_utc.isoformat()}",
            "file_key": file_key,
            "model": model,
            "query": query,
            "response": response,
            "timestamp": now_utc.isoformat(),
        }
        container.upsert_item(item)
        logger.info(f"Chat context stored for file_key={file_key}")

    except Exception as e:
        logger.warning(f"Could not store chat context: {e}")
