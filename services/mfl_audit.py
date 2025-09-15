"""
DB audit helpers for outbound MFL calls.

Add this file safely now. Nothing happens until you call the functions from your MFL client.
"""

import time
from dataclasses import dataclass, asdict
from typing import Any, Callable, Dict, Optional


# ---------- Data shape we store (mirrors the api_call_logs schema) ----------

@dataclass
class ApiCallRow:
    user_id: Optional[int] = None
    league_id: Optional[int] = None
    host: Optional[str] = None
    method: str = "GET"
    endpoint: str = ""
    params: Optional[Dict[str, Any]] = None
    status_code: Optional[int] = None
    response_ms: Optional[int] = None
    ok: int = 0
    throttled: int = 0
    message: Optional[str] = None


# ---------- Generic persistence (pass a function to write the row) ----------

def record_api_call(
    *,
    persist: Callable[[Dict[str, Any]], None],
    user_id: Optional[int],
    league_id: Optional[int],
    host: Optional[str],
    method: str,
    endpoint: str,
    params: Optional[Dict[str, Any]],
    status_code: Optional[int],
    response_ms: Optional[int],
    ok: bool,
    throttled: bool = False,
    message: Optional[str] = None,
) -> None:
    """Persist one api_call_logs row using a caller-provided function."""
    row = ApiCallRow(
        user_id=user_id,
        league_id=league_id,
        host=host,
        method=(method or "GET").upper(),
        endpoint=endpoint or "",
        params=params or None,
        status_code=status_code,
        response_ms=response_ms,
        ok=1 if ok else 0,
        throttled=1 if throttled else 0,
        message=(message[:255] if (message and len(message) > 255) else message),
    )
    payload = asdict(row)
    # If your DB driver needs JSON string for params, do it inside your `persist` function.
    persist(payload)


# ---------- Optional: lightweight context manager for timing ----------

class api_call_timer:
    """
    Usage:
        with api_call_timer() as t:
            resp = requests.get(...)
        record_api_call(..., response_ms=t.ms, status_code=resp.status_code, ok=resp.ok, ...)
    """
    def __enter__(self):
        self._start = time.time()
        self.ms = None
        return self

    def __exit__(self, exc_type, exc, tb):
        self.ms = int((time.time() - self._start) * 1000)
        return False  # don't suppress exceptions


# ---------- Alternative path: pass a SQLAlchemy session & model ----------

def record_api_call_sqlalchemy(
    *,
    session: Any,
    ApiCallLogModel: Any,
    **kwargs
) -> None:
    """Same semantics as record_api_call, but using a SQLAlchemy session + mapped model."""
    row = ApiCallRow(
        user_id=kwargs.get("user_id"),
        league_id=kwargs.get("league_id"),
        host=kwargs.get("host"),
        method=(kwargs.get("method") or "GET").upper(),
        endpoint=kwargs.get("endpoint") or "",
        params=kwargs.get("params"),
        status_code=kwargs.get("status_code"),
        response_ms=kwargs.get("response_ms"),
        ok=1 if kwargs.get("ok") else 0,
        throttled=1 if kwargs.get("throttled") else 0,
        message=(kwargs.get("message")[:255] if kwargs.get("message") and len(kwargs.get("message")) > 255 else kwargs.get("message")),
    )
    session.add(ApiCallLogModel(**asdict(row)))
    session.commit()
