"""SQLite connection, schema, and repeatable CSV import.

All three agents share one database file. Set SOCIETY_DESK_DB to point at a
different file; the test suite uses this to stay away from the demo database.
"""

import csv
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from common import (
    CATEGORIES,
    COMMON_AREA,
    CONFIRMATION_CONFIRMED,
    CONFIRMATION_NOT_REQUESTED,
    MAX_DESCRIPTION_LENGTH,
    SLA_HOURS,
    STATUS_CLOSED,
    STATUS_OPEN,
    ValidationError,
    block_from_flat,
    calculate_due_at,
    clean_inline,
    clean_text,
    from_iso,
    parse_csv_money,
    to_iso,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DB_PATH = PROJECT_ROOT / "database" / "society_desk.db"

# All money columns hold integer euro cents.
SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
    vendor_id           TEXT PRIMARY KEY,
    vendor_name         TEXT NOT NULL,
    service_category    TEXT NOT NULL,
    estimated_cost      INTEGER NOT NULL CHECK (estimated_cost > 0),
    response_hours      INTEGER NOT NULL CHECK (response_hours > 0),
    rating              REAL NOT NULL CHECK (rating BETWEEN 0 AND 5),
    contact_information TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id           TEXT PRIMARY KEY,
    flat_number         TEXT NOT NULL,
    block_name          TEXT NOT NULL,
    category            TEXT NOT NULL,
    severity            TEXT NOT NULL CHECK (severity IN ('Urgent', 'Normal', 'Low')),
    description         TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN
        ('OPEN', 'WORK ORDER CREATED', 'IN PROGRESS', 'VENDOR-DONE', 'CLOSED')),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    due_at              TEXT NOT NULL,
    vendor_id           TEXT REFERENCES vendors (vendor_id),
    confirmation_status TEXT NOT NULL CHECK (confirmation_status IN
        ('NOT REQUESTED', 'PENDING', 'CONFIRMED', 'DECLINED'))
);

CREATE TABLE IF NOT EXISTS budgets (
    budget_id        TEXT PRIMARY KEY,
    category         TEXT NOT NULL UNIQUE,
    allocated_amount INTEGER NOT NULL CHECK (allocated_amount >= 0),
    spent_amount     INTEGER NOT NULL CHECK (spent_amount >= 0),
    remaining_amount INTEGER NOT NULL CHECK (remaining_amount >= 0),
    CHECK (remaining_amount = allocated_amount - spent_amount)
);

CREATE TABLE IF NOT EXISTS work_orders (
    work_order_id TEXT PRIMARY KEY,
    ticket_id     TEXT NOT NULL REFERENCES tickets (ticket_id),
    vendor_id     TEXT NOT NULL REFERENCES vendors (vendor_id),
    amount        INTEGER NOT NULL CHECK (amount > 0),
    status        TEXT NOT NULL CHECK (status IN ('APPROVED', 'COMPLETED')),
    created_at    TEXT NOT NULL,
    approved_at   TEXT
);

-- The database itself refuses a second active work order for a ticket.
CREATE UNIQUE INDEX IF NOT EXISTS one_active_work_order_per_ticket
    ON work_orders (ticket_id) WHERE status = 'APPROVED';

