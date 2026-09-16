"""Add MFL lineup-mode and roster-location columns to an existing database.

Run once in each deployed environment:
    python scripts/migrate_mfl_lineup_awareness.py
"""
from pathlib import Path
import sys

from sqlalchemy import Column, DateTime, Integer, String, inspect, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app, db


COLUMNS = {
    "leagues": [
        Column("lineup_mode", String(16), nullable=False, server_default="UNKNOWN"),
        Column("taxi_slots_max", Integer, nullable=True),
        Column("roster_status_synced_at", DateTime, nullable=True),
    ],
    "rosters": [
        Column("roster_status", String(16), nullable=False, server_default="UNKNOWN"),
    ],
}


def migrate() -> None:
    inspector = inspect(db.engine)
    compiler = db.engine.dialect.ddl_compiler(db.engine.dialect, None)
    preparer = db.engine.dialect.identifier_preparer
    with db.engine.begin() as connection:
        for table_name, columns in COLUMNS.items():
            existing = {column["name"] for column in inspector.get_columns(table_name)}
            for column in columns:
                if column.name in existing:
                    print(f"{table_name}.{column.name}: already present")
                    continue
                specification = compiler.get_column_specification(column)
                statement = f"ALTER TABLE {preparer.quote(table_name)} ADD COLUMN {specification}"
                connection.execute(text(statement))
                print(f"{table_name}.{column.name}: added")


if __name__ == "__main__":
    app = create_app()
    with app.app_context():
        migrate()
