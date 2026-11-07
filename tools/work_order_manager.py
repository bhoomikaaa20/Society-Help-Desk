"""Work order execution.

Human approval is enforced by Switch, BEFORE this code runs: the Treasurer agent
asks to run the command and Switch posts the Allow once / Decline request. This
module has no way to verify that approval and deliberately does not pretend to.
A declined request never reaches this code, so nothing is created or charged.
"""

import budget_manager
import ticket_manager
import vendor_manager
from common import (
    RuleViolation,
    STATUS_OPEN,
    STATUS_WORK_ORDER,
    format_eur,
    parse_amount,
    to_iso,
    utc_now,
    validate_ticket_id,
    validate_vendor_id,
)
from database import transaction


def create_work_order(conn, ticket_id, vendor_id, amount, now=None):
    """Validate, then atomically create the work order, charge the budget, update
    the ticket and write history. Any failure leaves all of them unchanged."""
    ticket_id = validate_ticket_id(ticket_id)
    vendor_id = validate_vendor_id(vendor_id)
    cents = parse_amount(amount)
    moment = to_iso(now or utc_now())

    with transaction(conn):
        ticket = ticket_manager.get_ticket(conn, ticket_id)
        vendor = vendor_manager.get_vendor(conn, vendor_id)

        active = conn.execute(
            "SELECT work_order_id FROM work_orders WHERE ticket_id = ? AND status = 'APPROVED'",
            (ticket_id,),
        ).fetchone()
        if active:
            raise RuleViolation(
                f"Duplicate work order: {active['work_order_id']} is already active for ticket {ticket_id}."
            )
        if ticket["status"] != STATUS_OPEN:
            raise RuleViolation(
                f"Ticket {ticket_id} has status {ticket['status']}; work orders need status {STATUS_OPEN}."
            )
        if vendor["service_category"] != ticket["category"]:
            raise RuleViolation(
                f"Vendor {vendor_id} provides {vendor['service_category']}, "
                f"but ticket {ticket_id} is {ticket['category']}."
            )

        budget_manager.charge(conn, ticket["category"], cents)

        work_order_id = ticket_manager.next_id(conn, "work_orders", "work_order_id", "WO-")
        conn.execute(
            """INSERT INTO work_orders (work_order_id, ticket_id, vendor_id, amount, status, created_at, approved_at)
               VALUES (?, ?, ?, ?, 'APPROVED', ?, ?)""",
            (work_order_id, ticket_id, vendor_id, cents, moment, moment),
        )
        conn.execute(
            "UPDATE tickets SET status = ?, vendor_id = ?, updated_at = ? WHERE ticket_id = ?",
            (STATUS_WORK_ORDER, vendor_id, moment, ticket_id),
        )
        ticket_manager.add_history(
            conn,
            ticket_id,
            "WORK_ORDER_CREATED",
            f"{work_order_id} issued to {vendor_id} ({vendor['vendor_name']}) for {format_eur(cents)}",
            moment,
            cost=cents,
        )
    return {
        "work_order": conn.execute(
            "SELECT * FROM work_orders WHERE work_order_id = ?", (work_order_id,)
        ).fetchone(),
        "vendor": vendor,
        "budget": budget_manager.get_budget(conn, ticket["category"]),
    }
