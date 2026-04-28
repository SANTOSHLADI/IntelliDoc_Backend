"""
scripts/create_search_index.py

Run once to create the Azure AI Search index for IDP documents.
Usage:
    python scripts/create_search_index.py
"""

import json
import os
import requests

SEARCH_ENDPOINT = os.environ["AZURE_SEARCH_ENDPOINT"]
SEARCH_API_KEY  = os.environ["AZURE_SEARCH_API_KEY"]
INDEX_NAME      = os.environ.get("AZURE_SEARCH_INDEX", "idp-documents")
API_VERSION     = "2024-02-01"

HEADERS = {
    "Content-Type": "application/json",
    "api-key": SEARCH_API_KEY,
}

INDEX_DEFINITION = {
    "name": INDEX_NAME,
    "fields": [
        {"name": "id",           "type": "Edm.String", "key": True,  "searchable": False, "filterable": True},
        {"name": "documentId",   "type": "Edm.String", "key": False, "searchable": True,  "filterable": True},
        {"name": "documentType", "type": "Edm.String", "key": False, "searchable": True,  "filterable": True, "facetable": True},
        {"name": "fileName",     "type": "Edm.String", "key": False, "searchable": True,  "filterable": True},
        {"name": "content",      "type": "Edm.String", "key": False, "searchable": True,  "filterable": False},
        {"name": "metadata",     "type": "Edm.String", "key": False, "searchable": True,  "filterable": False},
        {"name": "indexedAt",    "type": "Edm.DateTimeOffset", "key": False, "searchable": False, "filterable": True, "sortable": True},
    ],
    "semantic": {
        "configurations": [
            {
                "name": "default",
                "prioritizedFields": {
                    "contentFields": [{"fieldName": "content"}],
                    "keywordsFields": [{"fieldName": "documentType"}, {"fieldName": "fileName"}],
                },
            }
        ]
    },
}


def create_index():
    url = f"{SEARCH_ENDPOINT}/indexes/{INDEX_NAME}?api-version={API_VERSION}"
    response = requests.put(url, headers=HEADERS, json=INDEX_DEFINITION)

    if response.status_code in (200, 201):
        print(f"✅ Index '{INDEX_NAME}' created/updated successfully.")
    else:
        print(f"❌ Failed to create index: {response.status_code}")
        print(response.text)


if __name__ == "__main__":
    create_index()
