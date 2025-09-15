"""
Terms / Privacy / AUP acceptance helpers for FantasyHub (Kingdom Tools LLC)

SAFE to add now: this module has no side-effects and does not import your app.
Use these helpers in auth/signup flows and before write actions.

Environment versions (optional):
  - TERMS_VERSION   (e.g., "2025-09-12")
  - PRIVACY_VERSION (e.g., "2025-09-12")
  - AUP_VERSION     (e.g., "2025-09-12")

Expected User fields (add via DB migration when ready):
  - tos_version : str | None
  - privacy_version : str | None
  - aup_version : str | None
  - terms_accepted_at : datetime | None
  - terms_accepted_ip : str | None
"""

from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Dict, Optional, Tuple


# —————————————————————————————————————————————————————————
# Version management
# —————————————————————————————————————————————————————————

DEFAULT_TERMS_VERSION = "2025-09-12"
DEFAULT_PRIVACY_VERSION = "2025-09-12"
DEFAULT_AUP_VERSION = "2025-09-12"


@dataclass(frozen=True)
class TermsVersions:
    terms: str
    privacy: str
    aup: str

    @classmethod
    def current(cls) -> "TermsVersions":
        """Read current versions from environment with sensible defaults."""
        return cls(
            terms=os.getenv("TERMS_VERSION", DEFAULT_TERMS_VERSION),
            privacy=os.getenv("PRIVACY_VERSION", DEFAULT_PRIVACY_VERSION),
            aup=os.getenv("AUP_VERSION", DEFAULT_AUP_VERSION),
        )


def acceptance_status(user: Any) -> Dict[str, Optional[str]]:
    """
    Return a dict showing what the user has accepted (versions or None).
    Non-invasive (read-only).
    """
    return {
        "tos_version": getattr(user, "tos_version", None),
        "privacy_version": getattr(user, "privacy_version", None),
        "aup_version": getattr(user, "aup_version", None),
        "terms_accepted_at": getattr(user, "terms_accepted_at", None),
        "terms_accepted_ip": getattr(user, "terms_accepted_ip", None),
    }


def needs_reaccept(user: Any, current: Optional[TermsVersions] = None) -> bool:
    """
    True if the user must accept the latest Terms/Privacy/AUP versions.
    """
    current = current or TermsVersions.current()
    u_tos = getattr(user, "tos_version", None)
    u_priv = getattr(user, "privacy_version", None)
    u_aup = getattr(user, "aup_version", None)
    return not (u_tos == current.terms and u_priv == current.privacy and u_aup == current.aup)


# —————————————————————————————————————————————————————————
# Recording acceptance
# —————————————————————————————————————————————————————————

def mark_accepted_in_memory(
    user: Any,
    client_ip: Optional[str] = None,
    current: Optional[TermsVersions] = None,
) -> Any:
    """
    Mutate the given user object in-memory to reflect acceptance of current versions.
    Does NOT commit to DB (handy for unit tests or when using your own session).
    """
    current = current or TermsVersions.current()
    setattr(user, "tos_version", current.terms)
    setattr(user, "privacy_version", current.privacy)
    setattr(user, "aup_version", current.aup)
    setattr(user, "terms_accepted_at", datetime.utcnow())
    if client_ip:
        setattr(user, "terms_accepted_ip", client_ip)
    return user


def mark_accepted_and_commit(
    db_session: Any,
    user: Any,
    client_ip: Optional[str] = None,
    current: Optional[TermsVersions] = None,
) -> Any:
    """
    Update the user with acceptance info and COMMIT using the provided db_session
    (e.g., SQLAlchemy session). Returns the user.
    """
    mark_accepted_in_memory(user, client_ip=client_ip, current=current)
    if db_session is not None:
        db_session.add(user)
        db_session.commit()
    return user


# —————————————————————————————————————————————————————————
# Helper for API responses / UI prompts
# —————————————————————————————————————————————————————————

def acceptance_prompt_context(user: Any) -> Dict[str, Any]:
    """
    Build a small dict you can return to the client when they need to accept terms.
    Useful to drive a modal: links/versions and the user's current status.
    """
    current = TermsVersions.current()
    status = acceptance_status(user)
    return {
        "needsTerms": needs_reaccept(user, current),
        "currentVersions": asdict(current),
        "userStatus": {
            "tos_version": status["tos_version"],
            "privacy_version": status["privacy_version"],
            "aup_version": status["aup_version"],
            "terms_accepted_at": (
                status["terms_accepted_at"].isoformat() if status["terms_accepted_at"] else None
            ),
        },
        # You can render these paths in your frontend templates
        "links": {
            "terms": "/legal/terms",
            "privacy": "/legal/privacy",
            "aup": "/legal/aup",
        },
        "message": "Please review and accept the Terms of Service, Privacy Policy, and Acceptable Use Policy to continue.",
    }


# —————————————————————————————————————————————————————————
# Lightweight predicate for write routes
# —————————————————————————————————————————————————————————

def has_current_acceptance(user: Any) -> bool:
    """Convenience shortcut for route guards."""
    return not needs_reaccept(user, TermsVersions.current())


# —————————————————————————————————————————————————————————
# Usage notes (for you)
# —————————————————————————————————————————————————————————
"""
How to wire this module later (examples):

1) On signup or login:
   from services.terms import needs_reaccept, acceptance_prompt_context, mark_accepted_and_commit

   if needs_reaccept(current_user):
       # Render a modal or page with links/checkbox and POST to /accept-terms
       ctx = acceptance_prompt_context(current_user)
       return render_template("accept_terms.html", **ctx)

2) On POST /accept-terms:
   mark_accepted_and_commit(db.session, current_user, client_ip=request.remote_addr)
   return redirect(next_url or "/")

3) Before any WRITE action (lineup submit / propose trades):
   from services.terms import has_current_acceptance
   if not has_current_acceptance(current_user):
       return jsonify({"error": "Please accept the Terms/Privacy/AUP to continue.", "needsTerms": True}), 402
"""
