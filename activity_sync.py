#!/usr/bin/env python3
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path.home() / "meshcore-bbs"
DB_FILE = BASE_DIR / "bbs.db"

WEBHOOK_URL = os.getenv("MESHCORE_WEBHOOK_URL", "").strip()
WEBHOOK_TOKEN = os.getenv("MESHCORE_WEBHOOK_TOKEN", "").strip()
WEBHOOK_SOURCE = os.getenv(
    "MESHCORE_WEBHOOK_SOURCE",
    "NAC-BBS",
).strip() or "NAC-BBS"


def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def get_public_device_snapshot():
    conn = get_db()

    rows = conn.execute(
        """
        SELECT
            public_id,
            advertised_name,
            node_type,
            first_seen_at,
            last_seen_at,
            last_advert_path_len,
            sighting_count
        FROM devices
        ORDER BY last_seen_at DESC
        """
    ).fetchall()

    conn.close()

    nodes = []

    for row in rows:
        path_len = row["last_advert_path_len"]

        if path_len is None:
            connection = "unknown"
        elif path_len == 0:
            connection = "direct"
        else:
            connection = "via_mesh"

        nodes.append({
            "public_id": row["public_id"],
            "advertised_name": row["advertised_name"],
            "connection": connection,
            "node_type": row["node_type"],
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
            "sighting_count": row["sighting_count"],
        })

    return nodes


def main():
    if not WEBHOOK_URL or not WEBHOOK_TOKEN:
        print(
            "MeshCore activity sync disabled: "
            "MESHCORE_WEBHOOK_URL or MESHCORE_WEBHOOK_TOKEN is missing.",
            file=sys.stderr,
        )
        return 2

    if not DB_FILE.exists():
        print(
            f"MeshCore activity sync failed: database not found: {DB_FILE}",
            file=sys.stderr,
        )
        return 2

    payload = json.dumps({
        "source": WEBHOOK_SOURCE,
        "nodes": get_public_device_snapshot(),
    }).encode("utf-8")

    request = urllib.request.Request(
        WEBHOOK_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {WEBHOOK_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "NAC-BBS-Activity-Sync/1.0",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode(
                "utf-8",
                errors="replace",
            )

            if 200 <= response.status < 300:
                print(
                    "MeshCore activity sync complete: "
                    f"{len(json.loads(payload)['nodes'])} node(s)."
                )
                return 0

            print(
                f"MeshCore activity sync failed: HTTP {response.status}: {body}",
                file=sys.stderr,
            )
            return 1

    except urllib.error.HTTPError as exc:
        body = exc.read().decode(
            "utf-8",
            errors="replace",
        )
        print(
            f"MeshCore activity sync failed: HTTP {exc.code}: {body}",
            file=sys.stderr,
        )
        return 1

    except Exception as exc:
        print(
            f"MeshCore activity sync failed: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
