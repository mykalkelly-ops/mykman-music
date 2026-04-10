"""
Minimal admin auth. One password (env var MYKMAN_ADMIN_PASSWORD), one cookie.

Not cryptographically strong — it's intended for a single-user hobby app.
Use HTTPS when you eventually expose it publicly.
"""
import os
import secrets
from datetime import datetime, timedelta
from fastapi import Request, HTTPException, Response

COOKIE_NAME = "mykman_admin"
SUB_COOKIE_NAME = "mykman_sub"
PASSWORD_ENV = "MYKMAN_ADMIN_PASSWORD"
DEFAULT_PASSWORD = "changeme"  # only used if env var not set

# In-memory token store (lost on restart; that's fine for a hobby app)
_VALID_TOKENS: set[str] = set()


def admin_password() -> str:
    return os.environ.get(PASSWORD_ENV, DEFAULT_PASSWORD)


def login(response: Response, password: str) -> bool:
    if not secrets.compare_digest(password, admin_password()):
        return False
    token = secrets.token_urlsafe(32)
    _VALID_TOKENS.add(token)
    response.set_cookie(
        COOKIE_NAME, token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30
    )
    return True


def logout(request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        _VALID_TOKENS.discard(token)
    response.delete_cookie(COOKIE_NAME)


def is_admin(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    return bool(token and token in _VALID_TOKENS)


def require_admin(request: Request) -> None:
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="admin only")


# ---------- Subscriber (paywall) auth ----------

# code -> subscriber_id, populated lazily
_VALID_SUB_CODES: dict[str, int] = {}
# subscriber_id -> last touch time, throttle DB writes
_SUB_LAST_TOUCH: dict[int, datetime] = {}


def _cleanup_expired(db) -> None:
    """Mark active subscribers whose expires_at lapsed as expired. Cheap."""
    from .models import Subscriber
    now = datetime.utcnow()
    try:
        (
            db.query(Subscriber)
            .filter(
                Subscriber.status == "active",
                Subscriber.expires_at.isnot(None),
                Subscriber.expires_at < now,
            )
            .update({Subscriber.status: "expired"}, synchronize_session=False)
        )
        db.commit()
    except Exception:
        db.rollback()


def unlock_subscriber(response: Response, code: str, db) -> bool:
    from .models import Subscriber
    if not code:
        return False
    code = code.strip()
    sub = (
        db.query(Subscriber)
        .filter(Subscriber.access_code == code, Subscriber.status == "active")
        .first()
    )
    if sub is None:
        return False
    _VALID_SUB_CODES[code] = sub.id
    sub.last_used_at = datetime.utcnow()
    _SUB_LAST_TOUCH[sub.id] = datetime.utcnow()
    db.commit()
    response.set_cookie(
        SUB_COOKIE_NAME,
        code,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 60,
    )
    return True


def is_subscriber(request: Request, db) -> bool:
    # Admins are always subscribers.
    if is_admin(request):
        return True
    code = request.cookies.get(SUB_COOKIE_NAME)
    if not code:
        return False
    _cleanup_expired(db)
    from .models import Subscriber
    sub_id = _VALID_SUB_CODES.get(code)
    sub = None
    if sub_id is not None:
        sub = db.get(Subscriber, sub_id)
    if sub is None:
        sub = db.query(Subscriber).filter(Subscriber.access_code == code).first()
        if sub is not None:
            _VALID_SUB_CODES[code] = sub.id
    if sub is None or sub.status != "active":
        return False
    # throttle last_used_at writes to once/hour
    now = datetime.utcnow()
    last = _SUB_LAST_TOUCH.get(sub.id)
    if last is None or (now - last) > timedelta(hours=1):
        try:
            sub.last_used_at = now
            db.commit()
        except Exception:
            db.rollback()
        _SUB_LAST_TOUCH[sub.id] = now
    return True


def lock_subscriber(response: Response) -> None:
    response.delete_cookie(SUB_COOKIE_NAME)
