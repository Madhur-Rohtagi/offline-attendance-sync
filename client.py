"""Offline attendance client. Run:  python client.py A   (or B)

Each client has its own SQLite file (client_A.db / client_B.db) and works
fully offline. "Synchronize" sends pending rows (synced = 0) to the server.
"""
import sys
import uuid
from datetime import date

import requests

from db import (connect, get_all_attendance, get_record, get_students, init_db,
                now_utc, save_record)

DEFAULT_SERVER_URL = "http://127.0.0.1:5000"
SYNC_FIELDS = ["student_id", "att_date", "status", "client_id", "update_id", "modified_at"]


def client_db_path(client_id):
    return f"client_{client_id}.db"


# ---------- local (offline) operations ----------

def mark_attendance(db_path, client_id, student_id, att_date, status, modified_at=None):
    """Save (or modify) attendance locally as a pending update. No network needed.

    modified_at can be passed explicitly (used by the demo to get 10:00 / 10:05).
    """
    if status not in ("Present", "Absent"):
        raise ValueError("status must be Present or Absent")
    conn = connect(db_path)
    try:
        if not any(s["student_id"] == student_id for s in get_students(conn)):
            raise ValueError(f"unknown student {student_id}")
        record = {
            "student_id": student_id,
            "att_date": att_date,
            "status": status,
            "client_id": client_id,
            "update_id": str(uuid.uuid4()),   # every change gets a new unique id
            "modified_at": modified_at or now_utc(),
        }
        with conn:
            save_record(conn, record, synced=0)   # 0 = pending until the server acks
        return record
    finally:
        conn.close()


def get_pending(db_path):
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM attendance WHERE synced = 0 ORDER BY att_date, student_id")]
    finally:
        conn.close()


# ---------- synchronization ----------

def sync(db_path, client_id, server_url=DEFAULT_SERVER_URL):
    """Send pending updates, then store the server's canonical records locally.

    Returns {"ok": bool, "message": str, "results": [...]}.
    Local data only changes after the server has acknowledged the sync, so on
    any failure the pending updates stay exactly as they were.
    """
    pending = get_pending(db_path)
    payload = {
        "client_id": client_id,
        "updates": [{f: row[f] for f in SYNC_FIELDS} for row in pending],
    }
    try:
        response = requests.post(f"{server_url}/sync", json=payload, timeout=5)
        body = response.json()
    except requests.RequestException as e:
        return {"ok": False, "results": [],
                "message": f"server unavailable ({type(e).__name__}); "
                           f"{len(pending)} update(s) kept as pending"}
    except ValueError:
        return {"ok": False, "results": [],
                "message": "server sent an invalid response; updates kept as pending"}

    if response.status_code != 200 or not body.get("ok"):
        return {"ok": False, "results": [],
                "message": f"server rejected sync: {body.get('error', response.status_code)}; "
                           f"{len(pending)} update(s) kept as pending"}

    # Server acknowledged: replace local rows with the canonical server versions.
    conn = connect(db_path)
    try:
        with conn:
            for record in body["records"]:
                save_record(conn, record, synced=1)
    finally:
        conn.close()
    return {"ok": True, "results": body["results"],
            "message": f"sent {len(pending)} update(s), received {len(body['records'])} record(s)"}


def print_sync_report(report):
    for r in report["results"]:
        who = f"student {r['student_id']} on {r['att_date']}"
        if r["outcome"] == "duplicate":
            print(f"  DUPLICATE  {who}: server already had this update, nothing changed")
        elif r["outcome"] == "incoming" and r["conflict"]:
            print(f"  CONFLICT   {who}: your {r['sent_status']} @ {r['sent_modified_at']} "
                  f"is newer -> WINS")
        elif r["outcome"] == "incoming":
            print(f"  ACCEPTED   {who}: {r['sent_status']} saved on server")
        else:
            print(f"  CONFLICT   {who}: your {r['sent_status']} @ {r['sent_modified_at']} LOST to "
                  f"{r['winner_status']} by {r['winner_client']} @ {r['winner_modified_at']}")
    print(("Synchronization completed: " if report["ok"] else "Synchronization FAILED: ")
          + report["message"])


# ---------- CLI ----------

def print_attendance(rows):
    if not rows:
        print("  (no attendance records)")
        return
    print(f"  {'Student':<8}{'Date':<12}{'Status':<9}{'By':<4}{'Modified (UTC)':<29}Sync")
    for r in rows:
        state = "synced" if r["synced"] else "PENDING"
        print(f"  {r['student_id']:<8}{r['att_date']:<12}{r['status']:<9}"
              f"{r['client_id']:<4}{r['modified_at']:<29}{state}")


def menu(client_id, server_url):
    db_path = client_db_path(client_id)
    init_db(db_path)
    while True:
        print(f"\n=== Client {client_id} ({db_path}) | pending updates: "
              f"{len(get_pending(db_path))} ===")
        print("1. View Students\n2. Mark / Modify Attendance\n3. View Attendance\n"
              "4. Synchronize with Server\n5. Exit")
        choice = input("Choose: ").strip()

        if choice == "1":
            conn = connect(db_path)
            for s in get_students(conn):
                print(f"  {s['student_id']}  {s['name']}")
            conn.close()

        elif choice == "2":
            try:
                student_id = int(input("Student ID: ").strip())
            except ValueError:
                print("Student ID must be a number.")
                continue
            att_date = input(f"Date [{date.today()}]: ").strip() or str(date.today())
            try:
                date.fromisoformat(att_date)
            except ValueError:
                print("Date must be YYYY-MM-DD.")
                continue
            letter = input("Status (P = Present, A = Absent): ").strip().upper()
            status = {"P": "Present", "A": "Absent"}.get(letter)
            if not status:
                print("Enter P or A.")
                continue
            conn = connect(db_path)
            old = get_record(conn, student_id, att_date)
            conn.close()
            try:
                rec = mark_attendance(db_path, client_id, student_id, att_date, status)
            except ValueError as e:
                print(f"Error: {e}")
                continue
            action = f"modified ({old['status']} -> {status})" if old else "saved"
            print(f"Attendance {action} locally at {rec['modified_at']} (pending sync).")

        elif choice == "3":
            conn = connect(db_path)
            print_attendance(get_all_attendance(conn))
            conn.close()

        elif choice == "4":
            print(f"Synchronization started with {server_url} ...")
            print_sync_report(sync(db_path, client_id, server_url))

        elif choice == "5":
            break
        else:
            print("Please choose 1-5.")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1].upper() not in ("A", "B"):
        print("Usage: python client.py A|B [server_url]")
        sys.exit(1)
    menu(sys.argv[1].upper(), sys.argv[2] if len(sys.argv) > 2 else DEFAULT_SERVER_URL)
