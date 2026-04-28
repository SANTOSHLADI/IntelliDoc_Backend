"""
Document utility functions for Azure IDP.
Replaces AWS: src/utils.py

Key changes:
  - download_file_from_blob()  replaces  download_file_from_s3()
  - All Blob Storage calls use azure-storage-blob SDK
  - PDF / DOCX / image extraction logic is identical to AWS version
"""

import base64
import logging
import os
import json

import fitz          # PyMuPDF
import PyPDF2
from docx import Document
from PIL import Image
from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceNotFoundError

from src.config_loader import CONFIG

logger = logging.getLogger("azure.idp.utils")
logger.setLevel(logging.INFO)

# ── Config shortcuts ──────────────────────────────────────────────────
def _get_max_pages():
    """Read MAX_PAGES dynamically so Azure config changes take effect without redeploy."""
    return int(os.environ.get("MAX_PAGES", CONFIG["document_upload"].get("maximum_pages", 10)))

def _get_max_pages_bank():
    return int(os.environ.get("MAX_PAGES_BANK", CONFIG["document_upload"].get("maximum_pages_bank", 20)))

_MAX_PAGES       = _get_max_pages()
_MAX_PAGES_BANK  = _get_max_pages_bank()
_MAX_IMAGE_MB    = int(CONFIG["document_upload"]["image_size"])
_IMAGE_QUALITY   = int(CONFIG["document_upload"]["image_quality"])
_ZOOM_X          = float(CONFIG["document_upload"]["zoom_x"])
_ZOOM_Y          = float(CONFIG["document_upload"]["zoom_y"])
_QUALITY_DEC     = int(CONFIG["image_compression"]["quality_decrement"])
_MIN_QUALITY     = int(CONFIG["image_compression"]["min_quality"])
_SIZE_MULT       = int(CONFIG["image_compression"]["size_multiplier"])


# ══════════════════════════════════════════════════════════════════════
# BLOB STORAGE  (replaces S3)
# ══════════════════════════════════════════════════════════════════════

def _get_blob_client(blob_name: str):
    """Return a BlobClient for the given blob name."""
    conn_str = CONFIG["blob"]["connection_string"]
    container = CONFIG["blob"]["container_name"]
    service = BlobServiceClient.from_connection_string(conn_str)
    return service.get_blob_client(container=container, blob=blob_name)


def download_file_from_blob(file_key: str) -> str:
    """
    Download a file from Azure Blob Storage to /tmp and return the local path.
    Equivalent to download_file_from_s3().

    Args:
        file_key: Blob path, e.g. "uploads/invoice.pdf"

    Returns:
        Local file path under /tmp/

    Raises:
        FileNotFoundError: if the blob does not exist
    """
    upload_folder = CONFIG["blob"]["upload_folder"]
    if not file_key.startswith(upload_folder):
        file_key = f"{upload_folder}/{file_key}"

    try:
        blob_client = _get_blob_client(file_key)

        # Check existence
        try:
            blob_client.get_blob_properties()
        except ResourceNotFoundError:
            logger.error(f"Blob not found: {file_key}")
            raise FileNotFoundError(f"Blob '{file_key}' not found in container.")

        os.makedirs("/tmp", exist_ok=True)
        local_path = f"/tmp/temp_{os.path.basename(file_key)}"

        with open(local_path, "wb") as f:
            stream = blob_client.download_blob()
            stream.readinto(f)

        logger.info(f"Downloaded blob '{file_key}' → {local_path}")
        return local_path

    except FileNotFoundError:
        raise
    except Exception as e:
        logger.error(f"Error downloading blob '{file_key}': {e}")
        raise


# ══════════════════════════════════════════════════════════════════════
# CONTENT EXTRACTION  (unchanged from AWS version)
# ══════════════════════════════════════════════════════════════════════

def extract_content(file_path: str, document_type: str = None, force_images: bool = False):
    """
    Determine file type and extract text or base64 image list.

    Args:
        file_path: Local path to the file
        document_type: Hint to choose extraction strategy (e.g. "bank_statement")
        force_images: Always extract as images (for OCR workflows)

    Returns:
        str (text) or list[str] (base64 images)
    """
    if force_images:
        return _extract_pdf_images_as_base64(file_path, _MAX_PAGES)

    if file_path.endswith(".pdf"):
        max_p = _MAX_PAGES_BANK if document_type == "bank_statement" else _MAX_PAGES
        return _extract_pdf_content(file_path, max_p)
    elif file_path.endswith(".docx"):
        return _extract_docx_content(file_path)
    elif file_path.lower().endswith((".png", ".jpeg", ".jpg")):
        return _extract_image_content(file_path)
    else:
        raise ValueError(
            f"Unsupported file format: {file_path}. "
            "Only .pdf, .docx, .png, .jpeg, .jpg are supported."
        )


