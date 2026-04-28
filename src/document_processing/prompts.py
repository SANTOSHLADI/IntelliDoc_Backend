"""
Document-specific extraction prompts.

Each prompt defines EXACTLY which fields to extract for that document type.
This prevents cross-contamination (e.g. Aadhaar result never contains invoice fields).

Rules for every prompt:
  1. List only the fields relevant to that document type.
  2. Instruct the model to OMIT fields it cannot find (no null/N/A values).
  3. Wrap output in <response>YOUR_JSON</response> tags.
"""

# ── Canonical type mapping ────────────────────────────────────────────
# Maps fine-grained classifier output → processing group used by model_inference
CANONICAL_TYPE = {
    "aadhaar":          "identity_card",
    "pan":              "identity_card",
    "passport":         "identity_card",
    "voter_id":         "identity_card",
    "driving_license":  "identity_card",
    "invoice":          "invoice",
    "bank_statement":   "bank_statement",
    "loan_application": "loan_application",
    "marks_card":       "marks_card",
    "cdsl_report":      "cdsl_report",
    "unknown":          "unknown",
}

# ── Per-document extraction prompts ──────────────────────────────────
EXTRACTION_PROMPTS = {

    "aadhaar": (
        "You are an AI system that extracts structured data from Aadhaar cards.\n\n"
        "Extract ONLY these fields:\n"
        "  name, gender, date_of_birth, aadhaar_number,\n"
        "  address (as an object with: street, district, state, pin)\n\n"
        "Strict rules:\n"
        "- Output MUST be in English only. Translate any Hindi or regional language text into English.\n"
        "- aadhaar_number must be 12 digits formatted as XXXX XXXX XXXX.\n"
        "- date_of_birth format: DD/MM/YYYY\n"
        "- address: translate all fields to English; omit sub-fields you cannot find.\n"
        "- Omit any field you cannot find — do NOT include null or N/A values.\n"
        "- Do NOT include any other fields (no income, loan_amount, collateral, employment,\n"
        "  fraud_indicators, bank, marks, or any field not listed above).\n"
        "- Do NOT include explanations, warnings, or extra keys.\n"
        "- Return ONLY valid JSON wrapped in <response>YOUR_JSON</response> tags.\n\n"
        "Example output:\n"
        "<response>{\"name\": \"Bhawani Dwivedi\", \"gender\": \"Male\","
        " \"date_of_birth\": \"06/07/2002\", \"aadhaar_number\": \"4906 5637 6032\","
        " \"address\": {\"district\": \"Faridabad\", \"state\": \"Haryana\","
        " \"pin\": \"121003\"}}</response>"
    ),

    "pan": (
        "Extract ONLY these fields from this PAN card:\n"
        "  name, father_name, dob, pan_number\n\n"
        "Rules:\n"
        "- pan_number is exactly 10 characters (e.g. ABCDE1234F)\n"
        "- dob format: DD/MM/YYYY\n"
        "- Output MUST be in English only. Translate any Hindi or regional language text into English.\n"
        "- Omit any field you cannot find — do NOT include null or N/A values.\n"
        "- Do NOT include address, income, bank, or any other fields.\n"
        "- Do NOT include explanations or extra keys.\n\n"
        "Return ONLY valid JSON wrapped in <response>YOUR_JSON</response> tags."
    ),

    "passport": (
        "Extract ONLY these fields from this passport:\n"
        "  surname, given_names, nationality, dob, gender,\n"
        "  passport_number, issue_date, expiry_date, place_of_birth, issuing_authority\n\n"
        "Rules:\n"
        "- Dates format: DD/MM/YYYY\n"
        "- Output MUST be in English only. Translate any non-English text into English.\n"
        "- Omit any field you cannot find — do NOT include null or N/A values.\n"
        "- Do NOT include address, income, bank, or marks fields.\n"
        "- Do NOT include explanations or extra keys.\n\n"
        "Return ONLY valid JSON wrapped in <response>YOUR_JSON</response> tags."
    ),

    "voter_id": (
        "Extract ONLY these fields from this Voter ID / EPIC card:\n"
        "  name, father_or_husband_name, dob, gender, epic_number, address, part_number\n\n"
        "Rules:\n"
        "- Output MUST be in English only. Translate any Hindi or regional language text into English.\n"
        "- Omit any field you cannot find — do NOT include null or N/A values.\n"
        "- Do NOT include income, bank, invoice, or marks fields.\n"
        "- Do NOT include explanations or extra keys.\n\n"
        "Return ONLY valid JSON wrapped in <response>YOUR_JSON</response> tags."
    ),

    "driving_license": (
        "Extract ONLY these fields from this driving licence:\n"
        "  name, dob, dl_number, issue_date, expiry_date, address,\n"
        "  vehicle_classes (list), issuing_rto\n\n"
        "Rules:\n"
        "- vehicle_classes should be a list e.g. [\"LMV\", \"MCWG\"]\n"
        "- Output MUST be in English only. Translate any Hindi or regional language text into English.\n"
        "- Omit any field you cannot find — do NOT include null or N/A values.\n"
        "- Do NOT include income, bank, invoice, or marks fields.\n"
        "- Do NOT include explanations or extra keys.\n\n"
        "Return ONLY valid JSON wrapped in <response>YOUR_JSON</response> tags."
    ),

    "invoice": (
        "Extract ONLY these fields from this invoice/bill/receipt:\n"
        "  invoice_number, invoice_date, due_date,\n"
        "  vendor_name, vendor_address, vendor_gstin,\n"
        "  customer_name, customer_address, customer_gstin,\n"
        "  line_items (list of {description, quantity, unit_price, amount}),\n"
        "  subtotal, tax_amount, tax_rate, total_amount, currency, payment_terms\n\n"
        "Rules:\n"
        "- line_items must be a list; omit if no line items found\n"
        "- All amounts as numbers (no currency symbols inside values)\n"
        "- Omit any field you cannot find\n"
        "- Do NOT include Aadhaar, PAN, bank account, or personal ID fields\n\n"
        "Return JSON wrapped in <response>YOUR_JSON</response> tags."
    ),

    "bank_statement": (
        "Extract ONLY these fields from this bank statement:\n"
        "  account_holder_name, account_number, bank_name, branch,\n"
        "  ifsc_code, statement_period_from, statement_period_to,\n"
        "  opening_balance, closing_balance, total_credits, total_debits,\n"
        "  transactions (list of {date, description, debit, credit, balance})\n\n"
        "Rules:\n"
        "- transactions must be a list; include all rows found\n"
        "- All amounts as numbers\n"
        "- Omit any field you cannot find\n"
        "- Do NOT include Aadhaar, PAN, invoice, or marks fields\n\n"
        "Return JSON wrapped in <response>YOUR_JSON</response> tags."
    ),

    "loan_application": (
        "Extract ONLY these fields from this loan application:\n"
        "  applicant_name, dob, gender, mobile, email, address,\n"
        "  loan_type, loan_amount_requested, loan_tenure_months,\n"
        "  purpose_of_loan, employment_type, employer_name,\n"
        "  monthly_income, existing_loans, collateral_details,\n"
        "  co_applicant_name, guarantor_name,\n"
        "  fraud_indicators (list of any suspicious observations)\n\n"
        "Rules:\n"
        "- fraud_indicators: list strings; omit key if none found\n"
        "- All amounts as numbers\n"
        "- Omit any field you cannot find\n"
        "- Do NOT include invoice line items, marks, or CDSL fields\n\n"
        "Return JSON wrapped in <response>YOUR_JSON</response> tags."
    ),

    "marks_card": (
        "Extract ONLY these fields from this academic transcript / mark sheet:\n"
        "  student_name, registration_number, programme,\n"
        "  institution_name, mode, date_of_registration, cgpa, equivalent_percentage,\n"
        "  terms (list of objects, one per term, each with:\n"
        "    term_number, tgpa, equivalent_percentage,\n"
        "    subjects (list of {sno, course_code_and_name, credit, grade})\n"
        "  )\n\n"
        "Rules:\n"
        "- terms must be a list containing ALL terms found across ALL pages — do NOT skip any term.\n"
        "- subjects inside each term must include every single course row — do NOT skip any.\n"
        "- Omit any field you cannot find.\n"
        "- Do NOT include bank, income, invoice, or ID card fields.\n\n"
        "Return JSON wrapped in <response>YOUR_JSON</response> tags."
    ),

    "cdsl_report": (
        "Extract ONLY these fields from this CDSL demat / holdings report:\n"
        "  bo_id, client_name, dp_name, report_date,\n"
        "  holdings (list of {isin, company_name, quantity, market_price, market_value}),\n"
        "  total_portfolio_value\n\n"
        "Rules:\n"
        "- holdings must be a list\n"
        "- All amounts as numbers\n"
        "- Omit any field you cannot find\n"
        "- Do NOT include bank transactions, invoice, or personal ID fields\n\n"
        "Return JSON wrapped in <response>YOUR_JSON</response> tags."
    ),

    "unknown": (
        "Extract all key information from this document.\n"
        "Return a flat JSON object with descriptive key names.\n"
        "Omit any field that is empty or not found.\n\n"
        "Return JSON wrapped in <response>YOUR_JSON</response> tags."
    ),
}


def get_prompt(doc_type: str, document_text: str = None) -> str:
    """
    Return the extraction prompt for a given document type.
    Appends document text for text-mode processing.
    """
    prompt = EXTRACTION_PROMPTS.get(doc_type, EXTRACTION_PROMPTS["unknown"])
    if document_text:
        prompt += f"\n\nDocument text:\n{document_text}"
    return prompt


def get_canonical_type(doc_type: str) -> str:
    """Map fine-grained type (e.g. 'aadhaar') to processing group (e.g. 'identity_card')."""
    return CANONICAL_TYPE.get(doc_type, "unknown")
