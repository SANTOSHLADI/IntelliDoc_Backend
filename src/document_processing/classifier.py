"""
Document classifier — auto-detects document type from content.

Called AFTER content extraction (text or images are already in memory),
BEFORE model inference. Classification result drives prompt selection.

Supported types:
    aadhaar | pan | passport | voter_id | driving_license |
    invoice | bank_statement | loan_application | marks_card |
    cdsl_report | unknown
"""

import json
import re
import logging

from openai import AzureOpenAI
from src.config_loader import CONFIG

logger = logging.getLogger("azure.idp.classifier")

_client = AzureOpenAI(
    azure_endpoint=CONFIG["azure_openai"]["endpoint"],
    api_key=CONFIG["azure_openai"]["api_key"],
    api_version=CONFIG["azure_openai"]["api_version"],
)
_DEPLOYMENT = CONFIG["azure_openai"]["vision_deployment"]

# ── Classification prompt ─────────────────────────────────────────────
_CLASSIFY_PROMPT = """You are a document classification expert.

Examine the document and return ONLY a JSON object with this exact structure:
{
  "document_type": "<type>",
  "confidence": <0.0-1.0>,
  "reason": "<one sentence>"
}

Allowed document_type values (pick exactly one):
- "aadhaar"           : Indian Aadhaar card (12-digit UID, UIDAI logo)
- "pan"               : Indian PAN card (10-char alphanumeric, Income Tax dept)
- "passport"          : Any country passport (MRZ lines, country code)
- "voter_id"          : Indian Voter ID / EPIC card
- "driving_license"   : Driving licence (any state/country)
- "invoice"           : Commercial invoice, tax invoice, bill, receipt, GST invoice
- "bank_statement"    : Bank account statement with transactions
- "loan_application"  : Loan application form, credit application
- "marks_card"        : Academic mark sheet, grade card, result sheet, transcript
- "cdsl_report"       : CDSL demat / portfolio holdings report
- "unknown"           : Cannot determine with confidence

Return ONLY the JSON. No explanation outside the JSON."""


def classify_document(file_content, input_type: str) -> str:
    """
    Classify a document from its already-extracted content.

    Args:
        file_content : str (text) or list[str] (base64 images)
        input_type   : "text" | "image"

    Returns:
        document_type string, e.g. "aadhaar", "invoice", "unknown"
    """
    try:
        if input_type == "image":
            return _classify_from_image(file_content[0])   # first page is enough
        else:
            return _classify_from_text(file_content)
    except Exception as e:
        logger.error(f"Classification failed: {e}")
        return "unknown"


def _classify_from_image(b64_image: str) -> str:
    data_url = f"data:image/png;base64,{b64_image}"
    response = _client.chat.completions.create(
        model=_DEPLOYMENT,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": _CLASSIFY_PROMPT},
            ],
        }],
        max_tokens=150,
        temperature=0,
    )
    return _parse_classification(response.choices[0].message.content)


def _classify_from_text(text: str) -> str:
    # Send only first 3000 chars — enough for classification, saves tokens
    snippet = text[:3000]
    response = _client.chat.completions.create(
        model=_DEPLOYMENT,
        messages=[
            {"role": "system", "content": _CLASSIFY_PROMPT},
            {"role": "user", "content": f"Document text:\n{snippet}"},
        ],
        max_tokens=150,
        temperature=0,
    )
    return _parse_classification(response.choices[0].message.content)


def _parse_classification(raw: str) -> str:
    try:
        # Strip markdown fences if present
        clean = re.sub(r"```(?:json)?|```", "", raw).strip()
        data = json.loads(clean)
        doc_type = data.get("document_type", "unknown")
        confidence = float(data.get("confidence", 0))
        logger.info(f"Classified as '{doc_type}' (confidence={confidence:.2f}): {data.get('reason','')}")
        # Treat low-confidence as unknown so fallback prompt is used
        return doc_type if confidence >= 0.6 else "unknown"
    except Exception as e:
        logger.warning(f"Could not parse classification response: {e} | raw={raw[:200]}")
        return "unknown"
