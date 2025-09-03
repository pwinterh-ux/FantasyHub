# config.py

# SECRET KEY (generated once using secrets.token_hex(32))
SECRET_KEY = "6212262fad20de9523f480ac9b9ba0272528022573f9456cdf874683c78ce04c"

# MySQL credentials
DB_USER = "pwindynasty"
DB_PASSWORD = "ncc746561"
DB_HOST = "pwindynasty.mysql.pythonanywhere-services.com"
DB_NAME = "pwindynasty$fantasyhubapp"

SQLALCHEMY_DATABASE_URI = f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}/{DB_NAME}"
SQLALCHEMY_TRACK_MODIFICATIONS = False
