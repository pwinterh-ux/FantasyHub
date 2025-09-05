# sync_players.py
import argparse
import io
import os
import sys
import xml.etree.ElementTree as ET

from app import create_app, db
from models import Player

# Optional: fast, atomic upsert using MySQL's ON DUPLICATE KEY UPDATE
try:
    from sqlalchemy.dialects.mysql import insert as mysql_insert
    HAVE_MYSQL_UPSERT = True
except Exception:
    HAVE_MYSQL_UPSERT = False


def _read_xml_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _ensure_single_root(xml_bytes: bytes) -> bytes:
    """ElementTree needs a single root. If parsing fails, wrap in <players>...</players>."""
    try:
        ET.fromstring(xml_bytes)
        return xml_bytes
    except ET.ParseError:
        wrapped = b"<players>" + xml_bytes + b"</players>"
        # If this still fails, let it raise upstream for a clear error
        ET.fromstring(wrapped)
        return wrapped


def _parse_players_xml(xml_bytes: bytes):
    """Stream-parse players into dict rows suitable for bulk insert/upsert."""
    rows = []
    for _, elem in ET.iterparse(io.BytesIO(xml_bytes), events=("end",)):
        if elem.tag != "player":
            continue
        try:
            pid = int(elem.attrib["id"])
        except (KeyError, ValueError):
            # Skip if no/invalid id
            elem.clear()
            continue

        rows.append(
            {
                # PK equals external MFL id in your DB
                "id": pid,
                "mfl_id": elem.attrib.get("id", str(pid)),
                "name": elem.attrib.get("name"),
                "position": elem.attrib.get("position"),
                "team": elem.attrib.get("team"),
                "status": elem.attrib.get("status"),
            }
        )
        elem.clear()
    return rows


def _bulk_upsert(rows):
    if not rows:
        return 0

    if HAVE_MYSQL_UPSERT:
        # Use MySQL ON DUPLICATE KEY UPDATE for speed and atomicity
        stmt = mysql_insert(Player.__table__).values(rows)
        stmt = stmt.on_duplicate_key_update(
            mfl_id=stmt.inserted.mfl_id,
            name=stmt.inserted.name,
            position=stmt.inserted.position,
            team=stmt.inserted.team,
            status=stmt.inserted.status,
        )
        db.session.execute(stmt)
        db.session.commit()
        return len(rows)

    # Fallback: row-by-row upsert via ORM (slower but simple)
    updated = 0
    for r in rows:
        p = Player.query.get(r["id"])
        if p is None:
            p = Player(**r)
            db.session.add(p)
        else:
            p.mfl_id = r["mfl_id"]
            p.name = r["name"]
            p.position = r["position"]
            p.team = r["team"]
            p.status = r["status"]
        updated += 1
    db.session.commit()
    return updated


def main():
    parser = argparse.ArgumentParser(description="Sync players.xml into the players table.")
    parser.add_argument(
        "--file",
        default=None,
        help="Path to players.xml (defaults to ./static/players.xml).",
    )
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        xml_path = args.file or os.path.join(app.root_path, "static", "players.xml")
        if not os.path.exists(xml_path):
            print(f"ERROR: file not found: {xml_path}", file=sys.stderr)
            sys.exit(1)

        xml_bytes = _read_xml_bytes(xml_path)
        xml_bytes = _ensure_single_root(xml_bytes)
        rows = _parse_players_xml(xml_bytes)
        if not rows:
            print("No <player> elements found. Nothing to do.")
            return

        count = _bulk_upsert(rows)
        print(f"Upserted {count} players from {xml_path}.")


if __name__ == "__main__":
    main()
