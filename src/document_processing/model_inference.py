"""
Azure OpenAI model inference for IDP document extraction.
Replaces AWS: src/model_inference.py  (which used Amazon Bedrock / Anthropic Claude)

Key changes:
  - boto3 Bedrock calls  →  openai AzureOpenAI client
  - Nova image format    →  GPT-4o vision (base64 image in messages)
  - Parallel image processing via ThreadPoolExecutor (unchanged logic)
"""

import json
import re
import base64
import logging
import concurrent.futures

from openai import AzureOpenAI
from src.config_loader import CONFIG

logger = logging.getLogger("azure.idp.inference")
logger.setLevel(logging.INFO)

# ── Azure OpenAI client (shared across calls) ─────────────────────────
_openai_client = AzureOpenAI(
    azure_endpoint=CONFIG["azure_openai"]["endpoint"],
    api_key=CONFIG["azure_openai"]["api_key"],
    api_version=CONFIG["azure_openai"]["api_version"],
)

_DEPLOYMENT      = CONFIG["azure_openai"]["deployment_name"]
_VIS_DEPLOYMENT  = CONFIG["azure_openai"]["vision_deployment"]
_MAX_TOKENS      = CONFIG["azure_openai"]["max_tokens"]
_TEMPERATURE     = CONFIG["azure_openai"]["temperature"]
_TOP_P           = CONFIG["azure_openai"]["top_p"]


# ══════════════════════════════════════════════════════════════════════
# PROMPT BUILDER  (same logic as AWS build_prompt)
# ══════════════════════════════════════════════════════════════════════

def build_prompt(document_type: str, document_text: str = None,
                 input_type: str = "text", user_instruction: str = None) -> str:
    """
    Return the extraction prompt for a given document type.
    Routes through the new per-type prompt registry (prompts.py).
    Falls back to legacy CONFIG prompts for backward compatibility.
    """
    from src.document_processing.prompts import get_prompt, EXTRACTION_PROMPTS

    # Fine-grained type (aadhaar, pan, invoice, marks_card, etc.) — use new registry
    if document_type in EXTRACTION_PROMPTS:
        prompt = get_prompt(document_type, document_text if input_type == "text" else None)
        if user_instruction:
            prompt += f"\n\nAdditional instruction: {user_instruction}"
        return prompt

    # Legacy group types — kept for backward compatibility
    if document_type == "identity_card":
        # Use the aadhaar prompt as the safe default for unclassified identity cards
        return get_prompt("aadhaar", document_text if input_type == "text" else None)
    if document_type == "bank_statement":
        prompt = CONFIG["bank_statement"]["bank_statement_prompts"]
        if document_text:
            prompt += f"\n\nDocument text:\n{document_text}"
        return prompt
    if document_type == "cdsl_report":
        prompt = CONFIG["cdsl_report"]["cdsl_report_prompts"]
        if document_text:
            prompt += f"\n\nDocument text:\n{document_text}"
        return prompt

    # Fallback
    prompt = CONFIG["loan_application"]["loan_application_prompt"]
    if user_instruction:
        prompt += f"\n\nAdditional instruction: {user_instruction}"
    if document_text:
        prompt += f"\n\nDocument text:\n{document_text}"
    return prompt


# ══════════════════════════════════════════════════════════════════════
# SINGLE IMAGE → TEXT  (replaces extract_text_from_image with Bedrock Nova)
# ══════════════════════════════════════════════════════════════════════

