"""Bounded, read-only latency probe for an already-running local DotBot relay."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, choices=range(1, 61), default=60, metavar="1..60")
    parser.add_argument("--after", type=int, help="Only return IDs newer than this previous probe message.")
    args = parser.parse_args()
    directory = args.data_dir.expanduser().resolve()
    settings = json.loads((directory / "config.json").read_text())
    db = sqlite3.connect((directory / "relay.sqlite").as_uri() + "?mode=ro", uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        after = args.after
        if after is None:
            after = db.execute("SELECT COALESCE(MAX(CAST(id AS INTEGER)), 0) FROM requests").fetchone()[0]
        started = time.time()
        deadline = time.monotonic() + args.timeout
        print(json.dumps({"ready": True, "wait_started_at": iso(started), "after": str(after)}), flush=True)
        while time.monotonic() < deadline:
            rows = db.execute("SELECT * FROM requests WHERE CAST(id AS INTEGER)>? AND author_id=? "
                              "ORDER BY CAST(id AS INTEGER) LIMIT 100", (after, settings["owner_id"])).fetchall()
            for row in rows:
                after = int(row["id"])
                if row["guild_id"] not in settings["guild_ids"] or row["channel_id"] not in settings["channel_ids"]:
                    continue
                observed = time.time()
                sent = ((int(row["id"]) >> 22) + 1420070400000) / 1000
                context = db.execute("SELECT id, author_id, author_name, content, received_at FROM requests "
                                     "WHERE channel_id=? AND received_at<=? ORDER BY received_at DESC, id DESC LIMIT 30",
                                     (row["channel_id"], row["received_at"])).fetchall()
                print(json.dumps({
                    "event": "message", "source": "local_sqlite_probe", "message": dict(row),
                    "context": list(reversed([dict(item) for item in context])),
                    "discord_sent_at": iso(sent), "relay_received_at": iso(row["received_at"]),
                    "probe_observed_at": iso(observed),
                    "relay_to_probe_ms": round((observed - row["received_at"]) * 1000, 3),
                    "probe_returning_at": iso(time.time()),
                }), flush=True)
                return
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        print(json.dumps({"event": "timeout", "after": str(after), "finished_at": iso(time.time())}), flush=True)
    finally:
        db.close()


if __name__ == "__main__":
    main()
