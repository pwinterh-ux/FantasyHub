from flask import Blueprint


waivers_bp = Blueprint(
    "waivers",
    __name__,
    url_prefix="/waivers",
)


from . import routes  # noqa: E402, F401
