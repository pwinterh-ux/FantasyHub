# app_local.py -- minimal app to test templates/static without DB
from flask import Flask, render_template
import os

# Force Flask to use absolute path for templates
template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
print("Template folder being used:", template_dir)

app = Flask(
    "FantasyHubLocal",
    static_folder="static",
    template_folder=template_dir
)


@app.route("/")
def index():
    return render_template("index.html", current_year=2025)

@app.route("/home")
def home():
    return render_template("home.html")

@app.route("/leagues")
def leagues():
    # simple placeholder context so my_leagues.html can render
    dummy_leagues = [
        {
            "league_name": "All Play League",
            "commissioner": "Pwin",
            "record": "0-0-0",
            "standing": 1,
            "roster_spots": "9: 1QB 2-4RB 3-5WR 1-3TE"
        }
    ]
    return render_template("my_leagues.html", leagues=dummy_leagues, current_year=2025)

if __name__ == "__main__":
    app.run(debug=True)
