# app.py
import os
import logging
from logging.handlers import RotatingFileHandler

from flask import Flask, render_template, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, current_user

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

    # Initialize extensions
    db.init_app(app)
    bcrypt.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"          # where @login_required redirects
    login_manager.login_message_category = "info"

    # Enable INFO logging & file logs
    _configure_logging(app)

    # Optional: cap MFL response body logging length (used by mfl_client)
    app.config.setdefault("MFL_LOG_BODY_CHARS", 800)

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

    app.register_blueprint(mfl_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(leagues_bp)

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

        from models import League  # local import to avoid early import cycles
        has_leagues = League.query.filter_by(user_id=current_user.id).count() > 0
        if has_leagues:
            return redirect(url_for("leagues.my_leagues"))
        return redirect(url_for("mfl.mfl_login"))

    # Dev convenience: create tables if they don't exist
    with app.app_context():
        db.create_all()
        app.logger.info("Database tables ensured (create_all).")

    return app


if __name__ == "__main__":
    app = create_app()
    # Use Flask's reloader for local dev
    app.run(debug=True)
