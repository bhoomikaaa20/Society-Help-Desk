"""Ticket creation, lifecycle, SLA tracking and recurring fault detection."""

import math
import re
import statistics
from datetime import timedelta

from common import (
    COMMON_AREA,
    CONFIRMATION_CONFIRMED,
    CONFIRMATION_DECLINED,
    CONFIRMATION_NOT_REQUESTED,
    CONFIRMATION_PENDING,
    MAX_DESCRIPTION_LENGTH,
    MAX_NOTE_LENGTH,
    NotFoundError,
    RuleViolation,
    SLA_HOURS,
    STATUS_CLOSED,
    STATUS_IN_PROGRESS,
    STATUS_OPEN,
    STATUS_VENDOR_DONE,
    STATUS_WORK_ORDER,
    ValidationError,
    block_from_flat,
    calculate_due_at,
    clean_inline,
    clean_text,
    format_duration,
    friendly_duration,
    from_iso,
    to_iso,
    utc_now,
    validate_block,
    validate_category,
    validate_flat,
    validate_severity,
    validate_ticket_id,
)
from database import transaction

ACTION_DECLINED = "WORK_ORDER_DECLINED"
ACTION_DELAY = "DELAY_REASON"
ACTION_REPORTED_BY = "REPORTED_BY"
MAX_REPORTER_LENGTH = 100

PROGRESS_TEXT = {
    STATUS_OPEN: "Logged, and a repair is being arranged",
    STATUS_WORK_ORDER: "Repair arranged",
    STATUS_IN_PROGRESS: "Repair under way",
    STATUS_VENDOR_DONE: "Repair finished, waiting for the resident to confirm",
    STATUS_CLOSED: "Closed",
}

# A fault counts as recurring once this many earlier matching incidents exist.
RECURRING_THRESHOLD = 2

# A block's lifts are one shared asset, so any earlier lift incident in the same
# block is related. Other categories also need a similar description.
SHARED_ASSET_CATEGORIES = {"Lift"}

STOP_WORDS = {
    "the", "and", "for", "from", "with", "that", "this", "has", "have", "was", "were",
    "are", "not", "our", "since", "last", "night", "again", "after", "into", "near",
    "out", "needs", "need", "keeps", "stopped", "working", "work", "flat", "block",
    # Locations say where a fault is, not what it is.
    "bathroom", "kitchen", "bedroom", "living", "room", "hallway", "corridor", "lobby",
    "ceiling", "wall", "floor", "sink", "under", "level", "main",
}

# Words that describe the same kind of fault are compared as one concept.
KEYWORD_GROUPS = {
    "water": "water", "leak": "water", "leaks": "water", "leaking": "water", "drip": "water",
    "dripping": "water", "seeping": "water", "seepage": "water", "pipe": "water",
    "burst": "water", "drain": "drain", "drains": "drain", "blocked": "drain", "clogged": "drain",
    "light": "light", "lights": "light", "lighting": "light", "flickering": "light",
    "gate": "gate", "gates": "gate", "camera": "cctv", "cctv": "cctv",
}


def next_id(conn, table, column, prefix):
    """Next sequential ID. Call inside a transaction. table and column are
    fixed names from this codebase, never user input."""
    row = conn.execute(
        f"SELECT MAX(CAST(SUBSTR({column}, ?) AS INTEGER)) AS top FROM {table}",
        (len(prefix) + 1,),
    ).fetchone()
    return f"{prefix}{(row['top'] or 0) + 1:03d}"


