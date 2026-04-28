"""
Azure OCR inference for Invoice and general document OCR.
Replaces AWS: IDP_Invoice_OCR/src/model_inference.py

Uses Azure OpenAI GPT-4o vision instead of Bedrock (Llama Scout / Nova).
Also replaces format_with_bedrock_model() / create_json.py with Azure OpenAI.
"""

import json
import re
import base64
import logging
import concurrent.futures

from openai import AzureOpenAI
from src.config_loader import CONFIG
from src.document_processing.utils import extract_json_from_string
from src.cosmos_db.store_metadata import store_chat_context

logger = logging.getLogger("azure.idp.ocr_inference")
logger.setLevel(logging.INFO)

_client = AzureOpenAI(
    azure_endpoint=CONFIG["azure_openai"]["endpoint"],
    api_key=CONFIG["azure_openai"]["api_key"],
    api_version=CONFIG["azure_openai"]["api_version"],
)

_DEPLOYMENT = CONFIG["azure_openai"]["vision_deployment"]  # gpt-4o supports vision
_MAX_TOKENS = CONFIG["azure_openai"]["max_tokens"]


def _image_to_data_url(image_data, media_type: str = "image/png") -> str:
    if isinstance(image_data, bytes):
        b64 = base64.b64encode(image_data).decode("utf-8")
    else:
        b64 = image_data
    return f"data:{media_type};base64,{b64}"


def extract_text_from_image_ocr(
    image_data,
    model_id: str,         # kept for API compatibility (ignored; we always use GPT-4o)
    media_type: str = "image/png",
    max_tokens: int = None,
    temperature: float = None,
    custom_prompt: str = None,
) -> str | None:
    """
    Extract text/JSON from a single image using Azure OpenAI GPT-4o vision.
    Replaces the multi-model (Llama Scout / Claude Haiku) branching in AWS version.
    """
    try:
        prompt = custom_prompt or CONFIG["image_prompt"]["system_message"]
        prompt += (
            "\nIMPORTANT: Your response must be in valid JSON format. "
            "Wrap your entire response in <response>YOUR_JSON_HERE</response> tags."
        )

        response = _client.chat.completions.create(
            model=_DEPLOYMENT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": _image_to_data_url(image_data, media_type)},
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            max_tokens=max_tokens or 8192,
            temperature=temperature if temperature is not None else 0,
        )

        return response.choices[0].message.content.strip()

    except Exception as e:
        logger.error(f"OCR image extraction error: {e}")
        return None


def _process_single_image(img_data, idx: int, prompt: str, model_id: str) -> str:
    text = extract_text_from_image_ocr(img_data, model_id, custom_prompt=prompt)
    return text or ""


def process_images_parallel_ocr(
    document_input: list,
    prompt: str,
    model_id: str,
    max_tokens: int = None,
    temperature: float = None,
) -> str:
    """Process all pages in parallel; return concatenated text."""
    logger.info(f"OCR: processing {len(document_input)} images in parallel")

    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = [
            (executor.submit(_process_single_image, img, idx, prompt, model_id), idx)
            for idx, img in enumerate(document_input)
        ]
        results = [
            f.result() for f, _ in sorted(futures, key=lambda x: x[1])
        ]

    return "\n".join(results)


def format_with_openai(extracted_data: str) -> dict:
    """
    Reformat raw extracted invoice data into a flat JSON key-value structure.
    Replaces format_with_bedrock_model() (which used Llama Scout).
    """
    try:
        system_prompt = (
            "Convert the given document data into a simplified flat JSON key-value format. "
            "Rules:\n"
            "1. No nested objects or arrays — all values at the top level.\n"
            "2. No duplicate keys.\n"
            "3. Preserve ALL information — do NOT skip, truncate, or summarise any items.\n"
            "4. If there are multiple terms, entries, or rows, include every single one.\n"
            "5. Use descriptive key names.\n"
            "Respond immediately enclosed in <response></response> tags."
        )

        response = _client.chat.completions.create(
            model=_DEPLOYMENT,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Here is the document data:\n{extracted_data}"},
            ],
            max_tokens=8192,
            temperature=0,
        )

        raw = response.choices[0].message.content.strip()
        formatted = extract_json_from_string(raw)

        return {
            "statusCode": 200,
            "body": {
                "results": [{
                    "extracted_content": [{
                        "ctype": "json",
                        "content": formatted,
                    }]
                }]
            }
        }

    except Exception as e:
        logger.error(f"format_with_openai error: {e}")
        return {"statusCode": 500, "error": str(e)}


def process_document_ocr(
    document_input,
    input_type: str = "image",
    doc_type: str = "invoice",
    user_instruction: str = None,
    file_key: str = None,
) -> dict:
    """
    Main OCR processing function.
    Replaces the OCR Lambda handler logic.

    Args:
        document_input: base64 image list OR text string
        input_type: "image" | "text"
        doc_type: "invoice" | "ocr_all_documents" | other
        user_instruction: optional override prompt
        file_key: blob key (for logging / Cosmos chat context)

    Returns:
        {"statusCode": 200, "body": {"output": <str|dict>}}
    """
    try:
        doc_type_lower = (doc_type or "").lower()

        if doc_type_lower == "ocr_all_documents":
            default_prompt = CONFIG["image_prompt"]["ocr_all_doc_prompt"]
        else:
            default_prompt = CONFIG["image_prompt"]["system_message"]

        extraction_prompt = (
            user_instruction
            if user_instruction and user_instruction.lower() != "none"
            else default_prompt
        )

        if input_type == "image":
            raw_output = process_images_parallel_ocr(
                document_input,
                prompt=extraction_prompt,
                model_id=_DEPLOYMENT,
            )
        else:
            # Text-based extraction
            response = _client.chat.completions.create(
                model=_DEPLOYMENT,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are an expert OCR and data extraction AI. "
                            "Extract all key information from the provided document text."
                        ),
                    },
                    {"role": "user", "content": f"{extraction_prompt}\n\n{document_input}"},
                ],
                max_tokens=_MAX_TOKENS,
                temperature=0,
            )
            raw_output = response.choices[0].message.content.strip()

        # For invoice: reformat to flat JSON
        if doc_type_lower == "invoice":
            fmt = format_with_openai(raw_output)
            final_output = fmt.get("body", {}).get("results", [{}])[0].get(
                "extracted_content", [{}]
            )[0].get("content", raw_output)
        else:
            final_output = extract_json_from_string(raw_output) or raw_output

        # Store chat context in Cosmos DB
        try:
            store_chat_context(
                query=extraction_prompt,
                model=_DEPLOYMENT,
                file_key=file_key or "",
                response=json.dumps(final_output) if isinstance(final_output, dict) else final_output,
            )
        except Exception as ctx_err:
            logger.warning(f"Could not store chat context: {ctx_err}")

        return {
            "statusCode": 200,
            "body": {"output": final_output},
        }

    except Exception as e:
        logger.error(f"process_document_ocr error: {e}")
        return {"statusCode": 500, "body": {"error": str(e)}}
