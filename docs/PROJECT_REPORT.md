# Project Report: Offline Attendance Sync

**Group:** 15  **Technique:** Synchronization with Conflict Resolution (Last-Write-Wins)
**Stack:** Python 3, Flask, SQLite (`sqlite3`), Requests

## 1. Introduction
In many classrooms the network is unreliable. An attendance app that needs a live server fails exactly when it is needed most. An **offline-first** app saves data locally first and synchronizes later. When two devices change the same record while offline, the system needs a clear rule to decide which change is kept. This project implements that with **Last-Write-Wins (LWW)**.

## 2. Problem statement
Two SQLite clients mark attendance offline and synchronize through a Flask server. Conflicts are resolved with Last-Write-Wins. The expected outcome is that three conflict scenarios merge and resolve correctly.

## 3. Objectives
1. Record attendance on two independent clients without a network.
2. Keep unsynchronized changes safely as pending updates until the server acknowledges them.
3. Synchronize over HTTP with a central Flask server.
4. Resolve conflicting updates with LWW, deterministically, including equal timestamps.
5. Make repeated, duplicate and out-of-order syncs safe.
6. End with identical data on the server and both clients.

## 4. Existing problem and proposed solution
| Existing (online-only) approach | Proposed system |
|---|---|
| Marking fails when the server is down | Every mark is saved to the client's own SQLite file first |
| Two teachers editing the same record: the last request processed silently wins, whatever its actual time | The server compares **when** each change was made (`modified_at`) and keeps the newest |
| Network retries can create duplicate rows | `PRIMARY KEY (student_id, att_date)` plus a unique `update_id` per change |

## 5. System architecture
```
+--------------------+                          +--------------------+
| Client A (CLI)     |                          | Client B (CLI)     |
| client.py A        |                          | client.py B        |
| client_A.db        |                          | client_B.db        |
|  students          |                          |  students          |
|  attendance        |                          |  attendance        |
|  (synced=0 means   |                          |  (synced=0 means   |
|   pending)         |                          |   pending)         |
+---------+----------+                          +----------+---------+
          |   POST /sync {client_id, updates[]}            |
          +--------------------+  +------------------------+
                               v  v
                    +-------------------------+
                    | Flask server (server.py)|
                    |  validate_update()      |
                    |  apply_update()         |
                    |   -> decide_winner() LWW|
                    |  server.db              |
                    +-------------------------+
          reply: {ok, results[] (outcome per update), records[] (canonical data)}
```

### Database design (identical in all three files)
**`students`**: `student_id INTEGER PRIMARY KEY`, `name`. Five students (101–105) are seeded in every database.

**`attendance`**
| Column | Purpose |
|---|---|
| `student_id`, `att_date` | Together form the **primary key**: one record per student per day, so a duplicate row cannot exist |
| `status` | `Present` / `Absent`, enforced with `CHECK` |
| `client_id` | Which client made this version (`A` / `B`). Also used as the LWW tie-breaker |
| `update_id` | UUID created for every change, `UNIQUE`. Identifies one specific version, so a re-sent update is detected as a duplicate |
| `modified_at` | UTC time of the change, fixed format `2026-11-20T10:05:00.000000Z`. This is the timestamp LWW compares |
| `synced` | Client side: `0` = pending, `1` = acknowledged by the server. Always `1` on the server |

The project uses no separate "pending queue" table: the pending flag lives on the row itself. That keeps the schema small. Modifying a record twice offline simply keeps the latest version, which is also what LWW would choose.

### API
| Method / Path | Request | Response |
|---|---|---|
| `GET /health` | none | `{"ok": true, "status": "online"}` |
| `POST /sync` | `{"client_id": "A", "updates": [ {student_id, att_date, status, client_id, update_id, modified_at}, ... ]}` | `200 {"ok": true, "results": [...], "records": [...]}`, or `400/500 {"ok": false, "error": "..."}` |

Each entry in `results` has `outcome`: `incoming` (accepted), `existing` (rejected because it is older) or `duplicate`. It also has `conflict` (true/false) and the winning status, client and timestamp.

## 6. Working methodology
1. **Mark offline:** `client.mark_attendance()` writes the row with a new `update_id`, the current UTC `modified_at` and `synced = 0`. No network is used.
2. **Sync:** `client.sync()` reads all pending rows and posts them to `/sync`.
3. **Validate:** the server checks every update first (fields, types, known student, valid date, valid timestamp, allowed status). If anything is invalid, it rejects the whole batch with 400 and changes nothing.
4. **Resolve:** inside one SQLite transaction, `apply_update()` loads the stored record and calls `decide_winner(existing, incoming)`:
   - no stored record → accept
   - same `update_id` → duplicate, no change
   - otherwise the larger `(modified_at, client_id, update_id)` wins
