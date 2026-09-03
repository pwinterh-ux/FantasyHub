# models.py
from flask_login import UserMixin
from app import db, bcrypt  # created in app.py
from datetime import datetime
import json

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

    mfl_cookie_api = db.Column(db.Text, nullable=True)           # cookie string scoped to api.myfantasyleague.com
    mfl_cookie_hosts_json = db.Column(db.Text, nullable=True)    # JSON: {"www43.myfantasyleague.com": "...", ...}
    mfl_cookie_updated_at = db.Column(db.DateTime, nullable=True)

    # models.py (inside class User, add these fields)

    plan = db.Column(db.String(32))                      # e.g. FREE, MGR5_SEASON, etc.
    league_cap = db.Column(db.Integer)                   # nullable until entitlements wired
    mass_offer_daily_cap = db.Column(db.Integer)         # nullable
    bonus_mass_offers = db.Column(db.Integer, default=0)

    stripe_customer_id = db.Column(db.String(64))
    stripe_price_id = db.Column(db.String(64))
    founder_expires_at = db.Column(db.DateTime)

    tos_version = db.Column(db.String(16))
    privacy_version = db.Column(db.String(16))
    aup_version = db.Column(db.String(16))
    terms_accepted_at = db.Column(db.DateTime)
    terms_accepted_ip = db.Column(db.String(45))

    sleeper_user_id = db.Column(db.String(40), index=True)
    sleeper_display_name = db.Column(db.String(120))

    # Optional helpers (still inside User)
    def has_accepted_current_terms(self, versions: dict[str, str]) -> bool:
        """Compare stored versions to the current versions dict keys: tos, privacy, aup."""
        return (
            self.tos_version == versions.get("tos")
            and self.privacy_version == versions.get("privacy")
            and self.aup_version == versions.get("aup")
        )

    # relationships
    leagues = db.relationship(
        "League",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    sleeper_leagues = db.relationship(
        "SleeperLeague",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # --- convenience helpers (optional but handy) ---

    def get_mfl_host_cookies(self) -> dict[str, str]:
        """
        Return per-host cookies as a dict. Safe on empty/malformed JSON.
        """
        try:
            raw = self.mfl_cookie_hosts_json or "{}"
            obj = json.loads(raw)
            if isinstance(obj, dict):
                # normalize to str->str
                return {str(k): str(v) for k, v in obj.items()}
        except Exception:
            pass
        return {}

    def set_mfl_cookie_bundle(
        self,
        api_cookie: str | None,
        host_cookie_map: dict[str, str] | None,
    ) -> None:
        """
        Set api cookie + per-host cookies and stamp updated_at.
        """
        if api_cookie is not None:
            self.mfl_cookie_api = api_cookie
        if host_cookie_map is not None:
            try:
                self.mfl_cookie_hosts_json = json.dumps(host_cookie_map)
            except Exception:
                # fall back to empty object if dumping fails
                self.mfl_cookie_hosts_json = "{}"
        self.mfl_cookie_updated_at = datetime.utcnow()

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

    # core league fields already in your DB
    mfl_id = db.Column(db.String(50), nullable=False)        # e.g., '11376'
    name = db.Column(db.String(120), nullable=False)         # league name
    year = db.Column(db.Integer, nullable=False)             # season
    synced_at = db.Column(db.DateTime, nullable=True)        # when you last synced
    roster_slots = db.Column(db.String(255), nullable=True)  # e.g., 'QB:1,RB:2-4,...'
    franchise_id = db.Column(db.String(10), nullable=True)   # user's team in that league (e.g., '0006')

    # NEW: where this league "lives" + an optional cached homepage URL
    # store either "www43.myfantasyleague.com" or full "https://www43.myfantasyleague.com"
    league_host = db.Column(db.String(64), nullable=True)
    home_url = db.Column(db.String(255), nullable=True)

    # IR configuration (nullable: treat None as zero slots)
    ir_slots_max = db.Column(db.Integer, nullable=True)

    # Waiver / blind-bidding configuration (nullable for legacy leagues)
    waiver_type = db.Column(db.String(32), nullable=True)
    faab_starting_balance = db.Column(db.Numeric(10, 2), nullable=True)
    faab_minimum = db.Column(db.Numeric(10, 2), nullable=True)
    faab_increment = db.Column(db.Numeric(10, 2), nullable=True)
    faab_fcfs_charge = db.Column(db.Numeric(10, 2), nullable=True)
    max_waiver_rounds = db.Column(db.Integer, nullable=True)
    bbid_conditional = db.Column(db.Boolean, nullable=True)

    # relationships
    user = db.relationship("User", back_populates="leagues")
    teams = db.relationship(
        "Team",
        back_populates="league",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # --- convenience link builders ---

    def _league_base(self) -> str | None:
        """
        Normalizes league_host to a full 'https://host' base without trailing slash.
        Returns None if missing.
        """
        if not self.league_host:
            return None
        base = self.league_host.strip().rstrip("/")
        if not base:
            return None
        if not (base.startswith("http://") or base.startswith("https://")):
            base = f"https://{base}"
        return base

    def url_for_league_home(self) -> str | None:
        """
        e.g. https://www43.myfantasyleague.com/2025/home/55188
        """
        base = self._league_base()
        if not base or not self.year or not self.mfl_id:
            return None
        return f"{base}/{self.year}/home/{self.mfl_id}"

    def url_for_trades(self) -> str | None:
        """
        e.g. https://www43.myfantasyleague.com/2025/options?L=55188&O=05
        (O=05 is MFL's Trade screen)
        """
        base = self._league_base()
        if not base or not self.year or not self.mfl_id:
            return None
        return f"{base}/{self.year}/options?L={self.mfl_id}&O=05"

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

    # Waiver state (nullable for legacy leagues / non-waiver usage)
    faab_balance = db.Column(db.Numeric(10, 2), nullable=True)
    waiver_sort_order = db.Column(db.Integer, nullable=True)

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
    in_ir = db.Column(db.Boolean, nullable=True)

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


# ----- Sleeper integration tables -------------------------------------------


class SleeperLeague(db.Model):
    __tablename__ = "sleeper_leagues"

    id = db.Column(db.Integer, primary_key=True)
    sleeper_id = db.Column(db.String(40), nullable=False, index=True)
    name = db.Column(db.String(120))
    year = db.Column(db.Integer, nullable=False)
    synced_at = db.Column(db.DateTime)
    roster_slots = db.Column(db.String(255))
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    draft_id = db.Column(db.String(40))
    waiver_budget = db.Column(db.Integer)
    scoring_json = db.Column(db.JSON)

    user = db.relationship("User", back_populates="sleeper_leagues")
    teams = db.relationship(
        "SleeperTeam",
        back_populates="league",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<SleeperLeague {self.id} sid:{self.sleeper_id} {self.name or ''} {self.year}>"


class SleeperTeam(db.Model):
    __tablename__ = "sleeper_teams"

    id = db.Column(db.Integer, primary_key=True)
    league_id = db.Column(
        db.Integer,
        db.ForeignKey("sleeper_leagues.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sleeper_roster_id = db.Column(db.Integer, nullable=False, index=True)
    owner_user_id = db.Column(db.String(40), index=True)
    name = db.Column(db.String(120))
    owner_name = db.Column(db.String(120))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    record = db.Column(db.String(20))
    points_for = db.Column(db.Numeric(8, 2))
    points_against = db.Column(db.Numeric(8, 2))
    standing = db.Column(db.Integer)
    current_opponent_id = db.Column(db.Integer)
    waiver_balance = db.Column(db.Integer)
    meta_json = db.Column(db.JSON)

    league = db.relationship("SleeperLeague", back_populates="teams")
    rosters = db.relationship(
        "SleeperRoster",
        back_populates="team",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    draft_picks = db.relationship(
        "SleeperDraftPick",
        back_populates="team",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<SleeperTeam {self.id} league:{self.league_id} roster:{self.sleeper_roster_id} {self.name or ''}>"


class SleeperRoster(db.Model):
    __tablename__ = "sleeper_rosters"

    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(
        db.Integer,
        db.ForeignKey("sleeper_teams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    player_sid = db.Column(db.String(40), nullable=False, index=True)
    is_starter = db.Column(db.Boolean, nullable=False, default=False)
    order_index = db.Column(db.SmallInteger)
    lineup_slot = db.Column(db.String(20))

    team = db.relationship("SleeperTeam", back_populates="rosters")

    def __repr__(self) -> str:
        return f"<SleeperRoster {self.id} team:{self.team_id} player:{self.player_sid} starter:{int(bool(self.is_starter))}>"


class SleeperPlayer(db.Model):
    __tablename__ = "sleeper_players"

    id = db.Column(db.Integer, primary_key=True)
    sleeper_id = db.Column(db.String(40), nullable=False, unique=True, index=True)
    name = db.Column(db.String(120), index=True)
    position = db.Column(db.String(10), index=True)
    team = db.Column(db.String(10), index=True)
    status = db.Column(db.String(20))

    def __repr__(self) -> str:
        return f"<SleeperPlayer {self.id} sid:{self.sleeper_id} {self.name or ''}>"


class SleeperDraftPick(db.Model):
    __tablename__ = "sleeper_draft_picks"

    id = db.Column(db.Integer, primary_key=True)
    league_id = db.Column(
        db.Integer,
        db.ForeignKey("sleeper_leagues.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    team_id = db.Column(
        db.Integer,
        db.ForeignKey("sleeper_teams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    season = db.Column(db.Integer, nullable=False)
    round = db.Column(db.Integer, nullable=False)
    pick_number = db.Column(db.Integer)
    original_team = db.Column(db.String(120))
    original_roster_id = db.Column(db.Integer, nullable=False)

    league = db.relationship("SleeperLeague")
    team = db.relationship("SleeperTeam", back_populates="draft_picks")

    def __repr__(self) -> str:
        return (
            f"<SleeperDraftPick {self.id} league:{self.league_id} team:{self.team_id} "
            f"{self.season} R{self.round} orig:{self.original_roster_id}>"
        )


# ----- NFL Weekly Opponent Schedule (minimal) -------------------------------

class NflSchedule(db.Model):
    __tablename__ = "nflschedule"

    # Composite PK (no auto-increment id)
    year = db.Column(db.SmallInteger, primary_key=True, nullable=False)  # e.g., 2025
    week = db.Column(db.SmallInteger, primary_key=True, nullable=False)  # 1..18 (may include 19..22 for playoffs XML)
    team = db.Column(db.String(10), primary_key=True, nullable=False)    # e.g., DAL, KCC

    # Data
    opponent = db.Column(db.String(10), nullable=False)
    is_home = db.Column(db.Boolean, nullable=False)                      # True = home, False = away
    kickoff_unix = db.Column(db.BigInteger, nullable=True)               # epoch seconds (UTC)

    def __repr__(self) -> str:
        h = "H" if self.is_home else "A"
        return f"<NflSchedule {self.year} W{self.week} {self.team} vs {self.opponent} {h}>"


# ----- Dynasty Ranks (current only) -----------------------------------------

class DynastyRankSourceCurrent(db.Model):
    __tablename__ = "dynasty_rank_source_current"
    __table_args__ = (
        db.UniqueConstraint("source", "position", "mfl_id", name="uq_dynasty_rank_source_current_key"),
        db.Index("ix_dynasty_rank_source_current_source_position_rank", "source", "position", "source_rank"),
        db.Index("ix_dynasty_rank_source_current_position_rank", "position", "source_rank"),
        db.Index("ix_dynasty_rank_source_current_mfl", "mfl_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String(40), nullable=False, index=True)  # e.g. fantasycalc
    position = db.Column(db.String(10), nullable=False, index=True)  # QB/RB/WR/TE
    mfl_id = db.Column(db.String(20), nullable=False, index=True)

    player_name = db.Column(db.String(120), nullable=True)
    source_rank = db.Column(db.Integer, nullable=True)
    source_value = db.Column(db.Float, nullable=True)

    updated_at_utc = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    def __repr__(self) -> str:
        return f"<DynastyRankSourceCurrent {self.source} {self.position} mfl:{self.mfl_id} r:{self.source_rank}>"


class DynastyRankConsensusCurrent(db.Model):
    __tablename__ = "dynasty_rank_consensus_current"
    __table_args__ = (
        db.UniqueConstraint("position", "mfl_id", name="uq_dynasty_rank_consensus_current_key"),
        db.Index("ix_dynasty_rank_consensus_current_position_rank", "position", "consensus_rank"),
        db.Index("ix_dynasty_rank_consensus_current_position_positional_rank", "position", "positional_rank"),
        db.Index("ix_dynasty_rank_consensus_current_mfl", "mfl_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    position = db.Column(db.String(10), nullable=False, index=True)
    mfl_id = db.Column(db.String(20), nullable=False, index=True)

    player_name = db.Column(db.String(120), nullable=True)
    consensus_rank = db.Column(db.Float, nullable=True)
    positional_rank = db.Column(db.Integer, nullable=True)
    sources_used = db.Column(db.Integer, nullable=False, default=1)

    updated_at_utc = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    def __repr__(self) -> str:
        return (
            f"<DynastyRankConsensusCurrent {self.position} mfl:{self.mfl_id} "
            f"c:{self.consensus_rank} p:{self.positional_rank}>"
        )


class DynastyRankIngestAudit(db.Model):
    __tablename__ = "dynasty_rank_ingest_audit"
    __table_args__ = (
        db.Index("ix_dynasty_rank_ingest_audit_source_started", "source", "started_at_utc"),
    )

    id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String(40), nullable=False, index=True)  # e.g. fantasycalc

    started_at_utc = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    finished_at_utc = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="running")  # success/partial/failed

    qb_count = db.Column(db.Integer, nullable=False, default=0)
    rb_count = db.Column(db.Integer, nullable=False, default=0)
    wr_count = db.Column(db.Integer, nullable=False, default=0)
    te_count = db.Column(db.Integer, nullable=False, default=0)

    error_summary = db.Column(db.Text, nullable=True)

    def __repr__(self) -> str:
        return f"<DynastyRankIngestAudit {self.id} {self.source} {self.status}>"


class DynastyRankMapOverride(db.Model):
    __tablename__ = "dynasty_rank_map_overrides"
    __table_args__ = (
        db.UniqueConstraint("source", "position", "source_name_norm", name="uq_dynasty_rank_map_override_key"),
        db.Index("ix_dynasty_rank_map_override_source_pos", "source", "position"),
    )

    id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String(40), nullable=False, index=True)
    position = db.Column(db.String(10), nullable=False, index=True)
    source_name_norm = db.Column(db.String(160), nullable=False, index=True)
    source_name_raw = db.Column(db.String(160), nullable=True)
    mapped_mfl_id = db.Column(db.String(20), nullable=False, index=True)
    notes = db.Column(db.String(255), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    updated_at_utc = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    def __repr__(self) -> str:
        return f"<DynastyRankMapOverride {self.source} {self.position} {self.source_name_norm} -> {self.mapped_mfl_id}>"