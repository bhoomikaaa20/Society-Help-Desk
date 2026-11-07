"""TESTS 5, 9 and 10: work order execution.

The approval itself happens in Switch before the command runs, so it cannot be
exercised here. TEST 5 covers what the backend does once Switch lets the command
run. The Allow once and Decline paths have to be checked live in Switch.
"""

import sqlite3
import unittest

from helpers import DeskTestCase

import ticket_manager as tm
import work_order_manager as wom
from common import NotFoundError, RuleViolation, ValidationError


class WorkOrders(DeskTestCase):
    def test_05_successful_execution_after_approval(self):
        result = self.run_cli("work-order", "T-014", "V-02", "--amount", "2500")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WO-001", result.stdout)

        order = self.conn.execute("SELECT * FROM work_orders WHERE ticket_id = 'T-014'").fetchone()
        self.assertEqual((order["vendor_id"], order["amount"], order["status"]), ("V-02", 250000, "APPROVED"))
        self.assertIsNotNone(order["approved_at"])

        budget = self.budget("Plumbing")
        self.assertEqual((budget["spent_amount"], budget["remaining_amount"]), (1000000, 0))

        ticket = tm.get_ticket(self.conn, "T-014")
        self.assertEqual((ticket["status"], ticket["vendor_id"]), ("WORK ORDER CREATED", "V-02"))

        last = tm.get_history(self.conn, "T-014")[-1]
        self.assertEqual((last["action"], last["cost"]), ("WORK_ORDER_CREATED", 250000))

    def test_09_invalid_vendor_rejected(self):
        with self.assertRaises(NotFoundError):
            wom.create_work_order(self.conn, "T-014", "V-99", "100")
        with self.assertRaises(ValidationError):
            wom.create_work_order(self.conn, "T-014", "not-a-vendor", "100")
        self.assertEqual(self.count("work_orders"), 0)

    def test_vendor_from_another_category_rejected(self):
        with self.assertRaises(RuleViolation):
            wom.create_work_order(self.conn, "T-014", "V-04", "100")
        self.assertEqual(self.count("work_orders"), 0)

    def test_invalid_ticket_rejected(self):
        with self.assertRaises(NotFoundError):
            wom.create_work_order(self.conn, "T-999", "V-02", "100")

    def test_closed_ticket_rejected(self):
        with self.assertRaises(RuleViolation):
            wom.create_work_order(self.conn, "T-001", "V-02", "100")

    def test_10_duplicate_work_order_prevented(self):
        wom.create_work_order(self.conn, "T-014", "V-02", "1000")
        spent = self.budget("Plumbing")["spent_amount"]
        with self.assertRaises(RuleViolation) as caught:
            wom.create_work_order(self.conn, "T-014", "V-01", "500")
        self.assertIn("Duplicate work order", str(caught.exception))
        self.assertEqual(self.count("work_orders"), 1)
        self.assertEqual(self.budget("Plumbing")["spent_amount"], spent)

    def test_database_also_refuses_a_second_active_work_order(self):
        wom.create_work_order(self.conn, "T-014", "V-02", "1000")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """INSERT INTO work_orders (work_order_id, ticket_id, vendor_id, amount, status, created_at)
                   VALUES ('WO-900', 'T-014', 'V-01', 100, 'APPROVED', '2030-01-01T00:00:00+00:00')"""
            )

    def test_failure_midway_rolls_back_every_change(self):
        # Make the final step, the history insert, fail after everything else ran.
        self.conn.execute(
            """CREATE TRIGGER fail_history BEFORE INSERT ON ticket_history
               BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"""
        )
        with self.assertRaises(sqlite3.IntegrityError):
            wom.create_work_order(self.conn, "T-014", "V-02", "2500")
        self.assertEqual(self.budget("Plumbing")["spent_amount"], 750000)
        self.assertEqual(self.count("work_orders"), 0)
        self.assertEqual(tm.get_ticket(self.conn, "T-014")["status"], "OPEN")


class EndToEndCli(DeskTestCase):
    def test_prd_section_15_commands_run_in_order(self):
        steps = [
            ("add", "--flat", "B-204", "--category", "plumbing", "--severity", "urgent",
             "--text", "Water dripping from bathroom ceiling"),
            ("history", "T-014"),
            ("vendor", "T-014"),
            ("budget",),
            ("work-order", "T-014", "V-02", "--amount", "2500"),
            ("update", "T-014", "vendor-done", "--note", "Pipe joint replaced"),
            ("close", "T-014", "--confirmed-by", "B-204 resident"),
            ("overdue",),
            ("report", "--month", "2026-11"),
            ("--help",),
        ]
        for step in steps:
            with self.subTest(step=step[0]):
                result = self.run_cli(*step)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout.strip())
        self.assertEqual(tm.get_ticket(self.conn, "T-014")["status"], "CLOSED")

    def test_prd_section_20_lift_scenario(self):
        steps = [
            ("add", "--block", "B", "--category", "lift", "--severity", "normal",
             "--text", "The lift in Block B stopped working again."),
            ("history", "T-031"),
            ("vendor", "T-031"),
            ("budget", "--category", "lift"),
            ("work-order", "T-031", "V-04", "--amount", "3200"),
            ("update", "T-031", "vendor-done", "--note", "Lift controller replaced"),
            ("close", "T-031", "--confirmed-by", "B-204 resident"),
        ]
        for step in steps:
            with self.subTest(step=step[0]):
                result = self.run_cli(*step)
                self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(tm.get_ticket(self.conn, "T-031")["status"], "CLOSED")

    def test_close_without_confirmation_flag_is_a_clean_rejection(self):
        self.run_cli("work-order", "T-014", "V-02", "--amount", "2500")
        self.run_cli("update", "T-014", "vendor-done", "--note", "Done")
        result = self.run_cli("close", "T-014")
        self.assertEqual(result.returncode, 1)
        self.assertIn("resident confirmation is missing", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