def extract_text_from_image(
    image_data,
    media_type: str = "image/png",
    document_type: str = None,
    custom_prompt: str = None,
) -> str | None:
    """
    Send a single base64 image to Azure OpenAI GPT-4o vision and return extracted text.

    Args:
        image_data: bytes or base64 string
        media_type: MIME type of the image
        document_type: Hint for prompt selection
        custom_prompt: Override the default prompt

    Returns:
        Extracted text string, or None on failure
    """
    try:
        # Normalise to base64 string
        if isinstance(image_data, bytes):
            b64 = base64.b64encode(image_data).decode("utf-8")
        else:
            b64 = image_data

        prompt = custom_prompt or CONFIG["image_prompt"]["system_message"]

        data_url = f"data:{media_type};base64,{b64}"

        response = _openai_client.chat.completions.create(
            model=_VIS_DEPLOYMENT,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an expert AI assistant specialised in document OCR "
                        "and data extraction from images, including handwritten content."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": prompt},
                    ],
                },
            ],
            max_tokens=_MAX_TOKENS,
            temperature=_TEMPERATURE,
            top_p=_TOP_P,
        )

        return response.choices[0].message.content.strip()

    except Exception as e:
        logger.error(f"Error extracting text from image: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════
# IDENTITY CARD TYPE DETECTION  (replaces get_type_of_dcoument)
# ══════════════════════════════════════════════════════════════════════

def get_type_of_document(image_data, media_type: str = "image/png") -> str | None:
    """
    Classify an identity document image.
    Returns one of: aadhaar, pan, passport, voter_id, driving_license
    """
    try:
        if isinstance(image_data, bytes):
            b64 = base64.b64encode(image_data).decode("utf-8")
        else:
            b64 = image_data

        prompt = CONFIG["identity_card"]["identity_typeof_card"]
        data_url = f"data:{media_type};base64,{b64}"

        response = _openai_client.chat.completions.create(
            model=_VIS_DEPLOYMENT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            max_tokens=200,
            temperature=0,
        )

        raw = response.choices[0].message.content.strip()
        data = json.loads(raw)
        return data.get("document_type")

    except Exception as e:
        logger.error(f"Document type detection error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════
# PARALLEL IMAGE PROCESSING  (replaces process_images_in_parallel)
# ══════════════════════════════════════════════════════════════════════

def _extract_for_identity(img_data, identity_card_type: str, idx: int) -> str:
    prompt = CONFIG["identity_card"].get(
        identity_card_type, CONFIG["identity_card"]["identity_card_prompts"]
    )
    text = extract_text_from_image(img_data, "image/png", "identity_card", custom_prompt=prompt)
    logger.info(f"Identity image {idx + 1} processed.")
    return f"'''{text}'''"


def _extract_for_bank(img_data, idx: int) -> str:
    prompt = CONFIG["bank_statement"]["bank_statement_images_extraction"]
    text = extract_text_from_image(img_data, "image/png", "bank_statement", custom_prompt=prompt)
    logger.info(f"Bank statement image {idx + 1} processed.")
    return f"'''{text}'''"


def _extract_generic(img_data, idx: int) -> str:
    text = extract_text_from_image(img_data, "image/png")
    logger.info(f"Image {idx + 1} processed.")
    return f"'''{text}'''"


def process_images_in_parallel(document_input: list, document_type: str = None) -> str:
    """
    Process multiple images in parallel using ThreadPoolExecutor.
    document_type can now be a fine-grained type (aadhaar, pan, invoice, marks_card, etc.)
    or a legacy group type (identity_card, bank_statement).
    """
    from src.document_processing.prompts import get_prompt, EXTRACTION_PROMPTS

    logger.info(f"Processing {len(document_input)} images in parallel (type={document_type})...")

    # Resolve per-image prompt
    _ID_TYPES = {"aadhaar", "pan", "passport", "voter_id", "driving_license", "identity_card"}
    _BANK_TYPES = {"bank_statement"}

    if document_type in _ID_TYPES:
        # For fine-grained ID types, use the specific prompt directly (no second classification needed)
        if document_type in EXTRACTION_PROMPTS:
            id_prompt = get_prompt(document_type)
        else:
            # Legacy identity_card group — detect sub-type
            id_type = get_type_of_document(document_input[0], "image/png") or "aadhaar"
            id_prompt = get_prompt(id_type) if id_type in EXTRACTION_PROMPTS else get_prompt("aadhaar")

        def _extract(img, idx):
            text = extract_text_from_image(img, "image/png", custom_prompt=id_prompt)
            return f"'''{text}'''"

    elif document_type in _BANK_TYPES:
        bank_prompt = CONFIG["bank_statement"]["bank_statement_images_extraction"]

        def _extract(img, idx):
            text = extract_text_from_image(img, "image/png", custom_prompt=bank_prompt)
            return f"'''{text}'''"

    elif document_type in EXTRACTION_PROMPTS:
        # Any other fine-grained type (invoice, marks_card, loan_application, etc.)
        type_prompt = get_prompt(document_type)

        def _extract(img, idx):
            text = extract_text_from_image(img, "image/png", custom_prompt=type_prompt)
            return f"'''{text}'''"

    else:
        def _extract(img, idx):
            text = extract_text_from_image(img, "image/png")
            return f"'''{text}'''"

    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = [(executor.submit(_extract, img, idx), idx) for idx, img in enumerate(document_input)]
        extracted = [f.result() for f, _ in sorted(futures, key=lambda x: x[1])]

    return "\n".join(extracted)


# ══════════════════════════════════════════════════════════════════════
# MAIN PROCESSING FUNCTION  (replaces process_document_with_model)
# ══════════════════════════════════════════════════════════════════════

def process_document_with_model(
    document_input,
    document_type: str,
    input_type: str = "text",
    user_instruction: str = None,
) -> dict:
    """
    Process a document and extract structured data using Azure OpenAI.
    Replaces the AWS Bedrock-based process_document_with_model().

    Args:
        document_input: base64 image list OR plain text string
        document_type: e.g. "invoice", "identity_card", "bank_statement"
        input_type: "image" | "text"
        user_instruction: optional extra instruction from the user

    Returns:
        {"statusCode": 200, "res": {...}}  on success
        {"statusCode": 5xx, "error": "..."}  on failure
    """
    try:
        prompt = build_prompt(
            document_type,
            document_text=document_input if input_type == "text" else None,
            input_type=input_type,
            user_instruction=user_instruction,
        )

        if not prompt:
            return {"statusCode": 400, "error": f"Unsupported document type: {document_type}"}

        # ── IMAGE PATH ──────────────────────────────────────────────
        if input_type == "image":
            logger.info(f"Image mode: {len(document_input)} pages for '{document_type}'")
            combined_text = process_images_in_parallel(document_input, document_type)

            if not combined_text:
                return {"statusCode": 500, "error": "No text extracted from images."}

            # Re-process extracted text with text model (all types, including identity cards)
            return process_document_with_model(
                combined_text, document_type, input_type="text",
                user_instruction=user_instruction
            )

        # ── TEXT PATH ───────────────────────────────────────────────
        logger.info(f"Text mode for '{document_type}'")

        response = _openai_client.chat.completions.create(
            model=_DEPLOYMENT,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an expert document analysis AI. "
                        "Extract ONLY the fields specified in the user prompt — nothing more. "
                        "Do NOT include fields from other document types. "
                        "Do NOT include null, N/A, or empty values — omit those fields entirely. "
                        "Return valid JSON wrapped in <response>YOUR_JSON</response> tags."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=_MAX_TOKENS,
            temperature=_TEMPERATURE,
            top_p=_TOP_P,
        )

        extracted_text = response.choices[0].message.content.strip()
        logger.info(f"Model response received for '{document_type}'")

        # Parse <response>...</response>
        match = re.search(r"<response>(.*?)</response>", extracted_text, re.DOTALL)
        if match:
            try:
                extracted_data = json.loads(match.group(1).strip())

                # Post-process loan application (same as AWS version)
                if document_type == "loan_application":
                    if "Fraud Verdict" in extracted_data:
                        fd = extracted_data["Fraud Verdict"]
                        if isinstance(fd.get("Key Issues"), list):
                            fd["Key Issues"] = "; ".join(fd["Key Issues"])
                        if isinstance(fd.get("Verification Steps"), list):
                            fd["Verification Steps"] = "; ".join(fd["Verification Steps"])
                    if "Aadhar Card" in extracted_data:
                        dob = extracted_data["Aadhar Card"].get("DOB on Aadhar", "")
                        if dob.startswith("01-01-"):
                            extracted_data["Aadhar Card"]["DOB on Aadhar"] = dob.split("-")[-1]

                return {"statusCode": 200, "res": extracted_data}

            except json.JSONDecodeError as e:
                logger.error(f"JSON parse error: {e}")
                return {
                    "statusCode": 500,
                    "error": "Invalid JSON in model response",
                    "extracted_text": extracted_text,
                }

        logger.warning("No <response> tags found in model output.")
        return {
            "statusCode": 500,
            "error": "Model response missing <response> tags",
            "extracted_text": extracted_text,
        }

    except Exception as e:
        logger.error(f"process_document_with_model error: {e}")
        return {"statusCode": 500, "error": str(e)}
