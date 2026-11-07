"""TEST 12: monthly reporting accuracy, using a month with no seeded data."""

import unittest
from datetime import datetime, timedelta, timezone

from helpers import DeskTestCase

import report_manager
import ticket_manager as tm
import work_order_manager as wom
from common import ValidationError

IN_MONTH = datetime(2030, 3, 10, 9, 0, tzinfo=timezone.utc)
AS_OF = datetime(2030, 3, 20, 9, 0, tzinfo=timezone.utc)


class MonthlyReport(DeskTestCase):
    def setUp(self):
        super().setUp()
        add = lambda **details: tm.create_ticket(self.conn, now=IN_MONTH, **details)
        self.closed = add(block="B", category="Lift", severity="Urgent", text="Lift stopped")
        self.open_overdue = add(flat="A-1", category="Plumbing", severity="Normal", text="Slow drain")
        self.open_on_time = tm.create_ticket(
            self.conn, flat="A-2", category="Painting", severity="Low", text="Peeling paint",
            now=AS_OF - timedelta(days=1),
        )
        # These two fall on either side of the month and must not be counted.
        tm.create_ticket(self.conn, flat="A-3", category="Cleaning", severity="Low", text="Before",
                         now=datetime(2030, 2, 28, 23, 59, tzinfo=timezone.utc))
        tm.create_ticket(self.conn, flat="A-4", category="Cleaning", severity="Low", text="After",
                         now=datetime(2030, 4, 1, 0, 0, tzinfo=timezone.utc))

        wom.create_work_order(self.conn, self.closed["ticket_id"], "V-04", "3200", now=IN_MONTH)
        tm.mark_vendor_done(self.conn, self.closed["ticket_id"], "Fixed", now=IN_MONTH + timedelta(hours=2))
        tm.close_ticket(self.conn, self.closed["ticket_id"], "B-1 resident", now=IN_MONTH + timedelta(hours=3))
        self.report = report_manager.monthly_report(self.conn, "2030-03", now=AS_OF)

    def test_12_counts(self):
        self.assertEqual(self.report["total_complaints"], 3)
        self.assertEqual(self.report["closed_tickets"], 1)
        self.assertEqual(self.report["open_tickets"], 2)
        self.assertEqual(self.report["overdue_tickets"], 1)

    def test_12_breakdowns(self):
        self.assertEqual(self.report["by_category"], {"Lift": 1, "Painting": 1, "Plumbing": 1})
        self.assertEqual(self.report["by_severity"], {"Low": 1, "Normal": 1, "Urgent": 1})

    def test_12_recurring_faults(self):
        faults = self.report["recurring_faults"]
        self.assertEqual([(f["block_name"], f["category"]) for f in faults], [("Block B", "Lift")])
        self.assertEqual(faults[0]["ticket_ids"], ["T-004", "T-012", "T-021"])

    def test_12_money(self):
        self.assertEqual(self.report["total_approved_spending"], 320000)
        expected = self.conn.execute("SELECT SUM(remaining_amount) AS n FROM budgets").fetchone()["n"]
        self.assertEqual(self.report["remaining_budget_total"], expected)

    def test_12_vendor_performance(self):
        self.assertEqual(len(self.report["vendor_performance"]), 1)
        row = self.report["vendor_performance"][0]
        self.assertEqual(
            (row["vendor_id"], row["jobs"], row["completed"], row["on_time"], row["total_cost"]),
            ("V-04", 1, 1, 1, 320000),
        )

    def test_seeded_month(self):
        report = report_manager.monthly_report(self.conn, "2026-11", now=AS_OF)
        self.assertEqual(report["total_complaints"], 4)  # T-014, T-028, T-029, T-030.
        self.assertEqual(report["closed_tickets"], 1)
        self.assertEqual(report["total_approved_spending"], 27000)  # T-028, EUR 270.

    def test_invalid_months_rejected(self):
        for month in ("2026-13", "2026-00", "November", "26-11", ""):
            with self.subTest(month=month), self.assertRaises(ValidationError):
                report_manager.monthly_report(self.conn, month)

    def test_december_rolls_over_to_the_next_year(self):
        self.assertEqual(report_manager.monthly_report(self.conn, "2029-12", now=AS_OF)["total_complaints"], 0)

    def test_cli_report_is_readable(self):
        result = self.run_cli("report", "--month", "2030-03")
        self.assertEqual(result.returncode, 0, result.stderr)
        for expected in ("Total complaints: 3", "Recurring maintenance faults", "Vendor performance", "EUR 3,200.00"):
            self.assertIn(expected, result.stdout)


if __name__ == "__main__":
    unittest.main()
