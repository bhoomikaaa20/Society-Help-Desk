"""Monthly maintenance report, returned as plain data for the CLI to print."""

import budget_manager
import ticket_manager
from common import STATUS_CLOSED, to_iso, utc_now, validate_month


def _count_by(rows, key):
    counts = {}
    for row in rows:
        counts[row[key]] = counts.get(row[key], 0) + 1
    return dict(sorted(counts.items()))


def _recurring_faults(conn, tickets):
    """Apply the same rule as `history` to each ticket raised this month, keeping
    the widest result for each block and category."""
    faults = {}
    for ticket in tickets:
        analysis = ticket_manager.find_related_incidents(conn, ticket["ticket_id"])
        if not analysis["is_recurring"]:
            continue
        key = (analysis["block_name"], analysis["category"])
        best = faults.get(key)
        if best is None or analysis["previous_incident_count"] > best["previous_incident_count"]:
            faults[key] = analysis
    return [faults[key] for key in sorted(faults)]


def _vendor_performance(conn, tickets):
    """Per vendor, for tickets raised this month. A job counts as on time if the
    vendor finished by the ticket's due_at."""
    performance = {}
    for ticket in tickets:
        vendor_id = ticket["vendor_id"]
        if not vendor_id:
            continue
        entry = performance.get(vendor_id)
        if entry is None:
            vendor = conn.execute(
                "SELECT vendor_name, rating FROM vendors WHERE vendor_id = ?", (vendor_id,)
            ).fetchone()
            entry = performance[vendor_id] = {
                "vendor_id": vendor_id,
                "vendor_name": vendor["vendor_name"],
                "rating": vendor["rating"],
                "jobs": 0,
                "completed": 0,
                "on_time": 0,
                "total_cost": 0,
            }
        entry["jobs"] += 1

        cost = conn.execute(
            "SELECT COALESCE(SUM(cost), 0) AS total FROM ticket_history WHERE ticket_id = ?",
            (ticket["ticket_id"],),
        ).fetchone()
        entry["total_cost"] += cost["total"]

        finished = conn.execute(
            """SELECT MAX(created_at) AS finished_at FROM ticket_history
               WHERE ticket_id = ? AND action IN ('VENDOR_DONE', 'HISTORICAL_REPAIR')""",
            (ticket["ticket_id"],),
        ).fetchone()["finished_at"]
        if finished:
            entry["completed"] += 1
            if finished <= ticket["due_at"]:
                entry["on_time"] += 1
    return [performance[key] for key in sorted(performance)]


def monthly_report(conn, month, now=None):
    """Complaints are selected by created_at within the month. Spending covers
    costs recorded in the month. Overdue counts and remaining budget are as of now."""
    start, end = validate_month(month)
    as_of = to_iso(now or utc_now())

    tickets = conn.execute(
        "SELECT * FROM tickets WHERE created_at >= ? AND created_at < ? ORDER BY created_at",
        (start, end),
    ).fetchall()
    spending = conn.execute(
        """SELECT COALESCE(SUM(cost), 0) AS total FROM ticket_history
           WHERE cost IS NOT NULL AND created_at >= ? AND created_at < ?""",
        (start, end),
    ).fetchone()["total"]
    budgets = budget_manager.list_budgets(conn)

    closed = [ticket for ticket in tickets if ticket["status"] == STATUS_CLOSED]
    unclosed = [ticket for ticket in tickets if ticket["status"] != STATUS_CLOSED]

    return {
        "month": str(month).strip(),
        "as_of": as_of,
        "total_complaints": len(tickets),
        "open_tickets": len(unclosed),
        "closed_tickets": len(closed),
        "overdue_tickets": sum(1 for ticket in unclosed if ticket["due_at"] < as_of),
        "by_category": _count_by(tickets, "category"),
        "by_severity": _count_by(tickets, "severity"),
        "recurring_faults": _recurring_faults(conn, tickets),
        "total_approved_spending": spending,
        "remaining_budget_total": sum(budget["remaining_amount"] for budget in budgets),
        "budgets": budgets,
        "vendor_performance": _vendor_performance(conn, tickets),
    }
