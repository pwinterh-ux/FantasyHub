# legal_versions.py
"""
Single source of truth for current legal document versions.
Bump these constants whenever you materially update your docs.
"""

TOS_VERSION = "2025-09-13"
PRIVACY_VERSION = "2025-09-13"
AUP_VERSION = "2025-09-13"


def current_versions() -> dict[str, str]:
    return {
        "tos": TOS_VERSION,
        "privacy": PRIVACY_VERSION,
        "aup": AUP_VERSION,
    }
