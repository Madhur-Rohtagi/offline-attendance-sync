"""Automated tests. Run from the project folder:

    python -m unittest discover -v tests

Every test uses fresh temporary databases and a real Flask server over HTTP.
"""
import os
import shutil
import sys
import tempfile
import unittest

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from client import SYNC_FIELDS, get_pending, mark_attendance, sync  # noqa: E402
from db import connect, get_all_attendance, init_db  # noqa: E402
from server import start_background_server  # noqa: E402
from sync_logic import decide_winner  # noqa: E402

DAY = "2026-11-20"


def ts(hh_mm):
    return f"{DAY}T{hh_mm}:00.000000Z"


def rows(db_path):
    conn = connect(db_path)
    try:
        return get_all_attendance(conn)
    finally:
        conn.close()


class LWWUnitTests(unittest.TestCase):
    """decide_winner() on its own, no database or network."""

    def rec(self, modified_at, client_id, update_id):
        return {"modified_at": modified_at, "client_id": client_id, "update_id": update_id}

    def test_no_existing_record_accepts_incoming(self):
        self.assertEqual(decide_winner(None, self.rec(ts("10:00"), "A", "u1")), "incoming")

    def test_newer_wins_older_loses(self):
        old, new = self.rec(ts("10:00"), "A", "u1"), self.rec(ts("10:05"), "B", "u2")
        self.assertEqual(decide_winner(old, new), "incoming")
        self.assertEqual(decide_winner(new, old), "existing")

    def test_same_update_id_is_duplicate(self):
        r = self.rec(ts("10:00"), "A", "u1")
        self.assertEqual(decide_winner(r, dict(r)), "duplicate")

    def test_equal_timestamp_tie_break_is_deterministic(self):
        a, b = self.rec(ts("10:00"), "A", "u1"), self.rec(ts("10:00"), "B", "u2")
        # B > A, so B wins regardless of arrival order
        self.assertEqual(decide_winner(a, b), "incoming")
        self.assertEqual(decide_winner(b, a), "existing")

    def test_timestamp_beats_client_id(self):
        a_late, b_early = self.rec(ts("10:01"), "A", "u1"), self.rec(ts("10:00"), "B", "u2")
        self.assertEqual(decide_winner(a_late, b_early), "existing")


class SyncIntegrationTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.a_db = os.path.join(self.tmp, "client_A.db")
        self.b_db = os.path.join(self.tmp, "client_B.db")
        self.server_db = os.path.join(self.tmp, "server.db")
        for path in (self.a_db, self.b_db):
            init_db(path)
        self.server, self.url = start_background_server(self.server_db)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- offline behaviour ---

    def test_offline_marking_needs_no_server_and_persists(self):
        self.server.shutdown()
        self.server.server_close()
        mark_attendance(self.a_db, "A", 101, DAY, "Present")
        mark_attendance(self.b_db, "B", 101, DAY, "Absent")
        # reopen the files: data is still there and the two clients are independent
        self.assertEqual([r["status"] for r in rows(self.a_db)], ["Present"])
        self.assertEqual([r["status"] for r in rows(self.b_db)], ["Absent"])
        self.assertEqual(len(get_pending(self.a_db)), 1)
        self.server, self.url = start_background_server(self.server_db)  # for tearDown

    def test_modifying_offline_keeps_one_row(self):
        mark_attendance(self.a_db, "A", 101, DAY, "Present", ts("10:00"))
        mark_attendance(self.a_db, "A", 101, DAY, "Absent", ts("10:02"))
        self.assertEqual([r["status"] for r in rows(self.a_db)], ["Absent"])

    def test_server_unavailable_keeps_pending(self):
        mark_attendance(self.a_db, "A", 101, DAY, "Present")
        self.server.shutdown()
        self.server.server_close()
        report = sync(self.a_db, "A", self.url)
        self.assertFalse(report["ok"])
        self.assertIn("unavailable", report["message"])
        pending = get_pending(self.a_db)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["synced"], 0)
        # once the server is back, the same pending update syncs fine
        self.server, self.url = start_background_server(self.server_db)
        self.assertTrue(sync(self.a_db, "A", self.url)["ok"])
        self.assertEqual(get_pending(self.a_db), [])

    # --- scenarios ---

    def test_non_conflicting_updates_are_both_kept(self):
        mark_attendance(self.a_db, "A", 101, DAY, "Present", ts("09:00"))
        mark_attendance(self.b_db, "B", 102, DAY, "Absent", ts("09:01"))
        sync(self.a_db, "A", self.url)
        sync(self.b_db, "B", self.url)
        sync(self.a_db, "A", self.url)
        server = rows(self.server_db)
        self.assertEqual([(r["student_id"], r["status"]) for r in server],
                         [(101, "Present"), (102, "Absent")])
        self.assertEqual(rows(self.a_db), server)
        self.assertEqual(rows(self.b_db), server)

    def test_conflict_later_timestamp_wins(self):
        mark_attendance(self.a_db, "A", 101, DAY, "Present", ts("10:00"))
        mark_attendance(self.b_db, "B", 101, DAY, "Absent", ts("10:05"))
        sync(self.a_db, "A", self.url)
        report = sync(self.b_db, "B", self.url)
        self.assertEqual(report["results"][0]["outcome"], "incoming")
        self.assertTrue(report["results"][0]["conflict"])
        sync(self.a_db, "A", self.url)
        for db_path in (self.server_db, self.a_db, self.b_db):
            self.assertEqual([r["status"] for r in rows(db_path)], ["Absent"])

    def test_older_update_arriving_later_does_not_overwrite(self):
        mark_attendance(self.a_db, "A", 101, DAY, "Present", ts("10:00"))
        mark_attendance(self.b_db, "B", 101, DAY, "Absent", ts("10:05"))
        sync(self.b_db, "B", self.url)          # newer first
        report = sync(self.a_db, "A", self.url)  # older later
        self.assertEqual(report["results"][0]["outcome"], "existing")
        self.assertEqual([r["status"] for r in rows(self.server_db)], ["Absent"])
        self.assertEqual([r["status"] for r in rows(self.a_db)], ["Absent"])
        self.assertEqual(get_pending(self.a_db), [])  # acknowledged, even though it lost

    def test_repeated_sync_is_stable(self):
        mark_attendance(self.a_db, "A", 101, DAY, "Present", ts("10:00"))
        mark_attendance(self.b_db, "B", 101, DAY, "Absent", ts("10:05"))
        for _ in range(3):
            sync(self.a_db, "A", self.url)
            sync(self.b_db, "B", self.url)
        server = rows(self.server_db)
        self.assertEqual(len(server), 1)
        self.assertEqual(server[0]["status"], "Absent")
        self.assertEqual(rows(self.a_db), server)
        self.assertEqual(rows(self.b_db), server)

    def test_duplicate_update_is_ignored(self):
        update = mark_attendance(self.a_db, "A", 101, DAY, "Present", ts("10:00"))
        payload = {"client_id": "A", "updates": [{f: update[f] for f in SYNC_FIELDS}]}
        first = requests.post(f"{self.url}/sync", json=payload, timeout=5).json()
        second = requests.post(f"{self.url}/sync", json=payload, timeout=5).json()
        self.assertEqual(first["results"][0]["outcome"], "incoming")
        self.assertEqual(second["results"][0]["outcome"], "duplicate")
        self.assertEqual(len(rows(self.server_db)), 1)

    # --- invalid input ---

    def post(self, body):
        return requests.post(f"{self.url}/sync", json=body, timeout=5)

    def valid_update(self, **changes):
        update = {"student_id": 101, "att_date": DAY, "status": "Present",
                  "client_id": "A", "update_id": "u-1", "modified_at": ts("10:00")}
        update.update(changes)
        return update

    def test_invalid_payloads_are_rejected_and_change_nothing(self):
        bad_updates = [
            self.valid_update(status="Late"),
            self.valid_update(att_date="20-11-2026"),
            self.valid_update(student_id=999),
            self.valid_update(student_id="101"),
            self.valid_update(modified_at="10:00"),
            {k: v for k, v in self.valid_update().items() if k != "update_id"},
        ]
        for bad in bad_updates:
            # a valid update in the same batch must NOT be saved either (all-or-nothing)
            body = {"client_id": "A", "updates": [self.valid_update(update_id="ok-1"), bad]}
            response = self.post(body)
            self.assertEqual(response.status_code, 400, bad)
            self.assertFalse(response.json()["ok"])
        self.assertEqual(self.post({"updates": []}).status_code, 400)       # no client_id
        self.assertEqual(self.post({"client_id": "A"}).status_code, 400)    # no updates
        self.assertEqual(requests.post(f"{self.url}/sync", data="not json",
                                       timeout=5).status_code, 400)
        self.assertEqual(rows(self.server_db), [])

    def test_reused_update_id_for_other_record_is_rejected(self):
        self.post({"client_id": "A", "updates": [self.valid_update(update_id="same")]})
        response = self.post({"client_id": "A", "updates": [
            self.valid_update(update_id="same", student_id=102, modified_at=ts("11:00"))]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual([r["student_id"] for r in rows(self.server_db)], [101])

    def test_client_keeps_pending_when_server_rejects(self):
        mark_attendance(self.a_db, "A", 101, DAY, "Present", ts("10:00"))
        conn = connect(self.a_db)
        with conn:  # corrupt the local row so the server will reject it
            conn.execute("UPDATE attendance SET modified_at = 'yesterday'")
        conn.close()
        report = sync(self.a_db, "A", self.url)
        self.assertFalse(report["ok"])
        self.assertEqual(len(get_pending(self.a_db)), 1)


if __name__ == "__main__":
    unittest.main()
