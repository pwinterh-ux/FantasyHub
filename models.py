# models.py
from flask_login import UserMixin
from app import db, bcrypt  # created in app.py


# ----- User -----------------------------------------------------------------

class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)

    password_hash = db.Column(db.String(255), nullable=True)
    # DB shows tinyint(1) NULL; keep nullable=True (no default)
    is_admin = db.Column(db.Boolean, nullable=True)

    # matches existing column in DB (nullable, no default)
    created_at = db.Column(db.DateTime, nullable=True)

    # existing columns in your DB
    mfl_user = db.Column(db.String(120), nullable=True)
    session_key = db.Column(db.String(255), nullable=True)

    # relationships
    leagues = db.relationship(
        "League",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # password helpers
    def set_password(self, password: str) -> None:
        self.password_hash = bcrypt.generate_password_hash(password).decode("utf-8")

    def check_password(self, password: str) -> bool:
        if not self.password_hash:
            return False
        return bcrypt.check_password_hash(self.password_hash, password)

    def __repr__(self) -> str:
        return f"<User {self.id} {self.username}>"


# ----- League ---------------------------------------------------------------

class League(db.Model):
    __tablename__ = "leagues"

    id = db.Column(db.Integer, primary_key=True)

    # ownership
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # columns per your current MySQL table
    mfl_id = db.Column(db.String(50), nullable=False)        # e.g., '11376'
    name = db.Column(db.String(120), nullable=False)         # league name
    year = db.Column(db.Integer, nullable=False)             # season
    synced_at = db.Column(db.DateTime, nullable=True)        # when you last synced
    roster_slots = db.Column(db.String(255), nullable=True)  # e.g., 'QB:1,RB:2-4,...'
    franchise_id = db.Column(db.String(10), nullable=True)   # user's team in that league (e.g., '0006')

    # relationships
    user = db.relationship("User", back_populates="leagues")
    teams = db.relationship(
        "Team",
        back_populates="league",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<League {self.id} {self.name} {self.year} u{self.user_id} mfl:{self.mfl_id}>"


# ----- Team -----------------------------------------------------------------

class Team(db.Model):
    __tablename__ = "teams"

    id = db.Column(db.Integer, primary_key=True)
    league_id = db.Column(
        db.Integer,
        db.ForeignKey("leagues.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    mfl_id = db.Column(db.String(50), nullable=False)        # franchise id within league
    name = db.Column(db.String(120), nullable=False)
    owner_name = db.Column(db.String(120), nullable=True)

    # present in your DB:
    user_id = db.Column(db.Integer, nullable=True)           # optional link to a site user (null if unused)
    record = db.Column(db.String(20), nullable=True)         # e.g., "3-1-1"
    points_for = db.Column(db.Integer, nullable=True)
    points_against = db.Column(db.Integer, nullable=True)
    standing = db.Column(db.Integer, nullable=True)
    current_opponent_id = db.Column(db.String(50), nullable=True)  # opponent franchise id, e.g., "0002"

    league = db.relationship("League", back_populates="teams")

    # relationships to rosters and draft picks
    rosters = db.relationship(
        "Roster",
        back_populates="team",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    draft_picks = db.relationship(
        "DraftPick",
        back_populates="team",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<Team {self.id} L{self.league_id} mfl:{self.mfl_id} {self.name}>"


# ----- Player ---------------------------------------------------------------

class Player(db.Model):
    __tablename__ = "players"

    # Your table shows ids like 13593 that equal MFL ids.
    # Set autoincrement=False to match external ID PK.
    id = db.Column(db.Integer, primary_key=True, autoincrement=False)
    mfl_id = db.Column(db.String(20), nullable=False, index=True)

    name = db.Column(db.String(120), nullable=True)
    position = db.Column(db.String(10), nullable=True)
    team = db.Column(db.String(10), nullable=True)
    status = db.Column(db.String(20), nullable=True)

    # roster relationship
    rosters = db.relationship(
        "Roster",
        back_populates="player",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<Player {self.id} mfl:{self.mfl_id} {self.name or ''}>"


# ----- Roster (team-player link) -------------------------------------------

class Roster(db.Model):
    __tablename__ = "rosters"

    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(
        db.Integer,
        db.ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    player_id = db.Column(
        db.Integer,
        db.ForeignKey("players.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    is_starter = db.Column(db.Boolean, nullable=False, default=False)

    team = db.relationship("Team", back_populates="rosters")
    player = db.relationship("Player", back_populates="rosters")

    def __repr__(self) -> str:
        return f"<Roster {self.id} team:{self.team_id} player:{self.player_id} starter:{int(bool(self.is_starter))}>"


# ----- Draft Pick -----------------------------------------------------------

class DraftPick(db.Model):
    __tablename__ = "draft_picks"

    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(
        db.Integer,
        db.ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    season = db.Column(db.Integer, nullable=False)
    round = db.Column(db.Integer, nullable=False)
    pick_number = db.Column(db.Integer, nullable=True)       # NULL when not assigned
    original_team = db.Column(db.String(10), nullable=True)  # e.g., "0002"

    team = db.relationship("Team", back_populates="draft_picks")

    def __repr__(self) -> str:
        return f"<DraftPick {self.id} team:{self.team_id} {self.season} R{self.round} P{self.pick_number or '-'} orig:{self.original_team or '-'}>"
