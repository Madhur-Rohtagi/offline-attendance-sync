"""Conflict resolution (Last-Write-Wins) and validation used by the server."""
from datetime import datetime

from db import TIMESTAMP_FORMAT, get_record, save_record

REQUIRED_FIELDS = ["student_id", "att_date", "status", "client_id",
                   "update_id", "modified_at"]


def decide_winner(existing, incoming):
    """Last-Write-Wins between the stored record and an incoming update.

    Returns:
      "duplicate" - the server already has this exact update (same update_id)
      "incoming"  - the incoming update is newer, so it replaces the stored one
      "existing"  - the stored record is newer (or wins the tie), so keep it

    The newest modified_at wins. If two timestamps are exactly equal, the tie
    is broken by client_id and then update_id (the larger string wins), so every
    machine always picks the same winner.
    """
    if existing is None:
        return "incoming"
    if existing["update_id"] == incoming["update_id"]:
        return "duplicate"

    existing_key = (existing["modified_at"], existing["client_id"], existing["update_id"])
    incoming_key = (incoming["modified_at"], incoming["client_id"], incoming["update_id"])
    return "incoming" if incoming_key > existing_key else "existing"


def validate_update(update, known_student_ids):
    """Return an error message, or None if the update is valid."""
    if not isinstance(update, dict):
        return "each update must be a JSON object"
    missing = [f for f in REQUIRED_FIELDS if f not in update]
    if missing:
        return f"missing fields: {', '.join(missing)}"

    student_id = update["student_id"]
    if not isinstance(student_id, int) or isinstance(student_id, bool):
        return "student_id must be an integer"
    if student_id not in known_student_ids:
        return f"unknown student_id {student_id}"
    if update["status"] not in ("Present", "Absent"):
        return "status must be 'Present' or 'Absent'"
    for field in ("client_id", "update_id", "att_date", "modified_at"):
        if not isinstance(update[field], str) or not update[field].strip():
            return f"{field} must be a non-empty string"
    try:
        datetime.strptime(update["att_date"], "%Y-%m-%d")
    except ValueError:
        return "att_date must look like YYYY-MM-DD"
    try:
        datetime.strptime(update["modified_at"], TIMESTAMP_FORMAT)
    except ValueError:
        return "modified_at must be UTC like 2026-11-20T10:05:00.000000Z"
    return None


def apply_update(conn, update):
    """Apply one incoming update to the server DB using LWW.

    Must be called inside a transaction. Returns a result dict for the client.
    """
    # Guard: an update_id must never be reused for a different record,
    # otherwise INSERT OR REPLACE would silently delete that other row.
    clash = conn.execute(
        "SELECT student_id, att_date FROM attendance WHERE update_id = ?",
        (update["update_id"],),
    ).fetchone()
    if clash and (clash["student_id"], clash["att_date"]) != (update["student_id"], update["att_date"]):
        raise ValueError(f"update_id {update['update_id']} already used for another record")

    existing = get_record(conn, update["student_id"], update["att_date"])
    outcome = decide_winner(existing, update)

    if outcome == "incoming":
        save_record(conn, update, synced=1)
        winner = update
    else:
        winner = existing

    return {
        "update_id": update["update_id"],
        "student_id": update["student_id"],
        "att_date": update["att_date"],
        "outcome": outcome,
        # True when two different versions of the same record met on the server
        "conflict": existing is not None and outcome != "duplicate",
        "sent_status": update["status"],
        "sent_modified_at": update["modified_at"],
        "winner_status": winner["status"],
        "winner_client": winner["client_id"],
        "winner_modified_at": winner["modified_at"],
    }
