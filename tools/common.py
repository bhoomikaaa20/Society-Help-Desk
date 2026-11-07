"""Shared constants, errors, validators and formatting for Society Desk.

Conventions used across the backend:
  * Money is parsed with Decimal and stored as integer euro cents.
  * Timestamps are stored as ISO-8601 UTC strings, e.g. 2026-11-06T10:00:00+00:00.
    One fixed format means SQL string comparison is also chronological.
"""

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

CATEGORIES = (
    "Plumbing",
    "Electrical",
    "Lift",
    "Security",
    "Cleaning",
    "Painting",
    "General Maintenance",
)

# Severity -> resolution target.
SLA_HOURS = {"Urgent": 4, "Normal": 48, "Low": 168}

STATUS_OPEN = "OPEN"
STATUS_WORK_ORDER = "WORK ORDER CREATED"
STATUS_IN_PROGRESS = "IN PROGRESS"
STATUS_VENDOR_DONE = "VENDOR-DONE"
STATUS_CLOSED = "CLOSED"
STATUSES = (
    STATUS_OPEN,
    STATUS_WORK_ORDER,
    STATUS_IN_PROGRESS,
    STATUS_VENDOR_DONE,
    STATUS_CLOSED,
)

CONFIRMATION_NOT_REQUESTED = "NOT REQUESTED"
CONFIRMATION_PENDING = "PENDING"
CONFIRMATION_CONFIRMED = "CONFIRMED"
CONFIRMATION_DECLINED = "DECLINED"

# flat_number value for complaints about shared areas, e.g. a block's lift.
COMMON_AREA = "COMMON"

MAX_DESCRIPTION_LENGTH = 2000
MAX_NOTE_LENGTH = 1000
MAX_AMOUNT_CENTS = 100_000_000  # EUR 1,000,000.00 sanity ceiling


class DeskError(Exception):
    """Base class for expected errors. exit_code becomes the process exit code."""

    exit_code = 1
    prefix = "ERROR"


class RuleViolation(DeskError):
    """Valid input, but a business rule refused it."""

    exit_code = 1
    prefix = "REJECTED"


class ValidationError(DeskError):
    """Missing or malformed input."""

    exit_code = 2
    prefix = "INVALID INPUT"


class NotFoundError(DeskError):
    """A referenced record does not exist."""

    exit_code = 3
    prefix = "NOT FOUND"


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------

