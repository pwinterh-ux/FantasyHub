# auth/routes.py
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, current_user, login_required
from urllib.parse import urlparse, urljoin
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from app import db, bcrypt
from models import User, League, Team

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


def is_safe_url(target: str) -> bool:
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ("http", "https") and ref_url.netloc == test_url.netloc


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("start"))

    if request.method == "POST":
        username_or_email = request.form.get("username_or_email", "").strip()
        password = request.form.get("password", "")

        user = (
            User.query.filter_by(username=username_or_email).first()
            or User.query.filter_by(email=username_or_email.lower()).first()
        )

        if user and user.check_password(password):
            login_user(user, remember=True)
            next_url = request.args.get("next") or request.form.get("next")
            if next_url and is_safe_url(next_url):
                return redirect(next_url)
            return redirect(url_for("start"))

        flash("Invalid credentials.", "danger")

    return render_template("login.html")


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("start"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = (request.form.get("email", "") or "").strip().lower()
        password = request.form.get("password", "") or ""

        errors = []
        if len(username) < 3:
            errors.append("Username must be at least 3 characters.")
        if "@" not in email or "." not in email:
            errors.append("Please enter a valid email address.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")

        # Check if taken
        existing = User.query.filter(or_(User.username == username, User.email == email)).first()
        if existing:
            errors.append("Username or email is already in use.")

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("register.html")

        # Create user
        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("Username or email already exists.", "danger")
            return render_template("register.html")

        # Auto-login then run start logic
        login_user(user, remember=True)
        flash("Account created. You’re signed in.", "success")
        return redirect(url_for("start"))

    return render_template("register.html")


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "success")
    return redirect(url_for("index"))


# --- “Fetch MFL” (mock) pages ----------------------------------------------

@auth_bp.route("/mfl/link", methods=["GET"])
@login_required
def mfl_link():
    return render_template("mfl_link.html")


@auth_bp.route("/mfl/mock_sync", methods=["POST"])
@login_required
def mfl_mock_sync():
    # Seed only if user has no leagues yet
    existing = League.query.filter_by(user_id=current_user.id).count()
    if existing == 0:
        from datetime import datetime

        l1 = League(
            user_id=current_user.id,
            mfl_id="11376",
            name="#SFB15 - Springfield Isotopes",
            year=2025,
            synced_at=datetime.utcnow(),
            roster_slots=None,
            franchise_id=None,
        )
        l2 = League(
            user_id=current_user.id,
            mfl_id="61860",
            name="All Play League",
            year=2025,
            synced_at=datetime.utcnow(),
            roster_slots="QB:1,RB:2-4,WR:3-5,TE:1-3",
            franchise_id="0006",
        )
        db.session.add_all([l1, l2])
        db.session.flush()

        teams = [
            Team(league_id=l1.id, mfl_id="0001", name="Sharks", owner_name=current_user.username),
            Team(league_id=l1.id, mfl_id="0002", name="Wolves", owner_name="Rival GM"),
            Team(league_id=l2.id, mfl_id="0006", name="My Team", owner_name=current_user.username),
            Team(league_id=l2.id, mfl_id="0002", name="Hawks", owner_name="Rival GM"),
        ]
        db.session.add_all(teams)
        db.session.commit()
        flash("Mock MFL sync complete. Leagues added.", "success")
    else:
        flash("You already have leagues—no mock data added.", "info")

    return redirect(url_for("leagues.my_leagues"))
