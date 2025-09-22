# app.py
import os
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, date

from flask import Flask, render_template, redirect, url_for, request, flash
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, current_user, login_required

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
    # Raise app logger to INFO
    app.logger.setLevel(logging.INFO)
    for h in app.logger.handlers:
        h.setLevel(logging.INFO)

    # Add a rotating file handler
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    file_path = os.path.join(log_dir, "fantasyhub.log")

    file_handler = RotatingFileHandler(file_path, maxBytes=1_000_000, backupCount=3)
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))

    # Avoid adding duplicate handlers if app reloads
    already_added = any(
        isinstance(h, RotatingFileHandler) and getattr(h, 'baseFilename', '') == file_path
        for h in app.logger.handlers
    )
    if not already_added:
        app.logger.addHandler(file_handler)

    app.logger.info("Logging configured. Writing to %s", file_path)


def create_app():
    app = Flask(
        __name__,
        static_folder="static",
        template_folder=os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "templates"
        ),
    )

    # Load configuration (expects config.py in project root)
    app.config.from_object("config")

    # Optional feature flag: keep the legal gate OFF until you add the template
    app.config.setdefault("LEGAL_GATE_ENABLED", False)

    # Optional: cap MFL response body logging length (used by mfl_client)
    app.config.setdefault("MFL_LOG_BODY_CHARS", 5000)

    # Initialize extensions
    db.init_app(app)
    bcrypt.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"          # where @login_required redirects
    login_manager.login_message_category = "info"

    # Enable INFO logging & file logs
    _configure_logging(app)

    # Import models after db is ready to avoid circulars
    from models import User, League  # noqa: F401

    @login_manager.user_loader
    def load_user(user_id: str):
        try:
            return User.query.get(int(user_id))
        except Exception:
            return None

    # ----- Blueprints -----
    from auth.routes import auth_bp
    from leagues.routes import leagues_bp
    from mfl.routes import mfl_bp
    from offers.routes import offers_bp
    from live.routes import live_bp
    from lineups.routes import lineups_bp
    from admin import bp as admin_bp
    from billing.routes import billing_bp
    from injuries.routes import injuries_bp

    app.register_blueprint(offers_bp)
    app.register_blueprint(mfl_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(leagues_bp)
    app.register_blueprint(live_bp)
    app.register_blueprint(lineups_bp)
    app.register_blueprint(injuries_bp)
    app.register_blueprint(admin_bp)  # hidden: /_admin/*
    app.register_blueprint(billing_bp)

    # ----- Routes -----
    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/start")
    def start():
        """
        Universal entry for 'Get Started' / 'Leagues' buttons:
          - If NOT logged in -> site login page
          - If logged in and has leagues -> leagues page
          - If logged in and NO leagues -> MFL link/login page
        """
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login"))

        # local import to avoid early import cycles
        from models import League as _League
        has_leagues = _League.query.filter_by(user_id=current_user.id).count() > 0
        if has_leagues:
            return redirect(url_for("leagues.my_leagues"))
        return redirect(url_for("mfl.mfl_login"))

    # Dev convenience: create tables if they don't exist
    with app.app_context():
        db.create_all()
        app.logger.info("Database tables ensured (create_all).")

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
        from models import League as _League
        from services.entitlements import get_entitlements, describe_plan
        from services.store import (
            get_today_count,
            get_bonus_balance,
            get_weekly_free_used,
        )
        from services.guards import week_monday_key
        from datetime import date

        u = current_user

        # Counts + plan
        leagues_count = _League.query.filter_by(user_id=u.id).count()
        ent = get_entitlements(u)
        plan_label = describe_plan(u)
        plan_key = ent.get("plan_key", "free")

        # League cap label
        raw_cap = ent.get("league_cap", 0)
        league_cap_display = "Unlimited" if plan_key in ("unlimited", "founder") else raw_cap

        # Paid daily caps (shown for paid plans)
        mass_offer_daily_cap = int(ent.get("mass_offer_daily_cap", 0) or 0)
        mass_offers_today = int(get_today_count(u.id, date.today()) or 0)

        # Free weekly allowance
        weekly_free_quota = int(ent.get("free_mass_offer_weekly", 0) or 0)  # usually 1 on free, 0 on paid
        weekly_free_used = bool(get_weekly_free_used(u.id, week_monday_key())) if weekly_free_quota > 0 else False

        # Bonus balance (applies to all plans)
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
                return  # gate disabled by default until template is added
            if not current_user.is_authenticated:
                return
            # endpoints that must remain accessible
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
            # Never block the request if something unexpected happens
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
        # best-effort IP behind proxies
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
    # Use Flask's reloader for local dev
    app.run(debug=True)
