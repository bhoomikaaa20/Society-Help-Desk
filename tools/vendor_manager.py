"""Transparent, rule-based vendor recommendation from the vendors table only."""

import budget_manager
import ticket_manager
from common import (
    NotFoundError,
    SLA_HOURS,
    STATUS_CLOSED,
    RuleViolation,
    format_eur,
    validate_vendor_id,
)

# Score weights: faster response matters more for urgent work, price for low.
WEIGHTS = {
    "Urgent": {"response": 0.5, "rating": 0.3, "cost": 0.2},
    "Normal": {"response": 0.3, "rating": 0.35, "cost": 0.35},
    "Low": {"response": 0.2, "rating": 0.3, "cost": 0.5},
}


def get_vendor(conn, vendor_id):
    vendor_id = validate_vendor_id(vendor_id)
    row = conn.execute("SELECT * FROM vendors WHERE vendor_id = ?", (vendor_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Vendor {vendor_id} does not exist.")
    return row


def _closeness(value, values, higher_is_better):
    """Scale a value to 0..1 within the candidates, where 1 is the best."""
    low, high = min(values), max(values)
    if high == low:
        return 1.0
    position = (value - low) / (high - low)
    return position if higher_is_better else 1 - position


def recommend_vendors(conn, ticket_id):
    """Return every vendor for the ticket's category, best first, with reasons."""
    ticket = ticket_manager.get_ticket(conn, ticket_id)
    if ticket["status"] == STATUS_CLOSED:
        raise RuleViolation(f"Ticket {ticket['ticket_id']} is closed, so it does not need a vendor.")

    vendors = conn.execute(
        "SELECT * FROM vendors WHERE service_category = ?", (ticket["category"],)
    ).fetchall()
    if not vendors:
        raise NotFoundError(f"No suitable vendor exists for category {ticket['category']}.")

    budget = budget_manager.get_budget(conn, ticket["category"])
    weights = WEIGHTS[ticket["severity"]]
    costs = [vendor["estimated_cost"] for vendor in vendors]
    responses = [vendor["response_hours"] for vendor in vendors]
    ratings = [vendor["rating"] for vendor in vendors]

    results = []
    for vendor in vendors:
        score = (
            weights["response"] * _closeness(vendor["response_hours"], responses, False)
            + weights["rating"] * _closeness(vendor["rating"], ratings, True)
            + weights["cost"] * _closeness(vendor["estimated_cost"], costs, False)
        )
        results.append({**dict(vendor), "score": round(score, 3)})
    results.sort(key=lambda item: (-item["score"], item["vendor_id"]))

    for rank, result in enumerate(results):
        result["within_budget"] = result["estimated_cost"] <= budget["remaining_amount"]
        result["reason"] = _reason(result, ticket, rank, len(results), weights, budget)
    return {"ticket": ticket, "budget": budget, "vendors": results}


def _reason(vendor, ticket, rank, total, weights, budget):
    parts = []
    if total == 1:
        parts.append(f"Only {ticket['category']} vendor on file")
    elif rank == 0:
        parts.append(f"Highest score of {total} {ticket['category']} vendors for {ticket['severity']} severity")
    else:
        parts.append(f"Alternative, ranked {rank + 1} of {total}")
    parts.append(
        f"weights: response {weights['response']:.0%}, rating {weights['rating']:.0%}, cost {weights['cost']:.0%}"
    )
    target = SLA_HOURS[ticket["severity"]]
    relation = "within" if vendor["response_hours"] <= target else "slower than"
    parts.append(f"{vendor['response_hours']}h response is {relation} the {target}h target")
    if vendor["within_budget"]:
        parts.append(f"estimate fits the {format_eur(budget['remaining_amount'])} remaining budget")
    else:
        parts.append(f"estimate EXCEEDS the {format_eur(budget['remaining_amount'])} remaining budget")
    return "; ".join(parts)
