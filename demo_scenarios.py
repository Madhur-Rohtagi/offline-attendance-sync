"""Runs the three required scenarios end to end and checks the real databases.

    python demo_scenarios.py

Uses fresh database files in demo_data/, a real Flask server (in a background
thread) and the real client functions over HTTP. Nothing is hard-coded as
"expected output": every table is read back from SQLite, and the PASS/FAIL
lines compare the actual database contents.
"""
import os

import requests

from client import SYNC_FIELDS, get_pending, mark_attendance, print_sync_report, sync
from db import connect, get_all_attendance, init_db
from server import start_background_server

DEMO_DIR = "demo_data"
DAY = "2026-11-20"
A_DB = os.path.join(DEMO_DIR, "client_A.db")
B_DB = os.path.join(DEMO_DIR, "client_B.db")
SERVER_DB = os.path.join(DEMO_DIR, "server.db")


def ts(hh_mm):
    """'10:05' -> '2026-11-20T10:05:00.000000Z' (UTC)."""
    return f"{DAY}T{hh_mm}:00.000000Z"


def fresh_databases():
    os.makedirs(DEMO_DIR, exist_ok=True)
    for path in (A_DB, B_DB, SERVER_DB):
        if os.path.exists(path):
            os.remove(path)
        init_db(path)


def show(title, db_path):
    conn = connect(db_path)
    rows = get_all_attendance(conn)
    conn.close()
    print(f"\n  -- {title} --")
    if not rows:
        print("     (empty)")
    for r in rows:
        state = "synced" if r["synced"] else "PENDING"
        print(f"     student {r['student_id']}  {r['att_date']}  {r['status']:<8} "
              f"by {r['client_id']}  @ {r['modified_at']}  {state}")
    return rows


def show_all(label):
    print(f"\n[{label}]")
    return {name: show(name, path) for name, path in
            (("Server DB", SERVER_DB), ("Client A DB", A_DB), ("Client B DB", B_DB))}


def do_sync(client_id, db_path, url):
    print(f"\n>>> Client {client_id} synchronizes ({len(get_pending(db_path))} pending)")
    report = sync(db_path, client_id, url)
    print_sync_report(report)
    return report


def status_of(rows, student_id):
    return [r["status"] for r in rows if r["student_id"] == student_id]


def check(name, condition):
    print(f"  {'PASS' if condition else 'FAIL'}: {name}")
    return condition


def all_dbs_identical(tables):
    """Same canonical rows everywhere (ignoring nothing - synced flag included)."""
    return tables["Server DB"] == tables["Client A DB"] == tables["Client B DB"]


# ---------------------------------------------------------------------------

def scenario_1(url):
    print("\n" + "=" * 78 + "\nSCENARIO 1: Non-conflicting updates\n" + "=" * 78)
    fresh_databases()
    show_all("Initial state")

    print("\nUpdates (made offline):")
    print(f"  Client A: student 101 Present @ {ts('09:00')}")
    mark_attendance(A_DB, "A", 101, DAY, "Present", ts("09:00"))
    print(f"  Client B: student 102 Absent  @ {ts('09:01')}")
    mark_attendance(B_DB, "B", 102, DAY, "Absent", ts("09:01"))
    show_all("Local states before sync")

    do_sync("A", A_DB, url)
    do_sync("B", B_DB, url)
    do_sync("A", A_DB, url)  # A pulls B's record
    final = show_all("Final state")

    print("\nVerification:")
    return all([
        check("server has 101 Present", status_of(final["Server DB"], 101) == ["Present"]),
        check("server has 102 Absent", status_of(final["Server DB"], 102) == ["Absent"]),
        check("exactly 2 rows on server", len(final["Server DB"]) == 2),
        check("server, A and B are identical", all_dbs_identical(final)),
    ])


def scenario_2(url):
    print("\n" + "=" * 78 + "\nSCENARIO 2: Conflicting updates (same student, same day)\n" + "=" * 78)
    fresh_databases()
    show_all("Initial state")

    print("\nUpdates (made offline):")
    print(f"  Client A: student 101 Present @ {ts('10:00')}")
    mark_attendance(A_DB, "A", 101, DAY, "Present", ts("10:00"))
    print(f"  Client B: student 101 Absent  @ {ts('10:05')}")
    mark_attendance(B_DB, "B", 101, DAY, "Absent", ts("10:05"))
    show_all("Local states before sync")

    do_sync("A", A_DB, url)   # older update arrives first: accepted for now
    do_sync("B", B_DB, url)   # newer update arrives: conflict, B wins
    do_sync("A", A_DB, url)   # A receives the winning record
    final = show_all("Final state")

    print("\nVerification:")
    return all([
        check("server has 101 Absent (10:05 wins)", status_of(final["Server DB"], 101) == ["Absent"]),
        check("winner is B's update", final["Server DB"][0]["client_id"] == "B"),
        check("exactly 1 row on server", len(final["Server DB"]) == 1),
        check("server, A and B are identical", all_dbs_identical(final)),
    ])


def scenario_3(url):
    print("\n" + "=" * 78 + "\nSCENARIO 3: Out-of-order and repeated synchronization\n" + "=" * 78)
    fresh_databases()
    show_all("Initial state")

    print("\nUpdates (made offline):")
    print(f"  Client A: student 103 Absent  @ {ts('11:00')}  (older)")
    old_update = mark_attendance(A_DB, "A", 103, DAY, "Absent", ts("11:00"))
    print(f"  Client B: student 103 Present @ {ts('11:30')}  (newer)")
    new_update = mark_attendance(B_DB, "B", 103, DAY, "Present", ts("11:30"))
    show_all("Local states before sync")

    do_sync("B", B_DB, url)   # NEWER update reaches the server first
    do_sync("A", A_DB, url)   # OLDER update arrives later -> must be rejected
    show_all("After out-of-order sync")

    print("\n>>> Repeated synchronization (both clients sync twice more)")
    for _ in range(2):
        do_sync("A", A_DB, url)
        do_sync("B", B_DB, url)

    print("\n>>> Duplicate delivery: both original updates are re-sent, as if replies were lost")
    resent = {}
    for client_id, update in (("B", new_update), ("A", old_update)):
        payload = {"client_id": client_id, "updates": [{f: update[f] for f in SYNC_FIELDS}]}
        resent[client_id] = requests.post(f"{url}/sync", json=payload, timeout=5).json()["results"][0]
        r = resent[client_id]
        print(f"  re-sent {client_id}'s update -> outcome '{r['outcome']}' "
              f"(winner still {r['winner_status']} by {r['winner_client']})")

    final = show_all("Final state")
    print("\nVerification:")
    return all([
        check("older update did not overwrite: 103 is Present",
              status_of(final["Server DB"], 103) == ["Present"]),
        check("no duplicate rows: exactly 1 row on server", len(final["Server DB"]) == 1),
        check("re-sent winning update detected as duplicate", resent["B"]["outcome"] == "duplicate"),
        check("re-sent old update was not applied", resent["A"]["winner_status"] == "Present"),
        check("server, A and B are identical", all_dbs_identical(final)),
    ])


def main():
    os.makedirs(DEMO_DIR, exist_ok=True)
    init_db(SERVER_DB)
    server, url = start_background_server(SERVER_DB)
    try:
        results = [scenario_1(url), scenario_2(url), scenario_3(url)]
    finally:
        server.shutdown()
    print("\n" + "=" * 78)
    for i, ok in enumerate(results, 1):
        print(f"Scenario {i}: {'PASS' if ok else 'FAIL'}")
    raise SystemExit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
