"""TESTS 4 and 14: budget enforcement and amount validation, plus recommendations."""

import unittest

from helpers import DeskTestCase

import budget_manager
import ticket_manager as tm
import vendor_manager
import work_order_manager as wom
from common import NotFoundError, RuleViolation, ValidationError, format_eur, parse_amount


class BudgetRules(DeskTestCase):
    def test_prd_budget_example(self):
        budget = budget_manager.get_budget(self.conn, "plumbing")
        self.assertEqual(budget["allocated_amount"], 1000000)
        self.assertEqual(budget["spent_amount"], 750000)
        self.assertEqual(budget["remaining_amount"], 250000)

    def test_04_amount_over_budget_rejected_without_side_effects(self):
        with self.assertRaises(RuleViolation) as caught:
            wom.create_work_order(self.conn, "T-014", "V-02", "3000")
        self.assertIn("Insufficient budget", str(caught.exception))
        self.assertEqual(self.budget("Plumbing")["spent_amount"], 750000)
        self.assertEqual(self.count("work_orders"), 0)
        self.assertEqual(tm.get_ticket(self.conn, "T-014")["status"], "OPEN")

    def test_cli_over_budget_exit_code(self):
        result = self.run_cli("work-order", "T-014", "V-02", "--amount", "3000")
        self.assertEqual(result.returncode, 1)
        self.assertIn("REJECTED: Insufficient budget", result.stderr)

    def test_exact_remaining_budget_is_allowed(self):
        wom.create_work_order(self.conn, "T-014", "V-02", "2500")
        self.assertEqual(self.budget("Plumbing")["remaining_amount"], 0)

    def test_14_negative_and_zero_amounts_rejected(self):
        for amount in ("-100", "0", "0.00", "-0.01"):
            with self.subTest(amount=amount), self.assertRaises(ValidationError):
                wom.create_work_order(self.conn, "T-014", "V-02", amount)
        self.assertEqual(self.count("work_orders"), 0)

    def test_cli_negative_amount_exit_code(self):
        result = self.run_cli("work-order", "T-014", "V-02", "--amount", "-100")
        self.assertEqual(result.returncode, 2)
        self.assertIn("greater than zero", result.stderr)

    def test_malformed_amounts_rejected(self):
        for amount in ("abc", "NaN", "Infinity", "1.005", "", "1e9"):
            with self.subTest(amount=amount), self.assertRaises(ValidationError):
                parse_amount(amount)

    def test_money_handling_is_exact(self):
        self.assertEqual(parse_amount("2,500.10"), 250010)
        self.assertEqual(parse_amount("0.07"), 7)
        self.assertEqual(format_eur(250010), "EUR 2,500.10")


class VendorRecommendation(DeskTestCase):
    def test_returns_required_fields_for_the_ticket_category(self):
        vendors = vendor_manager.recommend_vendors(self.conn, "T-014")["vendors"]
        self.assertEqual(vendors[0]["vendor_id"], "V-02")
        self.assertEqual({vendor["service_category"] for vendor in vendors}, {"Plumbing"})
        for field in ("vendor_name", "estimated_cost", "response_hours", "rating", "reason"):
            self.assertTrue(vendors[0][field])

    def test_estimate_over_remaining_budget_is_flagged(self):
        self.conn.execute(
            "UPDATE budgets SET spent_amount = 990000, remaining_amount = 10000 WHERE category = 'Plumbing'"
        )
        top = vendor_manager.recommend_vendors(self.conn, "T-014")["vendors"][0]
        self.assertFalse(top["within_budget"])
        self.assertIn("EXCEEDS", top["reason"])

    def test_no_suitable_vendor_gives_clear_error(self):
        self.conn.execute("UPDATE tickets SET vendor_id = NULL WHERE category = 'Painting'")
        self.conn.execute("DELETE FROM vendors WHERE service_category = 'Painting'")
        ticket = tm.create_ticket(self.conn, flat="A-1", category="Painting", severity="Low", text="Peeling paint")
        with self.assertRaises(NotFoundError) as caught:
            vendor_manager.recommend_vendors(self.conn, ticket["ticket_id"])
        self.assertIn("No suitable vendor", str(caught.exception))

    def test_cli_labels_prices_as_estimates(self):
        result = self.run_cli("vendor", "T-014")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ESTIMATES", result.stdout)
        self.assertIn("RECOMMENDED: V-02", result.stdout)


if __name__ == "__main__":
    unittest.main()
