# app.py
import os
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime

from flask import Flask, render_template, redirect, url_for, request, flash
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, current_user, login_required
from sqlalchemy import text  # for non-fatal warmup ping

# Legal versions (single source of truth)
from legal_versions import current_versions

# ----- Extensions (import these in models.py) -----
db = SQLAlchemy()
bcrypt = Bcrypt()
login_manager = LoginManager()


def _configure_logging(app: Flask) -> None:
    """
    Make INFO logs visible and also write to logs/fantasyhub.log with rotation.
    PythonAnywhere will also capture these in the Error log.
    """
    app.logger.setLevel(logging.INFO)
    for h in app.logger.handlers:
        h.setLevel(logging.INFO)

    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    file_path = os.path.join(log_dir, "fantasyhub.log")

    file_handler = RotatingFileHandler(file_path, maxBytes=1_000_000, backupCount=3)
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))

    already_added = any(
        isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", "") == file_path
        for h in app.logger.handlers
    )
    if not already_added:
        app.logger.addHandler(file_handler)

    app.logger.info("Logging configured. Writing to %s", file_path)


def _apply_engine_defaults(app: Flask) -> None:
    """
    Apply safe SQLAlchemy engine defaults without clobbering values you may have in config.py.
    """
    opts = dict(app.config.get("SQLALCHEMY_ENGINE_OPTIONS", {}))

    # Only set defaults if not already present
    opts.setdefault("pool_pre_ping", True)
    opts.setdefault("pool_recycle", 280)   # seconds; under common MySQL idle timeout
    opts.setdefault("pool_size", 5)
    opts.setdefault("max_overflow", 5)

    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = opts


def _nonfatal_db_warmup(app: Flask) -> None:
    """
    Best-effort ping to surface connectivity issues in logs without breaking app startup.
    """
    try:
        with app.app_context():
            # one quick round-trip; if MySQL is briefly down, this will just log a warning
            db.session.execute(text("SELECT 1"))
            db.session.commit()
        app.logger.info("DB warmup ping: OK")
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        app.logger.warning("DB warmup ping failed (non-fatal). Service may be starting up.", exc_info=True)


