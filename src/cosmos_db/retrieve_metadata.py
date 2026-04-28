"""
Azure Cosmos DB metadata retrieval.
Replaces AWS: src/retrieval_data/metadata_info.py

Key changes:
  - boto3 DynamoDB query  →  Cosmos DB SQL API query
  - Returns latest record by Timestamp for a given DocumentID
"""

import json
import logging
from azure.cosmos import CosmosClient, exceptions as cosmos_exceptions
from src.config_loader import CONFIG

logger = logging.getLogger("azure.idp.cosmos_retrieve")

_BLOCKED_KEYS = {
    "income", "loan_amount", "collateral", "employment", "fraud_indicators",
    "monthly_income", "existing_loans", "collateral_details", "employer_name",
    "loan_type", "loan_tenure_months", "purpose_of_loan", "co_applicant_name",
    "guarantor_name", "employment_type", "loan_amount_requested",
}

def _clean_response_data(data, doc_type=None):
    """Strip nulls, empty values, and blocked keys from stored response data."""
    if isinstance(data, dict):
        return {
            k: _clean_response_data(v, doc_type)
            for k, v in data.items()
            if k not in _BLOCKED_KEYS
            and v is not None
            and v not in ["", "N/A", "NA", "Not Available", "null", "None"]
            and _clean_response_data(v, doc_type) not in ({}, [])
        }
    if isinstance(data, list):
        return [_clean_response_data(i, doc_type) for i in data if i is not None]
    return data


def _get_container():
    client = CosmosClient(
        url=CONFIG["cosmos"]["endpoint"],
        credential=CONFIG["cosmos"]["key"],
    )
    db = client.get_database_client(CONFIG["cosmos"]["database_name"])
    return db.get_container_client(CONFIG["cosmos"]["container_name"])


def retrieve_latest_data_from_cosmos(document_id: str) -> dict:
    """
    Retrieve the most recent document metadata for a given DocumentID.
    Equivalent to retrieve_latest_data_from_dynamodb().

    Args:
        document_id: The document ID to look up (e.g. "invoice.pdf")

    Returns:
        {"statusCode": 200, "body": {...}}  on success
        {"statusCode": 404, "body": {...}}  if not found
        {"statusCode": 500, "body": {...}}  on error
    """
    try:
        container = _get_container()

        # Cosmos SQL: order by Timestamp DESC, take 1
        query = (
            "SELECT TOP 1 * FROM c "
            "WHERE c.DocumentID = @docId "
            "ORDER BY c.Timestamp DESC"
        )
        params = [{"name": "@docId", "value": document_id}]

        items = list(
            container.query_items(
                query=query,
                parameters=params,
                enable_cross_partition_query=True,
            )
        )

        if items:
            record = items[0]
            for internal_key in ["_rid", "_self", "_etag", "_attachments", "_ts"]:
                record.pop(internal_key, None)

            # Clean ResponseData in case it was stored before null-stripping was in place
            if "ResponseData" in record and isinstance(record["ResponseData"], dict):
                doc_type = record.get("PolicyType")
                record["ResponseData"] = _clean_response_data(record["ResponseData"], doc_type)

            return {"statusCode": 200, "body": record}
        else:
            return {
                "statusCode": 404,
                "body": json.dumps({"error": f"No documents found with ID '{document_id}'"}),
            }

    except cosmos_exceptions.CosmosHttpResponseError as e:
        logger.error(f"Cosmos DB HTTP error retrieving '{document_id}': {e}")
        return {
            "statusCode": e.status_code or 500,
            "body": json.dumps({"error": str(e)}),
        }
    except Exception as e:
        logger.error(f"Unexpected error retrieving '{document_id}': {e}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e), "type": type(e).__name__}),
        }
