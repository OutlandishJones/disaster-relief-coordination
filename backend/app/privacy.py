"""PII protection: scrub text, mask values, and make linkable household tokens.

Design:
  * Raw name/phone/address are stored only in the `contacts` table.
  * Everything the LLM or dashboard sees goes through scrub_text() first.
  * household_token() is a KEYED hash (HMAC), so a household can be linked across
    reports without exposing the phone number, and tokens can't be reversed by
    guessing phone numbers unless the key leaks.

KNOWN LIMITS (say these in the presentation):
  * Regexes catch phones and emails reliably, but free-text NAMES can only be
    removed when we already know them (from the form's name field). A name
    mentioned in free text by someone else will slip through.
  * Street addresses in free text are kept on purpose: matching needs location.
    We store the precise address only in `contacts` and use zones for matching.
"""
import hashlib
import hmac
import os
import re
import warnings

# Phone-like runs: optional +, 8+ digits allowing spaces, dashes, dots, parentheses.
PHONE_RE = re.compile(r"(?<![\w])\+?\d[\d\s().-]{6,}\d(?![\w])")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

_DEV_KEY = b"dev-only-key-not-for-real-data"


def _key() -> bytes:
    k = os.environ.get("PII_HMAC_KEY")
    if not k:
        warnings.warn("PII_HMAC_KEY not set; using an insecure dev key (synthetic data only).")
        return _DEV_KEY
    return k.encode()


def digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def mask_phone(phone: str) -> str:
    """'+886 900 000 678' -> '***0678' (last 4 digits only)."""
    d = digits(phone)
    return "***" + d[-4:] if len(d) >= 4 else "***"


def mask_email(email: str) -> str:
    """'ana.lopez@example.com' -> 'a***@example.com'."""
    if "@" not in email:
        return "***"
    user, domain = email.split("@", 1)
    return (user[:1] or "*") + "***@" + domain


def mask_name(name: str) -> str:
    """'Francis Jones' -> 'F. J.'"""
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    return " ".join(p[0].upper() + "." for p in parts) or "***"


def scrub_text(text: str, known_names=None) -> str:
    """Replace phones, emails and any KNOWN names with placeholders."""
    out = EMAIL_RE.sub("[EMAIL]", text or "")
    out = PHONE_RE.sub("[PHONE]", out)
    for name in known_names or []:
        for token in {name, *name.split()}:
            if len(token) >= 2:
                out = re.sub(re.escape(token), "[NAME]", out, flags=re.IGNORECASE)
    return out


def household_token(name=None, phone=None, address=None) -> str:
    """Stable pseudonymous ID for a household, e.g. 'HH-7F3A21BC'.

    Uses the phone number when present, otherwise name + address. Two households
    at the same address with different phones therefore get DIFFERENT tokens, so
    valid separate requests are never merged.
    """
    if phone and digits(phone):
        basis = "p:" + digits(phone)
    else:
        basis = "n:" + (name or "").casefold().strip() + "|a:" + (address or "").casefold().strip()
    mac = hmac.new(_key(), basis.encode(), hashlib.sha256).hexdigest()
    return "HH-" + mac[:8].upper()
