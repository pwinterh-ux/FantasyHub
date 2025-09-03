from app import db
from datetime import datetime

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255))
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # 🔹 New MFL-related fields
    mfl_user = db.Column(db.String(120), nullable=True)   # stores MFL username/ID
    session_key = db.Column(db.String(255), nullable=True)  # temporary session token


class League(db.Model):
    __tablename__ = 'leagues'
    id = db.Column(db.Integer, primary_key=True)
    mfl_id = db.Column(db.String(50))  # league_id from MFL
    franchise_id = db.Column(db.String(50))  # 🔹 NEW: your own franchise ID within the league
    name = db.Column(db.String(120))
    year = db.Column(db.Integer, nullable=False)
    # 🔹 Removed commissioner (we don’t need franchise_name duplicated)
    synced_at = db.Column(db.DateTime)
    roster_slots = db.Column(db.String(120))  # optional still


class Team(db.Model):
    __tablename__ = 'teams'
    id = db.Column(db.Integer, primary_key=True)
    league_id = db.Column(db.Integer, db.ForeignKey('leagues.id'))
    mfl_id = db.Column(db.String(50))  # franchise id from MFL
    name = db.Column(db.String(120))
    owner_name = db.Column(db.String(120))
    # 🔹 user_id is optional and should *not* be assumed in templates
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    record = db.Column(db.String(20))       # e.g., "5-3-0"
    points_for = db.Column(db.Float, default=0.0)
    points_against = db.Column(db.Float, default=0.0)
    standing = db.Column(db.Integer)
    current_opponent_id = db.Column(db.String(50))


class Player(db.Model):
    __tablename__ = 'players'
    id = db.Column(db.Integer, primary_key=True)
    mfl_id = db.Column(db.String(50))
    name = db.Column(db.String(120))
    position = db.Column(db.String(10))
    team = db.Column(db.String(10))
    status = db.Column(db.String(50))


class Roster(db.Model):
    __tablename__ = 'rosters'
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'))
    player_id = db.Column(db.Integer, db.ForeignKey('players.id'))
    is_starter = db.Column(db.Boolean, default=False)


class DraftPick(db.Model):
    __tablename__ = 'draft_picks'
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'))
    season = db.Column(db.Integer)
    round = db.Column(db.Integer)
    pick_number = db.Column(db.Integer)
    original_team = db.Column(db.String(120))
