"""Category budgets. All budget arithmetic and enforcement happens here."""

from common import NotFoundError, RuleViolation, format_eur, validate_category


def list_budgets(conn):
    return conn.execute("SELECT * FROM budgets ORDER BY category").fetchall()


def get_budget(conn, category):
    category = validate_category(category)
    row = conn.execute("SELECT * FROM budgets WHERE category = ?", (category,)).fetchone()
    if row is None:
        raise NotFoundError(f"No budget is configured for category {category}.")
    return row


def ensure_affordable(conn, category, cents):
    budget = get_budget(conn, category)
    if cents > budget["remaining_amount"]:
        raise RuleViolation(
            f"Insufficient budget. {budget['category']} has {format_eur(budget['remaining_amount'])} "
            f"remaining but {format_eur(cents)} was requested. No work order was created."
        )
    return budget


def charge(conn, category, cents):
    """Commit spending. Call inside the caller's transaction, which is what makes
    the check and the update atomic with the work order itself."""
    ensure_affordable(conn, category, cents)
    conn.execute(
        """UPDATE budgets
           SET spent_amount = spent_amount + ?, remaining_amount = remaining_amount - ?
           WHERE category = ?""",
        (cents, cents, category),
    )
