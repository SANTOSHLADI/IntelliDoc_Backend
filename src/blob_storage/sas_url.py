"""
Azure Blob Storage SAS URL generator.
Replaces AWS: src/file_upload/s3_presigned_url.py

Generates a SAS (Shared Access Signature) URL that allows clients to upload
files directly to Azure Blob Storage — equivalent to S3 presigned PUT URLs.
"""

import logging
import os
from datetime import datetime, timezone, timedelta

from azure.storage.blob import (
    BlobServiceClient,
    BlobSasPermissions,
    generate_blob_sas,
)
from src.config_loader import CONFIG

logger = logging.getLogger("azure.idp.blob_sas")


def generate_sas_upload_url(container_name: str, blob_name: str) -> str | None:
    """
    Generate a SAS URL for a client to PUT (upload) a file directly to Blob Storage.

    Args:
        container_name: Target Blob container name
        blob_name: Full blob path (e.g. "uploads/invoice.pdf")

    Returns:
        SAS URL string, or None on failure
    """
    try:
        connection_string = CONFIG["blob"]["connection_string"]
        expiry_minutes = CONFIG["blob"]["sas_expiry_minutes"]

        blob_service_client = BlobServiceClient.from_connection_string(connection_string)

        # Parse account name and key from connection string
        account_name = blob_service_client.account_name
        account_key = blob_service_client.credential.account_key

        expiry_time = datetime.now(timezone.utc) + timedelta(minutes=expiry_minutes)
        start_time = datetime.now(timezone.utc) - timedelta(minutes=5)  # small clock skew buffer

        sas_token = generate_blob_sas(
            account_name=account_name,
            container_name=container_name,
            blob_name=blob_name,
            account_key=account_key,
            permission=BlobSasPermissions(write=True, create=True),
            expiry=expiry_time,
            start=start_time,
        )

        blob_url = (
            f"https://{account_name}.blob.core.windows.net/"
            f"{container_name}/{blob_name}?{sas_token}"
        )

        logger.info(f"Generated SAS URL for blob: {blob_name} (expires in {expiry_minutes} min)")
        return blob_url

    except Exception as e:
        logger.error(f"Error generating SAS URL: {e}")
        return None


def handle_sas_url_request(event: dict) -> dict:
    """
    Handle a SAS URL request.

    Expected event body:
      {
        "method": "presignedurl",
        "DocumentID": "invoice.pdf"
      }

    Returns:
      {
        "statusCode": 200,
        "body": { "sasUrl": "https://...", "blobName": "uploads/invoice.pdf" }
      }
    """
    try:
        document_id = event.get("DocumentID")
        if not document_id:
            return {
                "statusCode": 400,
                "body": {"error": "DocumentID is required"}
            }

        container_name = CONFIG["blob"]["container_name"]
        upload_folder = CONFIG["blob"]["upload_folder"]
        allowed_types = CONFIG["blob"]["allowed_file_types"]

        # Validate / enforce extension
        _, ext = os.path.splitext(document_id)
        ext = ext.lower()

        if not ext:
            document_id = f"{document_id}.pdf"
            ext = ".pdf"
        elif ext not in allowed_types:
            return {
                "statusCode": 400,
                "body": {
                    "error": f"Invalid file type '{ext}'. Allowed: {', '.join(allowed_types)}"
                }
            }

        blob_name = f"{upload_folder}/{document_id}"

        sas_url = generate_sas_upload_url(container_name, blob_name)
        if not sas_url:
            return {
                "statusCode": 500,
                "body": {"error": "Failed to generate SAS URL"}
            }

        return {
            "statusCode": 200,
            "body": {
                "sasUrl": sas_url,
                "blobName": blob_name,
                "containerName": container_name,
                "expiresInMinutes": CONFIG["blob"]["sas_expiry_minutes"],
            }
        }

    except Exception as e:
        logger.error(f"Error handling SAS URL request: {e}")
        return {
            "statusCode": 500,
            "body": {"error": f"Unexpected error: {str(e)}"}
        }
