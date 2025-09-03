import os
from flask import Flask, render_template
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt

db = SQLAlchemy()
bcrypt = Bcrypt()

def create_app():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    template_dir = os.path.join(base_dir, "templates")
    static_dir = os.path.join(base_dir, "static")

    print("Template folder being used:", template_dir)
    print("Static folder being used:", static_dir)

    app = Flask(
        "FantasyHub",
        static_folder=static_dir,
        template_folder=template_dir
    )
    app.config.from_object('config')
    db.init_app(app)
    bcrypt.init_app(app)

    # Debug prints - add these after app creation
    print("Template folder path:", app.template_folder)
    print("Absolute template path:", os.path.abspath(app.template_folder))
    print("Template folder exists:", os.path.exists(app.template_folder))

    # List files in template directory
    template_dir = os.path.abspath(app.template_folder)
    if os.path.exists(template_dir):
        print("Files in template directory:", os.listdir(template_dir))
    else:
        print("Template directory does not exist!")

    if os.path.exists(static_dir):
        print("Files in static dir:", os.listdir(static_dir))
    else:
        print("Static dir missing!")

    # Import and register blueprints
    from auth.routes import auth_bp
    from leagues.routes import leagues_bp
    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(leagues_bp)

    # Add index route here so it's part of the app
    @app.route("/")
    def index():
        return render_template("index.html")

    # Create tables if they don't exist
    with app.app_context():
        db.create_all()

    return app

if __name__ == "__main__":
    app = create_app()
    app.run(debug=True)