5. **Reply:** the server returns the outcome of each update and all canonical records.
6. **Update client:** in one transaction, the client overwrites its rows with the server's records and marks them `synced = 1`. If the request failed (server down, timeout, 400/500), the client changes nothing and the updates stay pending for the next try.

## 7. Technology choices
- **Python:** readable and quick to develop in. Has `sqlite3` and `uuid` built in.
- **SQLite:** a single-file database with no server process, ideal for an offline client. It supports transactions and constraints.
- **Flask:** a minimal web framework. One small file is enough for a JSON API.
- **Requests:** the simplest HTTP client for Python, with timeouts and clear connection errors.
- **unittest:** part of the standard library, so testing adds no extra dependency.

## 8. Results (verified)
Results are from actual runs on 2026-11-20 with Python 3.14, Flask 3.1 and Requests 2.33.

**Automated tests:** `python -m unittest discover -v tests` ran 16 tests, all **OK**.
As a mutation check, `decide_winner` was temporarily replaced with "always overwrite". 4 tests then failed, including the out-of-order integration test. This shows the tests really check LWW.

**Scenario demo:** `python demo_scenarios.py` passed all three scenarios. The full output is in `demo_output.txt`. Summary:

| Scenario | Updates | Sync order | Server decisions | Final state (server = A = B) |
|---|---|---|---|---|
| 1. Non-conflicting | A: 101 Present @09:00; B: 102 Absent @09:01 | A, B, A | both ACCEPTED | 101 Present (A), 102 Absent (B): 2 rows |
| 2. Conflict | A: 101 Present @10:00; B: 101 Absent @10:05 | A, B, A | A accepted; B conflict → newer → wins | 101 **Absent** by B: 1 row |
| 3. Out-of-order + repeated | A: 103 Absent @11:00 (older); B: 103 Present @11:30 (newer) | **B first**, then A, then 2 more rounds each, then both updates re-sent | A's older update REJECTED; re-sent B update = DUPLICATE; re-sent A update rejected again | 103 **Present** by B: 1 row |

Excerpt from Scenario 3 (real output):
```
>>> Client A synchronizes (1 pending)
[server] A: student 103 2026-11-20 Absent @ 2026-11-20T11:00:00.000000Z -> REJECTED (older) (winner: Present by B)
  CONFLICT   student 103 on 2026-11-20: your Absent @ 2026-11-20T11:00:00.000000Z LOST to Present by B @ 2026-11-20T11:30:00.000000Z
...
  PASS: older update did not overwrite: 103 is Present
  PASS: no duplicate rows: exactly 1 row on server
  PASS: re-sent winning update detected as duplicate
  PASS: re-sent old update was not applied
  PASS: server, A and B are identical
```

**Manual CLI smoke test:**
1. With the server stopped, Client A marked 104 Present and tried to sync. It printed `Synchronization FAILED: server unavailable (ConnectionError); 1 update(s) kept as pending`, and the row stayed `PENDING`.
2. With the server started, Client B marked 104 Absent later and synced (accepted).
3. Client A synced and printed `CONFLICT ... LOST to Absent by B`. Its database then showed 104 Absent by B, synced.

## 9. Advantages
- Attendance can be taken with no network at all, and nothing is lost if the server is down.
- Conflict resolution is automatic, deterministic and easy to explain.
- Duplicate rows are impossible by database design, and repeated syncs are harmless.
- All-or-nothing transactions mean a failed sync never leaves half-written data.
- Small codebase: four main source files, two dependencies.

## 10. Limitations and future scope
- **Depends on client clocks.** A device with a wrong clock can win or lose unfairly. Possible fixes: NTP time sync, comparing against server time, or logical clocks.
- **LWW loses the other edit.** A future version could keep a history/audit table, or flag conflicts for a teacher to review.
- The server returns all records on every sync. A `since` timestamp would cut traffic for large data.
- No authentication. Only for local or LAN demonstration.
- Students are a fixed seeded list and are not synchronized.

## 11. Conclusion
The system meets its goal. Two independent SQLite clients record attendance offline and synchronize through a Flask server. LWW resolves every conflict scenario correctly: non-conflicting merge, newer-wins, and out-of-order/repeated/duplicate delivery. All three databases end up identical, verified by automated tests and by reading the real databases.
