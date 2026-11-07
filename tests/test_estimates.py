"""Repair estimates, resident status, delay reasons and declined work orders."""

import unittest
from datetime import datetime, timedelta, timezone

from helpers import FIXED_NOW, DeskTestCase

import ticket_manager as tm
import work_order_manager as wom
from common import RuleViolation, ValidationError, friendly_duration


class Estimates(DeskTestCase):
    def estimate_for(self, **details):
        ticket = tm.create_ticket(self.conn, text="Estimate test", now=FIXED_NOW, **details)
        return tm.estimate_repair(self.conn, ticket)

    def test_uses_past_repairs_with_the_same_category_and_severity(self):
        urgent = self.estimate_for(flat="A-1", category="Plumbing", severity="Urgent")
        self.assertEqual((urgent["label"], urgent["based_on"]), ("about 4 hours", 2))
        normal = self.estimate_for(flat="A-2", category="Plumbing", severity="Normal")
        self.assertEqual((normal["label"], normal["based_on"]), ("about 2 days", 3))
        painting = self.estimate_for(flat="A-3", category="Painting", severity="Low")
        self.assertEqual(painting["label"], "about 5 days")

    def test_no_past_repairs_falls_back_to_the_target(self):
        estimate = self.estimate_for(flat="A-1", category="Painting", severity="Urgent")
        self.assertEqual((estimate["hours"], estimate["based_on"]), (4, 0))

    def test_estimate_never_exceeds_the_target(self):
        self.conn.execute(
            """UPDATE ticket_history SET created_at = '2026-06-01T00:00:00+00:00'
               WHERE ticket_id IN ('T-001', 'T-011') AND action = 'HISTORICAL_REPAIR'"""
        )
        estimate = self.estimate_for(flat="A-1", category="Plumbing", severity="Urgent")
        self.assertEqual(estimate["hours"], 4)

    def test_estimate_stays_the_same_after_later_repairs(self):
        ticket = tm.create_ticket(
            self.conn, flat="A-1", category="Painting", severity="Low", text="Peeling",
            now=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )
        # Painting repairs in the seed data all finished after this date.
        self.assertEqual(tm.estimate_repair(self.conn, ticket)["based_on"], 0)

    def test_cli_add_shows_the_estimate(self):
        result = self.run_cli(
            "add", "--block", "B", "--category", "lift", "--severity", "normal", "--text", "Lift stopped"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Estimated repair time: about 2 days", result.stdout)


class Status(DeskTestCase):
    def setUp(self):
        super().setUp()
        self.ticket = tm.create_ticket(
            self.conn, flat="B-1", category="Lift", severity="Normal", text="Lift stopped", now=FIXED_NOW
        )
        self.ticket_id = self.ticket["ticket_id"]

    def test_on_time_then_late(self):
        self.assertFalse(tm.get_status(self.conn, self.ticket_id, FIXED_NOW)["is_late"])
        later = FIXED_NOW + timedelta(days=3)
        self.assertTrue(tm.get_status(self.conn, self.ticket_id, later)["is_late"])

    def test_delay_reason_is_shown(self):
        tm.record_delay(self.conn, self.ticket_id, "Waiting for a spare part")
        self.assertEqual(tm.get_status(self.conn, self.ticket_id)["delay_reason"], "Waiting for a spare part")

    def test_delay_needs_a_reason(self):
        with self.assertRaises(ValidationError):
            tm.record_delay(self.conn, self.ticket_id, "  ")

    def test_closed_ticket_cannot_get_a_delay_and_is_not_late(self):
        with self.assertRaises(RuleViolation):
            tm.record_delay(self.conn, "T-001", "Too late")
        self.assertFalse(tm.get_status(self.conn, "T-001")["is_late"])

    def test_cli_status_hides_money_and_vendors(self):
        wom.create_work_order(self.conn, self.ticket_id, "V-04", "3200")
        result = self.run_cli("status", self.ticket_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Repair arranged", result.stdout)
        for private in ("EUR", "V-04", "Summit"):
            self.assertNotIn(private, result.stdout)

    def test_cli_late_ticket_with_no_reason_says_to_ask_the_manager(self):
        result = self.run_cli("status", "T-014")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Running late:   YES", result.stdout)
        self.assertIn("ask the manager", result.stdout)


class ClosingMessage(DeskTestCase):
    def test_reporter_is_saved_and_cleaned(self):
        ticket = tm.create_ticket(
            self.conn, block="B", category="Lift", severity="Normal", text="Lift stopped",
            reported_by="@Bhoomi\n", now=FIXED_NOW,
        )
        self.assertEqual(tm.get_reported_by(self.conn, ticket["ticket_id"]), "Bhoomi")

    def test_reporter_is_optional(self):
        ticket = tm.create_ticket(self.conn, block="B", category="Lift", severity="Normal", text="Lift stopped")
        self.assertIsNone(tm.get_reported_by(self.conn, ticket["ticket_id"]))

    def test_friendly_duration(self):
        for delta, expected in (
            (timedelta(minutes=20), "less than 1 hour"),
            (timedelta(hours=1), "about 1 hour"),
            (timedelta(hours=3, minutes=20), "about 3 hours"),
            (timedelta(hours=26), "about 1 day"),
            (timedelta(hours=50), "about 2 days"),
        ):
            with self.subTest(delta=delta):
                self.assertEqual(friendly_duration(delta), expected)

    def test_time_taken_runs_from_complaint_to_closure(self):
        ticket = tm.create_ticket(
            self.conn, block="B", category="Lift", severity="Normal", text="Lift stopped", now=FIXED_NOW
        )
        ticket_id = ticket["ticket_id"]
        wom.create_work_order(self.conn, ticket_id, "V-04", "3200", now=FIXED_NOW)
        tm.mark_vendor_done(self.conn, ticket_id, "Fixed", now=FIXED_NOW + timedelta(hours=20))
        closed = tm.close_ticket(self.conn, ticket_id, "B-204 resident", now=FIXED_NOW + timedelta(hours=26))
        self.assertEqual(tm.time_taken(closed), "about 1 day")

    def test_cli_close_shows_reporter_and_time_taken(self):
        steps = (
            ("add", "--block", "B", "--category", "lift", "--severity", "normal",
             "--text", "Lift stopped", "--reported-by", "Bhoomi"),
            ("work-order", "T-031", "V-04", "--amount", "3200"),
            ("update", "T-031", "vendor-done", "--note", "Controller replaced"),
        )
        for step in steps:
            self.assertEqual(self.run_cli(*step).returncode, 0)
        result = self.run_cli("close", "T-031", "--confirmed-by", "B-204 resident")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Reported by:  Bhoomi", result.stdout)
        self.assertIn("Time taken:   less than 1 hour", result.stdout)


class Declines(DeskTestCase):
    def test_decline_keeps_ticket_open_and_spends_nothing(self):
        spent = self.budget("Plumbing")["spent_amount"]
        ticket = tm.record_decline(self.conn, "T-014", "Quote too high", "Getting a cheaper quote")
        self.assertEqual(ticket["status"], "OPEN")
        self.assertEqual(self.budget("Plumbing")["spent_amount"], spent)
        self.assertEqual(self.count("work_orders"), 0)
        status = tm.get_status(self.conn, "T-014")
        self.assertEqual(status["decline_reason"], "Quote too high")
        self.assertEqual(status["next_step"], "Getting a cheaper quote")

    def test_decline_needs_a_reason_and_a_next_step(self):
        for reason, next_step in ((None, "Next"), ("Reason", None), ("  ", "Next"), ("Reason", "  ")):
            with self.subTest(reason=reason, next_step=next_step), self.assertRaises(ValidationError):
                tm.record_decline(self.conn, "T-014", reason, next_step)
        self.assertEqual(tm.get_status(self.conn, "T-014")["decline_reason"], None)

    def test_new_work_order_can_follow_a_decline(self):
        tm.record_decline(self.conn, "T-014", "Quote too high", "Trying another vendor")
        wom.create_work_order(self.conn, "T-014", "V-01", "1800")
        status = tm.get_status(self.conn, "T-014")
        self.assertEqual(status["ticket"]["status"], "WORK ORDER CREATED")
        self.assertIsNone(status["decline_reason"])

    def test_only_open_tickets_can_be_declined(self):
        with self.assertRaises(RuleViolation):
            tm.record_decline(self.conn, "T-001", "Reason", "Next")

    def test_cli_decline(self):
        missing = self.run_cli("update", "T-014", "declined", "--note", "Quote too high")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("Next step", missing.stderr)
        done = self.run_cli(
            "update", "T-014", "declined", "--note", "Quote too high", "--next-step", "Getting a cheaper quote"
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Next step:      Getting a cheaper quote", self.run_cli("status", "T-014").stdout)


if __name__ == "__main__":
    unittest.main()
