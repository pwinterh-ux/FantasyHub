# scripts/load_players_xml.py
import os, io
from typing import Iterator, Dict, List
from sqlalchemy import create_engine, text
from xml.etree import ElementTree as ET

DB_URL = os.environ["DATABASE_URL"]  # inherited if you opened the console from the Web tab
XML_PATH = "/home/pwindynasty/mysite/player_cache_2025.json"  # (XML content)

CHUNK = 2000  # tune for speed/memory

def iter_players(xml_path: str) -> Iterator[Dict]:
    # Try streaming parse first
    try:
        for event, elem in ET.iterparse(xml_path, events=("end",)):
            if elem.tag.lower() == "player":
                pid = int(elem.attrib["id"])
                yield {
                    "id": pid,
                    "mfl_id": str(pid),
                    "name": elem.attrib.get("name"),
                    "position": elem.attrib.get("position"),
                    "team": elem.attrib.get("team"),
                    "status": elem.attrib.get("status"),
                }
                elem.clear()
        return
    except ET.ParseError:
        pass  # likely no single root; fall back to wrapped parse

    # Fallback: wrap the file in a synthetic root
    with open(xml_path, "rb") as f:
        data = f.read()
    wrapped = b"<players>" + data + b"</players>"
    root = ET.fromstring(wrapped)
    for e in root.iter():
        if e.tag.lower() == "player":
            pid = int(e.attrib["id"])
            yield {
                "id": pid,
                "mfl_id": str(pid),
                "name": e.attrib.get("name"),
                "position": e.attrib.get("position"),
                "team": e.attrib.get("team"),
                "status": e.attrib.get("status"),
            }

def bulk_upsert(rows: List[Dict]):
    if not rows: return 0
    stmt = text("""
        INSERT INTO players (id, mfl_id, name, position, team, status)
        VALUES (:id, :mfl_id, :name, :position, :team, :status)
        ON DUPLICATE KEY UPDATE
          mfl_id=VALUES(mfl_id),
          name=VALUES(name),
          position=VALUES(position),
          team=VALUES(team),
          status=VALUES(status)
    """)
    with engine.begin() as conn:
        conn.execute(stmt, rows)
    return len(rows)

if __name__ == "__main__":
    engine = create_engine(DB_URL, pool_pre_ping=True, future=True)
    total = 0
    buf: List[Dict] = []
    for row in iter_players(XML_PATH):
        buf.append(row)
        if len(buf) >= CHUNK:
            total += bulk_upsert(buf); buf.clear()
    total += bulk_upsert(buf)
    print(f"Upserted {total} players.")