def create_app():
    app = Flask(
        __name__,
        static_folder="static",
        template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"),
    )

    # Load configuration (expects config.py in project root)
    app.config.from_object("config")

    # Optional feature flag: keep the legal gate OFF until you add the template
    app.config.setdefault("LEGAL_GATE_ENABLED", False)

    # Optional: cap MFL response body logging length (used by mfl_client)
    app.config.setdefault("MFL_LOG_BODY_CHARS", 5000)

    # Apply resilient SQLAlchemy engine defaults
    _apply_engine_defaults(app)

    # Initialize extensions
    db.init_app(app)
    bcrypt.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "info"

    # Enable INFO logging & file logs
    _configure_logging(app)

    # Import models after db is ready to avoid circulars
    from models import User, League, SleeperLeague  # <-- includes SleeperLeague

    @login_manager.user_loader
    def load_user(user_id: str):
        try:
            return User.query.get(int(user_id))
        except Exception:
            return None

    # ---- tiny helper: does this user have ANY leagues (MFL or Sleeper)? ----
    def _user_has_any_leagues(user_id: int) -> bool:
        try:
            # Count is fine here; exists() is also OK if you prefer.
            mfl_ct = League.query.filter_by(user_id=user_id).count()
            if mfl_ct > 0:
                return True
            slp_ct = SleeperLeague.query.filter_by(user_id=user_id).count()
            return slp_ct > 0
        except Exception:
            # Be permissive on errors (avoid trapping legit users)
            return False

    # ---- helper: does this user have MFL leagues? (for MFL-only gate) ----
    def _user_has_mfl_leagues(user_id: int) -> bool:
        try:
            return League.query.filter_by(user_id=user_id).limit(1).count() > 0
        except Exception:
            return False

    # ---- make MFL-only gate visible to ALL templates ----
    @app.context_processor
    def inject_feature_flags():
        has_mfl = False
        has_any = False
        try:
            if current_user.is_authenticated:
                has_mfl = _user_has_mfl_leagues(current_user.id)
                has_any = _user_has_any_leagues(current_user.id)
        except Exception:
            pass
        return {
            "has_mfl_leagues": has_mfl,  # <-- use this in buttons/links to gate MFL-only features
            "has_any_leagues": has_any,  # optional convenience
        }

    # ----- Blueprints -----
    from auth.routes import auth_bp
    from leagues.routes import leagues_bp
    from mfl.routes import mfl_bp
    from offers.routes import offers_bp
    from live.routes import live_bp
    from lineups.routes import lineups_bp
    from lineups.routes import lineups_bp
    from ir.routes import ir_bp
    from admin import bp as admin_bp
    from billing.routes import billing_bp
    from injuries.routes import injuries_bp
    from sleeper.routes import sleeper_bp
    from exposure.routes import exposure_bp
    from sos import sos_bp

    app.register_blueprint(offers_bp)
    app.register_blueprint(mfl_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(leagues_bp)
    app.register_blueprint(live_bp)
    app.register_blueprint(lineups_bp)
    app.register_blueprint(ir_bp)
    app.register_blueprint(injuries_bp)
    app.register_blueprint(admin_bp)  # hidden: /_admin/*
    app.register_blueprint(billing_bp)
    app.register_blueprint(sleeper_bp)
    app.register_blueprint(exposure_bp)
    app.register_blueprint(sos_bp)

    # ----- Routes -----
    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/start")
    def start():
        """
        Universal entry for 'Get Started' / 'Leagues' buttons:
          - If NOT logged in -> site login page
          - If logged in and has *any* leagues (MFL or Sleeper) -> leagues page
          - If logged in and NO leagues -> provider link/chooser
        """
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login"))

        if _user_has_any_leagues(current_user.id):
            return redirect(url_for("leagues.my_leagues"))

        # Show a simple provider chooser (you can send straight to Sleeper config if preferred)
        return redirect(url_for("link_providers"))

    @app.route("/link")
    @login_required
    def link_providers():
        # Renders templates/auth/link_providers.html (two buttons: Sleeper / MFL)
        return render_template("auth/link_providers.html")

    # IMPORTANT: Do NOT connect to the DB at startup (no create_all here)
    # If you need schema migrations, use Alembic or a one-off admin command.

    # Optional: non-fatal warmup ping (logs only)
    _nonfatal_db_warmup(app)

    @app.route("/pricing")
    def pricing():
        prices = {
            "MGR5_WEEKLY": os.getenv("PRICE_MGR5_WEEKLY", ""),
            "MGR5_SEASON": os.getenv("PRICE_MGR5_SEASON", ""),
            "MGR12_WEEKLY": os.getenv("PRICE_MGR12_WEEKLY", ""),
            "MGR12_SEASON": os.getenv("PRICE_MGR12_SEASON", ""),
            "UNLIMITED_WEEKLY": os.getenv("PRICE_UNLIMITED_WEEKLY", ""),
            "UNLIMITED_SEASON": os.getenv("PRICE_UNLIMITED_SEASON", ""),
            "FOUNDER_ONETIME": os.getenv("PRICE_FOUNDER_ONETIME", ""),
        }
        return render_template("pricing.html", prices=prices)

    @app.route("/account")
    @login_required
    def account():
        # Count both MFL + Sleeper leagues for display
        leagues_count = (
            League.query.filter_by(user_id=current_user.id).count()
            + SleeperLeague.query.filter_by(user_id=current_user.id).count()
        )

        from services.entitlements import get_entitlements, describe_plan
        from services.store import get_today_count, get_bonus_balance, get_weekly_free_used
        from services.guards import week_monday_key

        u = current_user

        # Plan + entitlements
        ent = get_entitlements(u)
        plan_label = describe_plan(u)
        plan_key = ent.get("plan_key", "free")

        # League cap label
        raw_cap = ent.get("league_cap", 0)
        league_cap_display = "Unlimited" if plan_key in ("unlimited", "founder") else raw_cap

        # Paid daily caps
        mass_offer_daily_cap = int(ent.get("mass_offer_daily_cap", 0) or 0)
        mass_offers_today = int(get_today_count(u.id, datetime.utcnow().date()) or 0)

        # Free weekly allowance
        weekly_free_quota = int(ent.get("free_mass_offer_weekly", 0) or 0)
        weekly_free_used = bool(get_weekly_free_used(u.id, week_monday_key())) if weekly_free_quota > 0 else False

        # Bonus balance
        bonus_mass_offers = int(get_bonus_balance(u.id) or 0)

        # Legal status
        v = current_versions()
        legal_ok = (
            getattr(u, "tos_version", None) == v["tos"]
            and getattr(u, "privacy_version", None) == v["privacy"]
            and getattr(u, "aup_version", None) == v["aup"]
        )

        return render_template(
            "account.html",
            plan_label=plan_label,
            plan_key=plan_key,
            league_cap=league_cap_display,
            leagues_count=leagues_count,
            mass_offer_daily_cap=mass_offer_daily_cap,
            mass_offers_today=mass_offers_today,
            weekly_free_quota=weekly_free_quota,
            weekly_free_used=weekly_free_used,
            free_recipients_cap=ent.get("free_recipients_cap"),
            bonus_mass_offers=bonus_mass_offers,
            stripe_customer_id=getattr(u, "stripe_customer_id", None),
            founder_expires_at=getattr(u, "founder_expires_at", None),
            tos_version=getattr(u, "tos_version", None),
            privacy_version=getattr(u, "privacy_version", None),
            aup_version=getattr(u, "aup_version", None),
            terms_accepted_at=getattr(u, "terms_accepted_at", None),
            terms_accepted_ip=getattr(u, "terms_accepted_ip", None),
            legal_ok=legal_ok,
        )

    @app.route("/legal/terms")
    def legal_terms():
        return render_template("legal/terms.html")

    @app.route("/legal/privacy")
    def legal_privacy():
        return render_template("legal/privacy.html")

    @app.route("/legal/aup")
    def legal_aup():
        return render_template("legal/aup.html")

    # ---------- Legal gate: require acceptance of current versions (flagged) ----------
    @app.before_request
    def _require_legal_acceptance():
        try:
            if not app.config.get("LEGAL_GATE_ENABLED", False):
                return
            if not current_user.is_authenticated:
                return
            allowed = {
                "legal_terms", "legal_privacy", "legal_aup",
                "legal_review", "legal_accept",
                "static", "auth.logout", "auth.login", "auth.register"
            }
            if request.endpoint in allowed:
                return

            v = current_versions()
            ok = (
                getattr(current_user, "tos_version", None) == v["tos"]
                and getattr(current_user, "privacy_version", None) == v["privacy"]
                and getattr(current_user, "aup_version", None) == v["aup"]
            )
            if not ok:
                nxt = request.full_path if request.full_path else request.path
                return redirect(url_for("legal_review", next=nxt))
        except Exception:
            return

    @app.route("/legal/review")
    def legal_review():
        nxt = request.args.get("next") or url_for("index")
        return render_template("legal/review.html",
                               versions=current_versions(),
                               next_url=nxt)

    @app.route("/legal/accept", methods=["POST"])
    def legal_accept():
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login"))

        v = current_versions()
        ip = request.headers.get("X-Forwarded-For", request.remote_addr) or ""
        ip = ip.split(",")[0].strip() if ip else None

        try:
            current_user.tos_version = v["tos"]
            current_user.privacy_version = v["privacy"]
            current_user.aup_version = v["aup"]
            current_user.terms_accepted_at = datetime.utcnow()
            current_user.terms_accepted_ip = ip
            db.session.commit()
            flash("Thanks! Your acceptance has been recorded.", "success")
        except Exception:
            db.session.rollback()
            flash("We couldn't record your acceptance. Please try again.", "danger")

        nxt = request.form.get("next") or url_for("index")
        return redirect(nxt)

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True)