def _extract_pdf_content(pdf_path: str, max_pages: int = _MAX_PAGES):
    # Count actual pages — use fitz as fallback if PyPDF2 fails
    try:
        with open(pdf_path, "rb") as f:
            total_pages = len(PyPDF2.PdfReader(f).pages)
    except Exception:
        try:
            doc = fitz.open(pdf_path)
            total_pages = len(doc)
            doc.close()
        except Exception:
            total_pages = max_pages  # assume full document, extract all allowed pages

    # Always use image extraction for multi-page PDFs to ensure all pages are processed
    if total_pages > 1:
        logger.info(f"Multi-page PDF ({total_pages} pages) — using image extraction for all pages.")
        return _extract_pdf_images_as_base64(pdf_path, min(total_pages, max_pages))

    text = _extract_pdf_text(pdf_path, max_pages)
    if not text:
        logger.info("No text in PDF — falling back to image extraction.")
        return _extract_pdf_images_as_base64(pdf_path, max_pages)
    return text


def _extract_pdf_text(pdf_path: str, max_pages: int = _MAX_PAGES) -> str:
    try:
        with open(pdf_path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            pages_to_read = min(len(reader.pages), max_pages)
            text = "".join(
                (reader.pages[i].extract_text() or "") for i in range(pages_to_read)
            )
        return text.strip()
    except Exception as e:
        logger.error(f"PyPDF2 text extraction error: {e}")
        return ""


def _extract_pdf_images_as_base64(
    pdf_path: str,
    max_pages: int = None,
    max_size_mb: int = _MAX_IMAGE_MB,
    zoom_x: float = _ZOOM_X,
    zoom_y: float = _ZOOM_Y,
) -> list:
    if max_pages is None:
        max_pages = _get_max_pages()
    base64_images = []
    try:
        doc = fitz.open(pdf_path)
        for page_num in range(min(len(doc), max_pages)):
            page = doc.load_page(page_num)
            matrix = fitz.Matrix(zoom_x, zoom_y)
            pix = page.get_pixmap(matrix=matrix)

            image_path = f"/tmp/page_{page_num + 1}.png"
            pix.save(image_path)

            if os.path.getsize(image_path) > max_size_mb * _SIZE_MULT:
                image_path = _compress_image(image_path, max_size_mb)

            b64 = _image_to_base64(image_path)
            if b64:
                base64_images.append(b64)
    except Exception as e:
        logger.error(f"PDF → image extraction error: {e}")
    return base64_images


def _extract_image_content(
    image_path: str,
    zoom_x: float = _ZOOM_X,
    zoom_y: float = _ZOOM_Y,
) -> list:
    try:
        with Image.open(image_path) as img:
            new_size = (int(img.width * zoom_x), int(img.height * zoom_y))
            img = img.resize(new_size, Image.LANCZOS)
            resized_path = f"/tmp/resized_{os.path.basename(image_path)}"
            img.save(resized_path)
        return [_image_to_base64(resized_path)]
    except Exception as e:
        logger.error(f"Image content extraction error: {e}")
        return []


def _extract_docx_content(docx_path: str) -> str:
    try:
        doc = Document(docx_path)
        return "\n".join(para.text for para in doc.paragraphs)
    except Exception as e:
        logger.error(f"DOCX extraction error: {e}")
        return ""


def _compress_image(
    image_path: str,
    max_size_mb: int = _MAX_IMAGE_MB,
    quality: int = _IMAGE_QUALITY,
) -> str:
    try:
        with Image.open(image_path) as img:
            out_path = f"{image_path.rsplit('.', 1)[0]}_compressed.jpg"
            rgb = img.convert("RGB")
            rgb.save(out_path, format="JPEG", quality=quality)

            while os.path.getsize(out_path) > max_size_mb * _SIZE_MULT:
                quality -= _QUALITY_DEC
                if quality < _MIN_QUALITY:
                    break
                rgb.save(out_path, format="JPEG", quality=quality)
            return out_path
    except Exception as e:
        logger.error(f"Image compression error: {e}")
        return image_path


def _image_to_base64(image_path: str) -> str | None:
    try:
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        logger.error(f"base64 conversion error: {e}")
        return None


def extract_json_from_string(text: str) -> dict:
    """
    Extract JSON content from a string that may contain <response>...</response> tags
    or raw JSON.
    """
    import re
    if not text:
        return {}
    
    # Try <response> tags first
    match = re.search(r"<response>(.*?)</response>", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass
    
    # Try raw JSON
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
    except json.JSONDecodeError:
        pass
    
    return {"raw_text": text}