"""TEST 3: recurring fault detection, plus repeatable CSV import."""

import unittest
from datetime import timedelta

from helpers import FIXED_NOW, DeskTestCase

import database
import ticket_manager as tm


class RecurringFaults(DeskTestCase):
    def test_03_block_b_lift_failures_detected(self):
        ticket = tm.create_ticket(
            self.conn, block="B", category="lift", severity="normal",
            text="The lift in Block B stopped working again.",
        )
        analysis = tm.find_related_incidents(self.conn, ticket["ticket_id"])
        self.assertEqual(analysis["previous_incident_count"], 3)
        self.assertEqual(analysis["ticket_ids"], ["T-004", "T-012", "T-021"])
        self.assertEqual([i["cost"] for i in analysis["incidents"]], [215000, 278000, 324000])
        self.assertEqual(analysis["total_cost"], 817000)
        self.assertTrue(analysis["is_recurring"])

    def test_other_blocks_are_not_counted(self):
        ticket = tm.create_ticket(self.conn, block="D", category="Lift", severity="Normal", text="Lift stuck")
        analysis = tm.find_related_incidents(self.conn, ticket["ticket_id"])
        self.assertEqual(analysis["previous_incident_count"], 0)
        self.assertFalse(analysis["is_recurring"])

    def test_non_asset_categories_need_a_similar_description(self):
        similar = tm.create_ticket(self.conn, flat="B-1", category="Plumbing", severity="Normal", text="Blocked drain")
        different = tm.create_ticket(self.conn, flat="B-2", category="Plumbing", severity="Normal", text="Tap handle loose")
        self.assertIn("T-007", tm.find_related_incidents(self.conn, similar["ticket_id"])["ticket_ids"])
        self.assertNotIn("T-007", tm.find_related_incidents(self.conn, different["ticket_id"])["ticket_ids"])

    def test_shared_location_word_is_not_a_similar_fault(self):
        # T-007 is a blocked bathroom drain. A bathroom leak is a different fault.
        ticket = tm.create_ticket(
            self.conn, flat="B-3", category="Plumbing", severity="Urgent",
            text="Water dripping from bathroom ceiling",
        )
        self.assertNotIn("T-007", tm.find_related_incidents(self.conn, ticket["ticket_id"])["ticket_ids"])

    def test_later_tickets_are_not_previous_incidents(self):
        early = tm.create_ticket(
            self.conn, block="E", category="Lift", severity="Low", text="First", now=FIXED_NOW
        )
        tm.create_ticket(
            self.conn, block="E", category="Lift", severity="Low", text="Second",
            now=FIXED_NOW + timedelta(days=1),
        )
        self.assertEqual(tm.find_related_incidents(self.conn, early["ticket_id"])["previous_incident_count"], 0)

    def test_cli_history_shows_the_incidents(self):
        created = self.run_cli(
            "add", "--block", "B", "--category", "lift", "--severity", "normal", "--text", "Lift stopped again"
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        result = self.run_cli("history", "T-031")
        self.assertEqual(result.returncode, 0, result.stderr)
        for expected in ("Previous incidents:    3", "T-004, T-012, T-021", "EUR 8,170.00", "Recurring issue:       YES"):
            self.assertIn(expected, result.stdout)


class CsvImport(DeskTestCase):
    def test_seed_data_shape(self):
        self.assertEqual(self.count("tickets"), 30)
        lifts = self.conn.execute(
            "SELECT COUNT(*) AS n FROM tickets WHERE block_name = 'Block B' AND category = 'Lift'"
        ).fetchone()["n"]
        self.assertEqual(lifts, 3)

    def test_historical_costs_fit_within_recorded_spending(self):
        rows = self.conn.execute(
            """SELECT t.category, SUM(h.cost) AS total FROM ticket_history h
               JOIN tickets t ON t.ticket_id = h.ticket_id GROUP BY t.category"""
        ).fetchall()
        for row in rows:
            with self.subTest(category=row["category"]):
                self.assertLessEqual(row["total"], self.budget(row["category"])["spent_amount"])

    def test_reimport_duplicates_nothing_and_keeps_live_data(self):
        tm.create_ticket(self.conn, flat="A-1", category="Lift", severity="Low", text="Keep me")
        self.conn.execute("UPDATE budgets SET spent_amount = 800000, remaining_amount = 200000 WHERE category = 'Plumbing'")
        history_before = self.count("ticket_history")

        summary = database.import_csv_data(self.conn)

        self.assertEqual(summary["past_tickets"], (0, 30))
        self.assertEqual(self.count("tickets"), 31)
        self.assertEqual(self.count("ticket_history"), history_before)
        self.assertEqual(self.budget("Plumbing")["spent_amount"], 800000)

    def test_reset_restores_the_demo_state(self):
        tm.create_ticket(self.conn, flat="A-1", category="Lift", severity="Low", text="Remove me")
        database.reset_demo_data(self.conn)
        database.reset_demo_data(self.conn)
        self.assertEqual(self.count("tickets"), 30)
        self.assertEqual(self.count("work_orders"), 0)

    def test_cli_reset_needs_explicit_confirmation(self):
        tm.create_ticket(self.conn, flat="A-1", category="Lift", severity="Low", text="Keep me")
        result = self.run_cli("init", "--reset")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.count("tickets"), 31)


if __name__ == "__main__":
    unittest.main()
