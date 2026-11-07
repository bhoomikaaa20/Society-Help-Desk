"""TEST 11: overdue identification and SLA calculation."""

import unittest
from datetime import timedelta, timezone

from helpers import FIXED_NOW, DeskTestCase

import ticket_manager as tm
import work_order_manager as wom
from common import from_iso


class Sla(DeskTestCase):
    def setUp(self):
        super().setUp()
        # Keep these tests independent of the seeded open tickets.
        self.conn.execute("UPDATE tickets SET status = 'CLOSED'")

    def new_ticket(self, severity, age_hours):
        return tm.create_ticket(
            self.conn, flat="A-1", category="Lift", severity=severity, text=f"{severity} {age_hours}",
            now=FIXED_NOW - timedelta(hours=age_hours),
        )

    def test_targets_for_each_severity(self):
        for severity, hours in (("Urgent", 4), ("Normal", 48), ("Low", 168)):
            with self.subTest(severity=severity):
                ticket = self.new_ticket(severity, 0)
                self.assertEqual(from_iso(ticket["due_at"]), FIXED_NOW + timedelta(hours=hours))

    def test_11_overdue_tickets_identified(self):
        late = self.new_ticket("Urgent", 6)
        self.new_ticket("Urgent", 1)  # Still inside its 4 hours.
        self.new_ticket("Normal", 6)  # Still inside its 48 hours.
        overdue = tm.list_overdue(self.conn, FIXED_NOW)
        self.assertEqual([row["ticket_id"] for row in overdue], [late["ticket_id"]])
        self.assertEqual(overdue[0]["time_overdue"], "2h 0m")
        for field in ("flat_number", "block_name", "severity", "created_at", "due_at"):
            self.assertTrue(overdue[0][field])

    def test_exactly_at_deadline_is_not_overdue(self):
        self.new_ticket("Urgent", 4)
        self.assertEqual(tm.list_overdue(self.conn, FIXED_NOW), [])

    def test_vendor_done_stays_overdue_until_resident_confirms(self):
        late = self.new_ticket("Urgent", 6)
        wom.create_work_order(self.conn, late["ticket_id"], "V-04", "100")
        tm.mark_vendor_done(self.conn, late["ticket_id"], "Fixed")
        self.assertEqual(len(tm.list_overdue(self.conn, FIXED_NOW)), 1)
        tm.close_ticket(self.conn, late["ticket_id"], "A-1 resident")
        self.assertEqual(tm.list_overdue(self.conn, FIXED_NOW), [])

    def test_timestamps_with_other_offsets_are_stored_as_utc(self):
        noon_in_india = FIXED_NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))
        ticket = tm.create_ticket(
            self.conn, flat="A-1", category="Lift", severity="Low", text="Offset test", now=noon_in_india
        )
        self.assertEqual(ticket["created_at"], "2030-01-15T12:00:00+00:00")
        self.assertEqual(ticket["due_at"], "2030-01-22T12:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
