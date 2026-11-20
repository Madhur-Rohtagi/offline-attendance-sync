"""Central Flask server. Run:  python server.py  (listens on http://127.0.0.1:5000)"""
import sqlite3
import sys

from flask import Flask, jsonify, request

from db import connect, get_all_attendance, get_students, init_db
from sync_logic import apply_update, validate_update

SERVER_DB = "server.db"


def create_app(db_path=SERVER_DB):
    init_db(db_path)
    app = Flask(__name__)

    def error(message, status):
        return jsonify({"ok": False, "error": message}), status

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "status": "online"})

    @app.post("/sync")
    def sync():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return error("request body must be a JSON object", 400)
        client_id = body.get("client_id")
        updates = body.get("updates")
        if not isinstance(client_id, str) or not client_id.strip():
            return error("client_id is required", 400)
        if not isinstance(updates, list):
            return error("updates must be a list", 400)

        conn = connect(db_path)
        try:
            known_ids = {s["student_id"] for s in get_students(conn)}
            # Validate the whole batch first: all-or-nothing keeps things simple.
            for i, update in enumerate(updates):
                problem = validate_update(update, known_ids)
                if problem:
                    return error(f"update #{i + 1} is invalid: {problem}", 400)

            with conn:  # one transaction for the whole batch
                results = [apply_update(conn, u) for u in updates]
            records = get_all_attendance(conn)
        except ValueError as e:
            return error(str(e), 400)
        except sqlite3.Error as e:
            return error(f"database error: {e}", 500)
        finally:
            conn.close()

        labels = {"incoming": "ACCEPTED", "existing": "REJECTED (older)", "duplicate": "DUPLICATE"}
        for r in results:
            print(f"[server] {client_id}: student {r['student_id']} {r['att_date']} "
                  f"{r['sent_status']} @ {r['sent_modified_at']} -> {labels[r['outcome']]}"
                  f" (winner: {r['winner_status']} by {r['winner_client']})", flush=True)

        # shortcut: return every canonical record, not a delta; fine for a class-sized
        # dataset, switch to a "changed since" filter if the data gets large.
        return jsonify({"ok": True, "results": results, "records": records})

    @app.errorhandler(404)
    def not_found(_):
        return error("endpoint not found", 404)

    @app.errorhandler(405)
    def wrong_method(_):
        return error("method not allowed", 405)

    return app


def start_background_server(db_path):
    """Run the real server in a thread on a free port (used by the demo and tests).

    Returns (server, url); call server.shutdown() when done.
    """
    import logging
    import threading
    from werkzeug.serving import make_server

    logging.getLogger("werkzeug").setLevel(logging.ERROR)  # hide per-request log lines
    server = make_server("127.0.0.1", 0, create_app(db_path))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    print(f"Server database: {SERVER_DB}")
    create_app().run(host="127.0.0.1", port=port)
