from app import create_app, db

# Import your models here
from models import User, League, Team, Player, Roster, DraftPick  # exact names from your models.py

from sqlalchemy import inspect

app = create_app()
with app.app_context():
    # Ensure tables are created
    db.create_all()

    # Inspect tables
    inspector = inspect(db.engine)
    tables = inspector.get_table_names()
    print("Tables in DB:", tables)
