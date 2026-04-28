"""
scripts/create_cosmos_containers.py

Run once to create the Cosmos DB database and containers.
Usage:
    python scripts/create_cosmos_containers.py
"""

import os
from azure.cosmos import CosmosClient, exceptions

COSMOS_ENDPOINT = os.environ["COSMOS_DB_ENDPOINT"]
COSMOS_KEY      = os.environ["COSMOS_DB_KEY"]
DB_NAME         = os.environ.get("COSMOS_DB_DATABASE", "idp-database")
DOC_CONTAINER   = os.environ.get("COSMOS_DB_CONTAINER", "documents")
CHAT_CONTAINER  = os.environ.get("COSMOS_CHAT_CONTAINER", "chat-history")


def setup_cosmos():
    client = CosmosClient(url=COSMOS_ENDPOINT, credential=COSMOS_KEY)

    # Create database
    try:
        db = client.create_database(DB_NAME)
        print(f"✅ Database '{DB_NAME}' created.")
    except exceptions.CosmosResourceExistsError:
        db = client.get_database_client(DB_NAME)
        print(f"ℹ️  Database '{DB_NAME}' already exists.")

    # Create documents container (partition key = /PolicyType)
    try:
        db.create_container(
            id=DOC_CONTAINER,
            partition_key={"paths": ["/PolicyType"], "kind": "Hash"},
        )
        print(f"✅ Container '{DOC_CONTAINER}' created.")
    except exceptions.CosmosResourceExistsError:
        print(f"ℹ️  Container '{DOC_CONTAINER}' already exists.")

    # Create chat-history container (partition key = /file_key)
    try:
        db.create_container(
            id=CHAT_CONTAINER,
            partition_key={"paths": ["/file_key"], "kind": "Hash"},
        )
        print(f"✅ Container '{CHAT_CONTAINER}' created.")
    except exceptions.CosmosResourceExistsError:
        print(f"ℹ️  Container '{CHAT_CONTAINER}' already exists.")

    print("\nCosmos DB setup complete.")


if __name__ == "__main__":
    setup_cosmos()