CREATE TABLE IF NOT EXISTS ticket_history (
    history_id  INTEGER PRIMARY KEY,
    ticket_id   TEXT NOT NULL REFERENCES tickets (ticket_id),
    action      TEXT NOT NULL,
    description TEXT NOT NULL,
    cost        INTEGER CHECK (cost IS NULL OR cost >= 0),
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS tickets_by_block_category ON tickets (block_name, category);
CREATE INDEX IF NOT EXISTS history_by_ticket ON ticket_history (ticket_id);
"""

HISTORICAL_REPAIR = "HISTORICAL_REPAIR"


def database_path():
    return Path(os.environ.get("SOCIETY_DESK_DB") or DEFAULT_DB_PATH)


def connect(db_path=None):
    """Open the database and make sure the schema exists."""
    path = Path(db_path) if db_path else database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: transactions are always explicit, via transaction().
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def open_database(db_path=None):
    """Connect, importing the demo data if this database has never been seeded."""
    conn = connect(db_path)
    if conn.execute("SELECT 1 FROM vendors LIMIT 1").fetchone() is None:
        import_csv_data(conn)
    return conn


@contextmanager
def transaction(conn):
    """Run a block atomically. BEGIN IMMEDIATE takes the write lock up front, so
    reads inside the block (such as a budget check) cannot go stale before the
    write that depends on them."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _read_csv(path):
    if not path.is_file():
        raise ValidationError(f"Required data file is missing: {path.name}")
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _field(row, name, source):
    """Fetch a required CSV value, with an error that names the file."""
    value = (row.get(name) or "").strip()
    if not value:
        raise ValidationError(f"{source}: column '{name}' is missing in row {clean_inline(dict(row))}")
    return value


def _known_category(value, source):
    if value not in CATEGORIES:
        raise ValidationError(f"{source}: unknown category '{clean_inline(value)}'.")
    return value


def _import_vendors(conn, data_dir):
    source = "vendors.csv"
    inserted = skipped = 0
    for row in _read_csv(data_dir / source):
        cursor = conn.execute(
            """INSERT INTO vendors (vendor_id, vendor_name, service_category, estimated_cost,
                                    response_hours, rating, contact_information)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT DO NOTHING""",
            (
                _field(row, "vendor_id", source),
                _field(row, "vendor_name", source),
                _known_category(_field(row, "service_category", source), source),
                parse_csv_money(_field(row, "estimated_cost_eur", source), "estimated_cost_eur"),
                int(_field(row, "response_hours", source)),
                float(_field(row, "rating", source)),
                _field(row, "contact_information", source),
            ),
        )
        inserted += cursor.rowcount
        skipped += 1 - cursor.rowcount
    return inserted, skipped


def _import_budgets(conn, data_dir):
    source = "budget.csv"
    inserted = skipped = 0
    for row in _read_csv(data_dir / source):
        allocated = parse_csv_money(_field(row, "allocated_eur", source), "allocated_eur")
        spent = parse_csv_money(_field(row, "spent_eur", source), "spent_eur")
        # ON CONFLICT DO NOTHING keeps live spending when the import is re-run.
        cursor = conn.execute(
            """INSERT INTO budgets (budget_id, category, allocated_amount, spent_amount, remaining_amount)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT DO NOTHING""",
            (
                _field(row, "budget_id", source),
                _known_category(_field(row, "category", source), source),
                allocated,
                spent,
                allocated - spent,
            ),
        )
        inserted += cursor.rowcount
        skipped += 1 - cursor.rowcount
    return inserted, skipped


def _import_past_tickets(conn, data_dir):
    source = "past_tickets.csv"
    inserted = skipped = 0
    for row in _read_csv(data_dir / source):
        ticket_id = _field(row, "ticket_id", source)
        flat = _field(row, "flat_number", source)
        severity = _field(row, "severity", source)
        status = _field(row, "status", source)
        category = _known_category(_field(row, "category", source), source)
        if severity not in SLA_HOURS or status not in (STATUS_OPEN, STATUS_CLOSED):
            raise ValidationError(f"{source}: {ticket_id} has an unsupported severity or status.")

        created = from_iso(_field(row, "created_at", source))
        closed_text = (row.get("closed_at") or "").strip()
        closed = from_iso(closed_text) if closed_text else None
        if (status == STATUS_CLOSED) != (closed is not None):
            raise ValidationError(f"{source}: {ticket_id} needs closed_at exactly when status is CLOSED.")

        block = _field(row, "block_name", source)
        if flat != COMMON_AREA and block != block_from_flat(flat):
            raise ValidationError(f"{source}: {ticket_id} flat and block do not match.")

        cursor = conn.execute(
            """INSERT INTO tickets (ticket_id, flat_number, block_name, category, severity, description,
                                    status, created_at, updated_at, due_at, vendor_id, confirmation_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT DO NOTHING""",
            (
                ticket_id,
                flat,
                block,
                category,
                severity,
                clean_text(row.get("description"), f"{source} {ticket_id} description", MAX_DESCRIPTION_LENGTH),
                status,
                to_iso(created),
                to_iso(closed or created),
                to_iso(calculate_due_at(created, severity)),
                (row.get("vendor_id") or "").strip() or None,
                CONFIRMATION_CONFIRMED if status == STATUS_CLOSED else CONFIRMATION_NOT_REQUESTED,
            ),
        )
        if cursor.rowcount == 0:
            skipped += 1
            continue
        inserted += 1

        # History is only written for a newly inserted ticket, so re-running
        # the import cannot duplicate it or double count historical costs.
        if status == STATUS_CLOSED:
            cost = parse_csv_money(row.get("historical_cost_eur") or "0", "historical_cost_eur")
            conn.execute(
                """INSERT INTO ticket_history (ticket_id, action, description, cost, created_at)
                   VALUES (?, ?, 'Imported historical repair record', ?, ?)""",
                (ticket_id, HISTORICAL_REPAIR, cost, to_iso(closed)),
            )
        else:
            conn.execute(
                """INSERT INTO ticket_history (ticket_id, action, description, cost, created_at)
                   VALUES (?, 'TICKET_CREATED', 'Imported open ticket', NULL, ?)""",
                (ticket_id, to_iso(created)),
            )
    return inserted, skipped


def _import_all(conn, data_dir):
    # Vendors go first because tickets reference them.
    try:
        return {
            "vendors": _import_vendors(conn, data_dir),
            "budgets": _import_budgets(conn, data_dir),
            "past_tickets": _import_past_tickets(conn, data_dir),
        }
    except ValueError as error:  # Malformed number or date in a CSV file.
        raise ValidationError(f"Could not import CSV data: {error}") from None


def import_csv_data(conn, data_dir=None):
    """Import the CSV files in one transaction. Safe to run repeatedly: existing
    records are kept unchanged and nothing is duplicated."""
    with transaction(conn):
        return _import_all(conn, Path(data_dir) if data_dir else DATA_DIR)


def reset_demo_data(conn, data_dir=None):
    """Replace all data with a clean copy of the CSV demo data, atomically."""
    with transaction(conn):
        for table in ("ticket_history", "work_orders", "tickets", "budgets", "vendors"):
            conn.execute(f"DELETE FROM {table}")  # Fixed names only, never user input.
        return _import_all(conn, Path(data_dir) if data_dir else DATA_DIR)