def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(moment):
    """Serialise a datetime as UTC. Naive datetimes are taken to be UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(text):
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def calculate_due_at(created_at, severity):
    return created_at + timedelta(hours=SLA_HOURS[severity])


def format_duration(delta):
    minutes = int(delta.total_seconds() // 60)
    days, remainder = divmod(minutes, 24 * 60)
    hours, mins = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h {mins}m"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def friendly_duration(delta):
    """Plain words for residents, such as 'about 3 hours' or 'about 2 days'."""
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return "less than 1 hour"
    if hours < 24:
        rounded = round(hours)
        return f"about {rounded} hour{'s' if rounded != 1 else ''}"
    rounded = round(hours / 24)
    return f"about {rounded} day{'s' if rounded != 1 else ''}"


def format_time(text):
    """Render a stored timestamp for people, keeping the timezone explicit."""
    return from_iso(text).strftime("%Y-%m-%d %H:%M UTC") if text else "-"


# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------

def parse_amount(value):
    """Convert user input to positive integer cents, or raise ValidationError."""
    try:
        amount = Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation:
        raise ValidationError(f"Amount '{clean_inline(value)}' is not a valid number.") from None
    if not amount.is_finite():
        raise ValidationError("Amount must be a finite number.")
    if amount <= 0:
        raise ValidationError("Amount must be greater than zero (negative or zero amounts are not allowed).")
    if amount > Decimal(MAX_AMOUNT_CENTS) / 100:
        raise ValidationError(f"Amount exceeds the maximum of {format_eur(MAX_AMOUNT_CENTS)}.")
    if amount != amount.quantize(Decimal("0.01")):
        raise ValidationError("Amount cannot have more than two decimal places.")
    return int(amount * 100)


def parse_csv_money(value, field):
    """Convert a CSV money value (zero allowed) to integer cents."""
    try:
        amount = Decimal(str(value).strip() or "0")
    except InvalidOperation:
        raise ValidationError(f"CSV field {field} has an invalid amount: {value!r}") from None
    if not amount.is_finite() or amount < 0:
        raise ValidationError(f"CSV field {field} has an invalid amount: {value!r}")
    return int(amount * 100)


def format_eur(cents):
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}EUR {cents // 100:,}.{cents % 100:02d}"


# --------------------------------------------------------------------------
# Text. Complaint text and notes are untrusted data: they are stored through
# parameterised SQL, printed as data, and never interpreted.
# --------------------------------------------------------------------------

def clean_inline(value):
    """Flatten text to one line and strip control/format characters.

    This stops stored text from forging extra lines of CLI output or sending
    terminal escape sequences.
    """
    cleaned = []
    for char in str(value):
        kind = unicodedata.category(char)
        if kind in ("Zl", "Zp") or char in "\t\r\n":
            cleaned.append(" ")
        elif kind not in ("Cc", "Cf", "Cs", "Co", "Cn"):
            cleaned.append(char)
    return " ".join("".join(cleaned).split())


def clean_text(value, field, max_length):
    text = clean_inline(value) if value is not None else ""
    if not text:
        raise ValidationError(f"{field} is required and cannot be empty.")
    if len(text) > max_length:
        raise ValidationError(f"{field} is too long (maximum {max_length} characters).")
    return text


# --------------------------------------------------------------------------
# Field validators
# --------------------------------------------------------------------------

def _normalised_id(value, pattern, label, example):
    text = str(value).strip().upper() if value is not None else ""
    if not re.fullmatch(pattern, text):
        raise ValidationError(f"Invalid {label} '{clean_inline(value)}'. Expected a format like {example}.")
    return text


def validate_ticket_id(value):
    return _normalised_id(value, r"T-\d{3,}", "ticket ID", "T-014")


def validate_vendor_id(value):
    return _normalised_id(value, r"V-\d{2,}", "vendor ID", "V-02")


def validate_flat(value):
    return _normalised_id(value, r"[A-Z]-\d{1,4}", "flat number", "B-204")


def validate_block(value):
    """Accept 'B' or 'Block B' and return the stored form 'Block B'."""
    text = str(value).strip().upper() if value is not None else ""
    match = re.fullmatch(r"(?:BLOCK\s+)?([A-Z])", text)
    if not match:
        raise ValidationError(f"Invalid block '{clean_inline(value)}'. Expected a format like B or 'Block B'.")
    return f"Block {match.group(1)}"


def block_from_flat(flat):
    return f"Block {flat.split('-')[0]}"


def validate_category(value):
    key = re.sub(r"[\s_-]+", " ", str(value)).strip().lower()
    for category in CATEGORIES:
        if category.lower() == key:
            return category
    raise ValidationError(
        f"Unknown category '{clean_inline(value)}'. Valid categories: {', '.join(CATEGORIES)}."
    )


def validate_severity(value):
    key = str(value).strip().capitalize()
    if key not in SLA_HOURS:
        raise ValidationError(
            f"Unknown severity '{clean_inline(value)}'. Valid severities: {', '.join(SLA_HOURS)}."
        )
    return key


def validate_month(value):
    """Return (start_iso, end_iso) for a YYYY-MM month: start <= t < end."""
    text = str(value).strip()
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", text) or text.startswith("0000"):
        raise ValidationError(f"Invalid month '{clean_inline(value)}'. Expected YYYY-MM, e.g. 2026-11.")
    year, month = int(text[:4]), int(text[5:])
    if year == 9999 and month == 12:
        raise ValidationError(f"Invalid month '{text}'.")
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=timezone.utc)
    return to_iso(start), to_iso(end)
