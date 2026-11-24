"""Shared text, date and identity utilities.

This module carries the messy-real-world knowledge the extraction and matching
agents rely on: Indian bank narration conventions, ambiguous date formats,
common OCR confusions, and cautious name matching.

Nothing here ever *decides* a financial fact. It produces candidates and
confidence signals; the agents decide, and record why.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Month recognition (English, Hindi, abbreviations, common misspellings)
# ---------------------------------------------------------------------------
MONTH_LOOKUP: dict[str, int] = {}


def _register_months() -> None:
    english = [
        ("january", "jan", "janaury", "janvary"),
        ("february", "feb", "febuary", "febrary"),
        ("march", "mar", "marcch"),
        ("april", "apr", "apirl"),
        ("may",),
        ("june", "jun"),
        ("july", "jul"),
        ("august", "aug", "agust"),
        ("september", "sep", "sept", "septmber"),
        ("october", "oct", "octber"),
        ("november", "nov", "novmber"),
        ("december", "dec", "decmber"),
    ]
    for idx, names in enumerate(english, start=1):
        for name in names:
            MONTH_LOOKUP[name] = idx

    hindi = [
        ("जनवरी",), ("फरवरी", "फ़रवरी"), ("मार्च",), ("अप्रैल", "अप्रेल"),
        ("मई",), ("जून",), ("जुलाई",), ("अगस्त",),
        ("सितम्बर", "सितंबर"), ("अक्टूबर", "अक्तूबर"),
        ("नवम्बर", "नवंबर"), ("दिसम्बर", "दिसंबर"),
    ]
    for idx, names in enumerate(hindi, start=1):
        for name in names:
            MONTH_LOOKUP[name] = idx


_register_months()

#: Words that indicate the narration is naming a salary period.
SALARY_WORDS = (
    "salary", "sal", "honorarium", "honararium", "hon", "wages", "pay",
    "वेतन", "मानदेय", "भुगतान",
)
ARREAR_WORDS = ("arrear", "arrears", "arrer", "बकाया", "एरियर")
INCENTIVE_WORDS = ("incentive", "prot", "protsahan", "प्रोत्साहन")
REVERSAL_WORDS = ("reversal", "reversed", "return", "returned", "failed",
                  "rejected", "refund of", "chargeback", "rtn")


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
def normalize_text(value: Optional[str]) -> str:
    """Collapse whitespace, unify case, strip control characters.

    Multi-line bank narrations are joined into one line so that patterns which
    straddle a line break still match.
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def normalize_narration(value: Optional[str]) -> str:
    """Lower-cased narration with punctuation reduced to single spaces."""
    text = normalize_text(value).lower()
    text = re.sub(r"[^\w\u0900-\u097f/\-:.]+", " ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def normalize_name(name: Optional[str]) -> str:
    """Upper-case, punctuation-free, space-collapsed name for candidate matching.

    Used ONLY to find candidates. Two people are never merged on this basis
    alone (spec s23).
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", str(name))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^A-Za-z\u0900-\u097f\s]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip().upper()


#: Honorifics that carry no identity information.
_HONORIFICS = {"MR", "MRS", "MS", "MISS", "SMT", "SHRI", "SRI", "KUM",
               "DR", "PROF", "M/S", "S/O", "D/O", "W/O"}


def name_tokens(name: Optional[str]) -> list[str]:
    return [tok for tok in normalize_name(name).split() if tok and tok not in _HONORIFICS]


def name_similarity(a: Optional[str], b: Optional[str]) -> float:
    """Cautious 0-1 name similarity.

    Handles the cases the specification names (s23): case differences, joined
    words ("Rajkumar" vs "Raj Kumar"), and initials ("R. Kumar" vs "Raj Kumar").
    Deliberately conservative -- a high score is a *hint*, never an identity
    decision.
    """
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0

    ja, jb = "".join(ta), "".join(tb)
    if ja == jb:
        return 1.0

    # Initial-compatible comparison: "R KUMAR" vs "RAJ KUMAR".
    if len(ta) == len(tb):
        compatible = True
        exact_parts = 0
        for x, y in zip(ta, tb):
            if x == y:
                exact_parts += 1
            elif (len(x) == 1 and y.startswith(x)) or (len(y) == 1 and x.startswith(y)):
                continue
            else:
                compatible = False
                break
        if compatible:
            # Full-token agreement scores higher than initial agreement.
            return 0.95 if exact_parts == len(ta) else 0.85

    # Token-set overlap (handles reordering and middle names).
    sa, sb = set(ta), set(tb)
    overlap = len(sa & sb) / max(len(sa), len(sb))

    # Character-level similarity on the joined form catches "RAJKUMAR".
    seq = SequenceMatcher(None, ja, jb).ratio()

    return round(max(overlap, seq * 0.9), 4)


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
_DATE_PATTERNS = (
    ("%d/%m/%Y", r"^\d{1,2}/\d{1,2}/\d{4}$"),
    ("%d-%m-%Y", r"^\d{1,2}-\d{1,2}-\d{4}$"),
    ("%d.%m.%Y", r"^\d{1,2}\.\d{1,2}\.\d{4}$"),
    ("%d/%m/%y", r"^\d{1,2}/\d{1,2}/\d{2}$"),
    ("%d-%m-%y", r"^\d{1,2}-\d{1,2}-\d{2}$"),
    ("%Y-%m-%d", r"^\d{4}-\d{1,2}-\d{1,2}$"),
    ("%Y/%m/%d", r"^\d{4}/\d{1,2}/\d{1,2}$"),
    # Both the abbreviated (%b, "Apr") and full (%B, "April") month names are
    # tried, because statements and orders use each interchangeably.
    ("%d-%b-%Y", r"^\d{1,2}-[A-Za-z]{3,9}-\d{4}$"),
    ("%d-%B-%Y", r"^\d{1,2}-[A-Za-z]{3,9}-\d{4}$"),
    ("%d %b %Y", r"^\d{1,2} [A-Za-z]{3,9} \d{4}$"),
    ("%d %B %Y", r"^\d{1,2} [A-Za-z]{3,9} \d{4}$"),
    ("%d-%b-%y", r"^\d{1,2}-[A-Za-z]{3,9}-\d{2}$"),
    ("%d-%B-%y", r"^\d{1,2}-[A-Za-z]{3,9}-\d{2}$"),
    ("%d %b %y", r"^\d{1,2} [A-Za-z]{3,9} \d{2}$"),
    ("%d %B %y", r"^\d{1,2} [A-Za-z]{3,9} \d{2}$"),
    ("%b %d, %Y", r"^[A-Za-z]{3,9} \d{1,2}, \d{4}$"),
    ("%B %d, %Y", r"^[A-Za-z]{3,9} \d{1,2}, \d{4}$"),
    ("%d%b%Y", r"^\d{1,2}[A-Za-z]{3}\d{4}$"),
)


class AmbiguousDate(ValueError):
    """Raised when a date string could mean two different real dates."""


def parse_date(raw: Optional[str], dayfirst: bool = True) -> Optional[date]:
    """Parse a date from a bank statement or document.

    Indian statements are overwhelmingly day-first, which is the default. A
    value like ``03/04/2026`` is therefore 3 April, not 4 March. Where the day
    exceeds 12 the format is unambiguous and detected automatically.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw

    text = normalize_text(str(raw))
    if not text:
        return None
    # Strip a trailing time component.
    text = re.sub(r"\s+\d{1,2}:\d{2}(:\d{2})?(\s*[APap][Mm])?$", "", text).strip()

    for fmt, pattern in _DATE_PATTERNS:
        if not re.match(pattern, text):
            continue
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            # e.g. 31/02 -- try the swapped interpretation before giving up.
            continue
        if not dayfirst and fmt.startswith("%d/") :
            try:
                swapped = datetime.strptime(text, fmt.replace("%d/%m", "%m/%d")).date()
                return swapped
            except ValueError:
                pass
        return parsed

    # Fallback: dd/mm/yyyy with unusual separators.
    match = re.match(r"^(\d{1,2})[^\d](\d{1,2})[^\d](\d{2,4})$", text)
    if match:
        a, b, c = (int(g) for g in match.groups())
        year = c if c > 99 else (2000 + c if c < 70 else 1900 + c)
        day, month = (a, b) if dayfirst else (b, a)
        if a > 12 >= b:
            day, month = a, b
        elif b > 12 >= a:
            day, month = b, a
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def month_end(year: int, month: int) -> date:
    """Last calendar day of a month, leap years handled (spec s94)."""
    if month == 12:
        return date(year, 12, 31)
    next_month = date(year, month + 1, 1)
    return date(next_month.year, next_month.month, 1).replace(day=1) - _one_day()


def _one_day():
    from datetime import timedelta
    return timedelta(days=1)


def days_in_month(year: int, month: int) -> int:
    return month_end(year, month).day


def add_months(anchor: date, months: int) -> date:
    """Shift a date by whole months, clamping the day to the target month."""
    total = anchor.month - 1 + months
    year = anchor.year + total // 12
    month = total % 12 + 1
    day = min(anchor.day, days_in_month(year, month))
    return date(year, month, day)


def financial_year(d: date) -> str:
    """Indian financial year label for a date (April to March) -- spec s94."""
    if d.month >= 4:
        return f"{d.year}-{str(d.year + 1)[2:]}"
    return f"{d.year - 1}-{str(d.year)[2:]}"


# ---------------------------------------------------------------------------
# Narration mining
# ---------------------------------------------------------------------------
#: A token that carries its own label, e.g. ``UTRN123456789012``. Matched
#: first so the label is not sheared off the front of the reference.
_UTR_SELF_LABELLED = re.compile(r"\b(UTR[A-Z0-9]{6,19})\b", re.I)
#: An explicit UTR/RRN label followed by a separator is the strongest signal.
_UTR_LABELLED = re.compile(
    r"(?:utr(?:\s*no)?|rrn|txn\s*id|transaction\s*id|ref(?:erence)?(?:\s*no)?)"
    r"[\s:\-/#]+([A-Z0-9]{8,22})", re.I)
#: A transfer-mode keyword is weaker: the token right after it is often the
#: originating IFSC rather than the UTR.
_UTR_AFTER_MODE = re.compile(r"(?:neft|imps|rtgs|ach|upi)[\s:\-/]*([A-Z0-9]{8,22})", re.I)
_UTR_BARE = re.compile(r"\b([A-Z]{2,6}[A-Z0-9]{8,18})\b")

#: Indian IFSC: 4 letters, then '0', then 6 alphanumerics. An IFSC identifies a
#: BRANCH, not a payment, and must never be recorded as a UTR.
_IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


def is_ifsc(token: Optional[str]) -> bool:
    return bool(token) and bool(_IFSC_RE.match(token.strip().upper()))


def extract_utr(narration: Optional[str]) -> Optional[str]:
    """Pull a UTR / reference number out of a narration.

    Returns ``None`` rather than guessing when nothing matches -- a fabricated
    UTR would be a fabricated financial fact (spec s52). IFSC codes are
    explicitly excluded: they identify a branch, not a transaction.
    """
    text = normalize_text(narration)
    if not text:
        return None

    for pattern in (_UTR_SELF_LABELLED, _UTR_LABELLED, _UTR_AFTER_MODE):
        for match in pattern.finditer(text):
            token = match.group(1).upper()
            if not is_ifsc(token):
                return token

    for match in _UTR_BARE.finditer(text.upper()):
        token = match.group(1)
        # An unlabelled token is only a reference if it actually carries
        # digits; otherwise ordinary words like "WITHDRAWAL" would be
        # recorded as transaction references.
        if not is_ifsc(token) and any(ch.isdigit() for ch in token):
            return token
    return None


def extract_reference_numbers(narration: Optional[str]) -> list[str]:
    text = normalize_text(narration).upper()
    return list(dict.fromkeys(re.findall(r"\b[A-Z0-9]{6,22}\b", text)))


#: Explicit "MAR-2026", "MARCH 2026", "03/2026", "SAL MAR26" forms.
_MONTH_YEAR_PATTERNS = [
    re.compile(r"\b([A-Za-z\u0900-\u097f]{3,12})[\s\-/,]*((?:19|20)\d{2})\b"),
    re.compile(r"\b([A-Za-z\u0900-\u097f]{3,12})[\s\-/]?(\d{2})\b"),
    re.compile(r"\b(0?[1-9]|1[0-2])[/\-]((?:19|20)\d{2})\b"),
]


def extract_month_references(narration: Optional[str]) -> list[tuple[int, Optional[int], str]]:
    """Find explicit salary-month references in a narration.

    Returns a list of ``(month, year_or_None, matched_text)``. The month named
    in a narration is the SALARY month, which is frequently not the month the
    credit landed (spec s8).
    """
    text = normalize_narration(narration)
    if not text:
        return []

    found: list[tuple[int, Optional[int], str]] = []
    seen: set[tuple[int, Optional[int]]] = set()

    # Any standalone 4-digit year in the narration, used as a fallback when a
    # month name is not directly followed by its year ("MARCH HONORARIUM 2026").
    loose_years = re.findall(r"\b((?:19|20)\d{2})\b", text)
    loose_year = int(loose_years[0]) if len(set(loose_years)) == 1 else None

    # Named month, optionally glued to a 2- or 4-digit year ("mar26", "mar-26").
    for match in re.finditer(r"\b([a-z\u0900-\u097f]{3,12})[\s\-/,']?(\d{2,4})?\b", text):
        word = match.group(1)
        month = MONTH_LOOKUP.get(word)
        if not month:
            continue

        year = None
        glued = match.group(2)
        if glued:
            value = int(glued)
            if len(glued) == 4 and 1900 <= value <= 2100:
                year = value
            elif len(glued) == 2:
                year = 2000 + value if value < 70 else 1900 + value
        if year is None:
            tail = text[match.end(): match.end() + 8]
            ym = re.match(r"[\s\-/,']*((?:19|20)\d{2})\b", tail)
            if ym:
                year = int(ym.group(1))
        if year is None:
            year = loose_year

        key = (month, year)
        if key not in seen:
            seen.add(key)
            found.append((month, year, match.group(0)))

    # Numeric MM/YYYY -- only when a salary word is present, otherwise a
    # date fragment would be misread as a salary month.
    if any(w in text for w in SALARY_WORDS):
        for match in re.finditer(r"\b(0?[1-9]|1[0-2])[/\-]((?:19|20)\d{2})\b", text):
            month, year = int(match.group(1)), int(match.group(2))
            key = (month, year)
            if key not in seen:
                seen.add(key)
                found.append((month, year, match.group(0)))

    return found


def contains_any(text: Optional[str], words: Iterable[str]) -> bool:
    normalized = normalize_narration(text)
    return any(w in normalized for w in words)


def looks_like_reversal(narration: Optional[str]) -> bool:
    return contains_any(narration, REVERSAL_WORDS)


def looks_like_arrear(narration: Optional[str]) -> bool:
    return contains_any(narration, ARREAR_WORDS)


def looks_like_salary(narration: Optional[str]) -> bool:
    return contains_any(narration, SALARY_WORDS)


#: Government / treasury sender markers seen in Indian statements.
GOVERNMENT_MARKERS = (
    "govt", "government", "gov of", "treasury", "pfms", "dbt", "sarkar",
    "nic", "cmp", "deptt", "dept of", "directorate", "icds", "wcd",
    "commissioner", "collector", "panchayat", "nagar nigam", "municipal",
    "सरकार", "कोषागार", "निदेशालय",
)


def looks_government(narration: Optional[str]) -> bool:
    """Whether a narration carries government markers.

    IMPORTANT: this is one weak signal among many. The specification forbids
    classifying a payment from the word "GOVT" alone (spec s7).
    """
    return contains_any(narration, GOVERNMENT_MARKERS)


def extract_counterparty(narration: Optional[str]) -> Optional[str]:
    """Best-effort sender extraction from an Indian NEFT/IMPS narration.

    Typical: ``NEFT-SBIN0001234-DIRECTORATE OF ICDS-UTR123456``
    """
    text = normalize_text(narration)
    if not text:
        return None
    parts = [p.strip() for p in re.split(r"[-/|]{1,2}", text) if p.strip()]
    candidates = [
        p for p in parts
        if len(p) >= 5
        and not re.fullmatch(r"[A-Z0-9]{6,22}", p.upper())
        and not re.fullmatch(r"[\d.,]+", p)
        and not re.match(r"^(neft|imps|rtgs|upi|ach|by|to|cr|dr)$", p, re.I)
    ]
    if not candidates:
        return None
    # The longest alphabetic fragment is usually the institution name.
    best = max(candidates, key=lambda p: sum(ch.isalpha() for ch in p))
    return best[:200] if sum(ch.isalpha() for ch in best) >= 4 else None


# ---------------------------------------------------------------------------
# OCR repair
# ---------------------------------------------------------------------------
#: Substitutions applied ONLY inside strings already known to be numeric.
_OCR_DIGIT_FIXES = {"O": "0", "o": "0", "I": "1", "l": "1", "|": "1",
                    "S": "5", "B": "8", "Z": "2", "g": "9"}


#: What a repaired amount must look like before we accept the repair.
_NUMERIC_SHAPE = re.compile(r"^[+-]?[\d,]+(\.\d+)?$")


def repair_numeric_ocr(text: Optional[str]) -> Optional[str]:
    """Repair letters that OCR produced inside an otherwise numeric field.

    The repair is accepted ONLY if the substituted result is a well-formed
    number (``1O,50O.OO`` -> ``10,500.00``). If it is not, the original text
    is returned untouched, so genuine words are never mangled into digits.
    """
    if not text:
        return text
    stripped = str(text).strip()
    if not stripped or not any(ch.isdigit() or ch in _OCR_DIGIT_FIXES for ch in stripped):
        return text
    if _NUMERIC_SHAPE.match(stripped):
        return stripped  # already clean

    repaired = "".join(_OCR_DIGIT_FIXES.get(ch, ch) for ch in stripped)
    return repaired if _NUMERIC_SHAPE.match(repaired) else text


def clean_amount_cell(text: Optional[str]) -> Optional[str]:
    """Prepare a spreadsheet/PDF cell for amount parsing."""
    if text is None:
        return None
    value = normalize_text(str(text))
    if not value or value in {"-", "--", "—", "NA", "N/A", "nil", "NIL"}:
        return None
    return repair_numeric_ocr(value)


def similarity(a: str, b: str) -> float:
    """Generic 0-1 string similarity."""
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()