def add_history(conn, ticket_id, action, description, created_at, cost=None):
    conn.execute(
        """INSERT INTO ticket_history (ticket_id, action, description, cost, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (ticket_id, action, description, cost, created_at),
    )


def get_ticket(conn, ticket_id):
    ticket_id = validate_ticket_id(ticket_id)
    row = conn.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Ticket {ticket_id} does not exist.")
    return row


def create_ticket(conn, flat=None, block=None, category=None, severity=None, text=None, now=None, reported_by=None):
    """Create a ticket. Missing details are reported, never guessed.

    Pass flat for a complaint about a flat, or block alone for a shared area
    such as a lift or a lobby.
    """
    missing = [
        label
        for label, value in (
            ("--flat or --block", flat or block),
            ("--category", category),
            ("--severity", severity),
            ("--text", text and str(text).strip()),
        )
        if not value
    ]
    if missing:
        raise ValidationError(
            "Missing required information: " + ", ".join(missing) + ". Ask the resident; do not guess."
        )

    category = validate_category(category)
    severity = validate_severity(severity)
    description = clean_text(text, "Complaint text", MAX_DESCRIPTION_LENGTH)

    if flat:
        flat_number = validate_flat(flat)
        block_name = block_from_flat(flat_number)
        if block and validate_block(block) != block_name:
            raise ValidationError(f"Flat {flat_number} is not in {validate_block(block)}.")
    else:
        flat_number = COMMON_AREA
        block_name = validate_block(block)

    created = now or utc_now()
    created_at = to_iso(created)
    reporter = clean_inline(reported_by).lstrip("@ ") if reported_by else ""
    if len(reporter) > MAX_REPORTER_LENGTH:
        raise ValidationError(f"Reporter name is too long (maximum {MAX_REPORTER_LENGTH} characters).")

    with transaction(conn):
        duplicate = conn.execute(
            """SELECT ticket_id FROM tickets
               WHERE flat_number = ? AND block_name = ? AND category = ?
                 AND LOWER(description) = LOWER(?) AND status <> ?""",
            (flat_number, block_name, category, description, STATUS_CLOSED),
        ).fetchone()
        if duplicate:
            raise RuleViolation(
                f"Duplicate complaint: ticket {duplicate['ticket_id']} is already active for this issue."
            )
        ticket_id = next_id(conn, "tickets", "ticket_id", "T-")
        conn.execute(
            """INSERT INTO tickets (ticket_id, flat_number, block_name, category, severity, description,
                                    status, created_at, updated_at, due_at, vendor_id, confirmation_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
            (
                ticket_id, flat_number, block_name, category, severity, description, STATUS_OPEN,
                created_at, created_at, to_iso(calculate_due_at(created, severity)),
                CONFIRMATION_NOT_REQUESTED,
            ),
        )
        add_history(conn, ticket_id, "TICKET_CREATED", f"{severity} {category} ticket created", created_at)
        if reporter:
            add_history(conn, ticket_id, ACTION_REPORTED_BY, reporter, created_at)
    return get_ticket(conn, ticket_id)


def get_reported_by(conn, ticket_id):
    """The name of the person who raised the ticket, or None if not recorded."""
    row = _latest_history(conn, validate_ticket_id(ticket_id), ACTION_REPORTED_BY)
    return row["description"] if row else None


def time_taken(ticket):
    """Time from the complaint to the ticket's last update, in plain words.
    For a closed ticket, that last update is the closure."""
    return friendly_duration(from_iso(ticket["updated_at"]) - from_iso(ticket["created_at"]))


def _set_status(conn, ticket_id, status, confirmation, updated_at):
    conn.execute(
        "UPDATE tickets SET status = ?, confirmation_status = ?, updated_at = ? WHERE ticket_id = ?",
        (status, confirmation, updated_at, ticket_id),
    )


def _require_status(ticket, allowed, action):
    if ticket["status"] not in allowed:
        raise RuleViolation(
            f"Cannot {action} ticket {ticket['ticket_id']} while its status is {ticket['status']}. "
            f"Required status: {' or '.join(allowed)}."
        )


def mark_in_progress(conn, ticket_id, note, now=None):
    """Record that the vendor has started. WORK ORDER CREATED -> IN PROGRESS."""
    note = clean_text(note, "Note", MAX_NOTE_LENGTH)
    moment = to_iso(now or utc_now())
    with transaction(conn):
        ticket = get_ticket(conn, ticket_id)
        _require_status(ticket, (STATUS_WORK_ORDER,), "start work on")
        _set_status(conn, ticket["ticket_id"], STATUS_IN_PROGRESS, ticket["confirmation_status"], moment)
        add_history(conn, ticket["ticket_id"], "IN_PROGRESS", note, moment)
    return get_ticket(conn, ticket_id)


def mark_vendor_done(conn, ticket_id, note, now=None):
    """Record vendor completion. This never closes the ticket; the resident
    still has to confirm. IN PROGRESS is optional on the way here, because the
    PRD's demo goes straight from the work order to vendor-done."""
    note = clean_text(note, "Vendor completion note", MAX_NOTE_LENGTH)
    moment = to_iso(now or utc_now())
    with transaction(conn):
        ticket = get_ticket(conn, ticket_id)
        _require_status(ticket, (STATUS_WORK_ORDER, STATUS_IN_PROGRESS), "record vendor completion for")
        _set_status(conn, ticket["ticket_id"], STATUS_VENDOR_DONE, CONFIRMATION_PENDING, moment)
        add_history(conn, ticket["ticket_id"], "VENDOR_DONE", note, moment)
    return get_ticket(conn, ticket_id)


def mark_not_resolved(conn, ticket_id, note, now=None):
    """The resident says the repair did not work: VENDOR-DONE -> IN PROGRESS.
    The existing work order stays active, so no extra money is committed."""
    note = clean_text(note, "Resident feedback note", MAX_NOTE_LENGTH)
    moment = to_iso(now or utc_now())
    with transaction(conn):
        ticket = get_ticket(conn, ticket_id)
        _require_status(ticket, (STATUS_VENDOR_DONE,), "record resident feedback for")
        _set_status(conn, ticket["ticket_id"], STATUS_IN_PROGRESS, CONFIRMATION_DECLINED, moment)
        add_history(conn, ticket["ticket_id"], "RESIDENT_NOT_RESOLVED", note, moment)
    return get_ticket(conn, ticket_id)


def record_decline(conn, ticket_id, reason, next_step, now=None):
    """Record why the manager declined a work order and what happens next. No
    money moves, and the ticket stays OPEN so that a new request can follow."""
    reason = clean_text(reason, "Decline reason", MAX_NOTE_LENGTH)
    next_step = clean_text(next_step, "Next step", MAX_NOTE_LENGTH)
    moment = to_iso(now or utc_now())
    with transaction(conn):
        ticket = get_ticket(conn, ticket_id)
        _require_status(ticket, (STATUS_OPEN,), "record a declined work order for")
        conn.execute(
            "UPDATE tickets SET updated_at = ? WHERE ticket_id = ?", (moment, ticket["ticket_id"])
        )
        add_history(conn, ticket["ticket_id"], ACTION_DECLINED, f"{reason} | Next step: {next_step}", moment)
    return get_ticket(conn, ticket_id)


def record_delay(conn, ticket_id, reason, now=None):
    """Record the reason a repair is taking longer, for residents who ask."""
    reason = clean_text(reason, "Delay reason", MAX_NOTE_LENGTH)
    moment = to_iso(now or utc_now())
    with transaction(conn):
        ticket = get_ticket(conn, ticket_id)
        if ticket["status"] == STATUS_CLOSED:
            raise RuleViolation(f"Ticket {ticket['ticket_id']} is already closed.")
        conn.execute(
            "UPDATE tickets SET updated_at = ? WHERE ticket_id = ?", (moment, ticket["ticket_id"])
        )
        add_history(conn, ticket["ticket_id"], ACTION_DELAY, reason, moment)
    return get_ticket(conn, ticket_id)


def estimate_repair(conn, ticket):
    """Estimate repair time from the median of earlier finished repairs with the
    same category and severity, never beyond the SLA target. With no earlier
    repairs, the SLA target is the estimate. Only repairs finished before the
    ticket was created count, so a ticket's estimate does not change later."""
    rows = conn.execute(
        """SELECT t.created_at AS started, MAX(h.created_at) AS finished
           FROM tickets t JOIN ticket_history h ON h.ticket_id = t.ticket_id
           WHERE t.category = ? AND t.severity = ? AND t.ticket_id <> ?
             AND h.action IN ('HISTORICAL_REPAIR', 'VENDOR_DONE') AND h.created_at <= ?
           GROUP BY t.ticket_id""",
        (ticket["category"], ticket["severity"], ticket["ticket_id"], ticket["created_at"]),
    ).fetchall()

    target = SLA_HOURS[ticket["severity"]]
    if rows:
        median = statistics.median(
            (from_iso(row["finished"]) - from_iso(row["started"])).total_seconds() / 3600 for row in rows
        )
        # Round up to whole hours or days, so that the promise is one we can keep.
        hours = math.ceil(median) if median < 24 else math.ceil(median / 24) * 24
        hours = max(1, min(hours, target))
    else:
        hours = target

    if hours < 24:
        label = f"about {hours} hour{'s' if hours != 1 else ''}"
    else:
        days = math.ceil(hours / 24)
        label = f"about {days} day{'s' if days != 1 else ''}"
    return {
        "hours": hours,
        "label": label,
        "based_on": len(rows),
        "expected_by": to_iso(from_iso(ticket["created_at"]) + timedelta(hours=hours)),
    }


def _latest_history(conn, ticket_id, action):
    return conn.execute(
        """SELECT description FROM ticket_history WHERE ticket_id = ? AND action = ?
           ORDER BY created_at DESC, history_id DESC LIMIT 1""",
        (ticket_id, action),
    ).fetchone()


def get_status(conn, ticket_id, now=None):
    """A resident-safe summary, with no costs, vendors or budget details."""
    ticket = get_ticket(conn, ticket_id)
    estimate = estimate_repair(conn, ticket)
    is_open = ticket["status"] != STATUS_CLOSED
    delay = _latest_history(conn, ticket["ticket_id"], ACTION_DELAY)

    decline_reason = next_step = None
    if ticket["status"] == STATUS_OPEN:
        declined = _latest_history(conn, ticket["ticket_id"], ACTION_DECLINED)
        if declined:
            decline_reason, _, next_step = declined["description"].partition(" | Next step: ")

    return {
        "ticket": ticket,
        "progress": PROGRESS_TEXT[ticket["status"]],
        "estimate": estimate,
        "is_late": is_open and to_iso(now or utc_now()) > estimate["expected_by"],
        "delay_reason": delay["description"] if delay and is_open else None,
        "decline_reason": decline_reason,
        "next_step": next_step,
        "reported_by": get_reported_by(conn, ticket["ticket_id"]),
    }


def _confirmation_matches_ticket(ticket, confirmed_by):
    """A confirmation must name a resident of the affected flat, or of the same
    block for a shared-area ticket. Vendors and staff can never match."""
    text = confirmed_by.strip().upper()
    if ticket["flat_number"] != COMMON_AREA:
        return text == f"{ticket['flat_number']} RESIDENT"
    letter = ticket["block_name"].split()[-1]
    return bool(re.fullmatch(rf"({letter}-\d{{1,4}}|BLOCK {letter}) RESIDENT", text))


def close_ticket(conn, ticket_id, confirmed_by=None, now=None):
    """Close a ticket. Python checks the status and the confirmation itself."""
    moment = to_iso(now or utc_now())
    with transaction(conn):
        ticket = get_ticket(conn, ticket_id)
        _require_status(ticket, (STATUS_VENDOR_DONE,), "close")
        confirmed_text = clean_inline(confirmed_by) if confirmed_by else ""
        if not confirmed_text:
            raise RuleViolation(
                f"Cannot close ticket {ticket['ticket_id']}: explicit resident confirmation is missing. "
                'Ask the resident, then pass --confirmed-by "<flat> resident".'
            )
        if not _confirmation_matches_ticket(ticket, confirmed_text):
            expected = (
                f"{ticket['flat_number']} resident"
                if ticket["flat_number"] != COMMON_AREA
                else f"<flat in {ticket['block_name']}> resident"
            )
            raise RuleViolation(
                f"Cannot close ticket {ticket['ticket_id']}: '{confirmed_text}' is not a valid resident "
                f"confirmation for this ticket. Expected: {expected}."
            )
        _set_status(conn, ticket["ticket_id"], STATUS_CLOSED, CONFIRMATION_CONFIRMED, moment)
        conn.execute(
            "UPDATE work_orders SET status = 'COMPLETED' WHERE ticket_id = ? AND status = 'APPROVED'",
            (ticket["ticket_id"],),
        )
        add_history(conn, ticket["ticket_id"], "CLOSED", f"Resident confirmed resolution: {confirmed_text}", moment)
    return get_ticket(conn, ticket_id)


def get_history(conn, ticket_id):
    ticket = get_ticket(conn, ticket_id)
    return conn.execute(
        "SELECT * FROM ticket_history WHERE ticket_id = ? ORDER BY created_at, history_id",
        (ticket["ticket_id"],),
    ).fetchall()


def _keywords(text):
    words = re.findall(r"[a-z]+", text.lower())
    return {KEYWORD_GROUPS.get(word, word) for word in words if len(word) >= 3 and word not in STOP_WORDS}


def find_related_incidents(conn, ticket_id):
    """Rule-based recurring fault check against earlier tickets.

    A previous incident matches when it has the same block and category and was
    created earlier. Outside SHARED_ASSET_CATEGORIES it must also share at least
    one description keyword.
    """
    ticket = get_ticket(conn, ticket_id)
    rows = conn.execute(
        """SELECT t.ticket_id, t.flat_number, t.description, t.status, t.created_at,
                  COALESCE((SELECT SUM(h.cost) FROM ticket_history h
                            WHERE h.ticket_id = t.ticket_id), 0) AS cost
           FROM tickets t
           WHERE t.block_name = ? AND t.category = ? AND t.ticket_id <> ? AND t.created_at < ?
           ORDER BY t.created_at""",
        (ticket["block_name"], ticket["category"], ticket["ticket_id"], ticket["created_at"]),
    ).fetchall()

    shared_asset = ticket["category"] in SHARED_ASSET_CATEGORIES
    wanted = _keywords(ticket["description"])
    incidents = []
    for row in rows:
        common = wanted & _keywords(row["description"])
        if shared_asset or common:
            matched_on = f"same block and {ticket['category']} asset" if shared_asset else (
                "same block and category; keywords: " + ", ".join(sorted(common))
            )
            incidents.append({**dict(row), "matched_on": matched_on})

    return {
        "ticket_id": ticket["ticket_id"],
        "block_name": ticket["block_name"],
        "category": ticket["category"],
        "previous_incident_count": len(incidents),
        "ticket_ids": [incident["ticket_id"] for incident in incidents],
        "incidents": incidents,
        "total_cost": sum(incident["cost"] for incident in incidents),
        "is_recurring": len(incidents) >= RECURRING_THRESHOLD,
    }


def list_overdue(conn, now=None):
    """Unclosed tickets past due_at. VENDOR-DONE tickets stay on this list
    because the issue is not resolved until the resident confirms."""
    moment = now or utc_now()
    rows = conn.execute(
        "SELECT * FROM tickets WHERE status <> ? AND due_at < ? ORDER BY due_at",
        (STATUS_CLOSED, to_iso(moment)),
    ).fetchall()
    return [
        {**dict(row), "time_overdue": format_duration(moment - from_iso(row["due_at"]))}
        for row in rows
    ]
