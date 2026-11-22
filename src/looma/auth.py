"""A username and password for the dashboard, for one person. The password's scrypt hash
and the username are kept in settings;
signing in gives the browser a random token in an HttpOnly cookie, and only the token's
hash is stored. Until a password is set the dashboard asks for one; `looma password` in
a terminal sets a new one, which is the way back in when it's forgotten."""

import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone

from looma.store import connect
from looma.store.db import now

SETTING = "password"
USERNAME = "username"
MAX_USERNAME = 40
COOKIE = "looma_login"
LOGIN_DAYS = 30
MIN_LENGTH = 8
# scrypt cost: about 50 ms per check, slow enough to make guessing a stolen hash expensive.
SCRYPT = {"n": 2**14, "r": 8, "p": 1}
# Wrong passwords allowed per minute before signing in pauses.
TRIES_PER_MINUTE = 5

_failures: list[float] = []


class AuthError(ValueError):
    pass


class TooManyTries(AuthError):
    pass


def password_set() -> bool:
    return _stored() is not None


def username() -> str | None:
    return _setting(USERNAME)


def set_password(new: str, current: str | None = None, check_current: bool = True,
                 name: str | None = None) -> None:
    """Sets the password, and the username when one is given (the first time it must be).
    Changing them needs the current password, except from the terminal. Every browser
    signed in with the old password is signed out."""
    if name is not None:
        name = name.strip()
        if not name or len(name) > MAX_USERNAME:
            raise AuthError(f"Pick a username of 1 to {MAX_USERNAME} characters.")
    elif username() is None:
        raise AuthError("Pick a username.")
    if len(new) < MIN_LENGTH:
        raise AuthError(f"Use at least {MIN_LENGTH} characters.")
    if check_current and password_set() and not verify(current or ""):
        raise AuthError("The current password is wrong.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(new.encode(), salt=salt, **SCRYPT)
    value = f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${salt.hex()}${digest.hex()}"
    conn = connect()
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (name, value) VALUES (?, ?)", (SETTING, value))
        if name is not None:
            conn.execute("INSERT OR REPLACE INTO settings (name, value) VALUES (?, ?)", (USERNAME, name))
        conn.execute("DELETE FROM logins")
    conn.close()


def set_username(name: str, current: str) -> None:
    """Changes the username, with the current password. Browsers stay signed in."""
    name = (name or "").strip()
    if not name or len(name) > MAX_USERNAME:
        raise AuthError(f"Pick a username of 1 to {MAX_USERNAME} characters.")
    if not verify(current or ""):
        raise AuthError("The current password is wrong.")
    conn = connect()
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (name, value) VALUES (?, ?)", (USERNAME, name))
    conn.close()


def verify(password: str) -> bool:
    stored = _stored()
    if stored is None:
        return False
    _, n, r, p, salt, digest = stored.split("$")
    attempt = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    return hmac.compare_digest(attempt, bytes.fromhex(digest))


def log_in(name: str, password: str) -> str:
    """A new login token for the right username and password. A password set before
    usernames existed has none yet: the first right sign-in keeps the name it was given."""
    moment = time.monotonic()
    _failures[:] = [t for t in _failures if moment - t < 60]
    if len(_failures) >= TRIES_PER_MINUTE:
        raise TooManyTries("Too many wrong tries. Wait a minute and try again.")
    name = name.strip()
    stored = username()
    # The password is always checked, so a wrong username takes as long as a wrong password.
    right = verify(password) and bool(name) and len(name) <= MAX_USERNAME
    if stored is not None:
        right = right and hmac.compare_digest(name.casefold().encode(), stored.casefold().encode())
    if not right:
        _failures.append(moment)
        # Not saying which one was wrong.
        raise AuthError("That username or password isn't right.")
    if stored is None:
        conn = connect()
        with conn:
            conn.execute("INSERT INTO settings (name, value) VALUES (?, ?)", (USERNAME, name))
        conn.close()
    return new_login()


def new_login() -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=LOGIN_DAYS)
    conn = connect()
    with conn:
        conn.execute("DELETE FROM logins WHERE expires_at < ?", (now(),))
        conn.execute("INSERT INTO logins (token_hash, created_at, expires_at) VALUES (?, ?, ?)",
                     (_hash(token), now(), expires.isoformat(timespec="seconds")))
    conn.close()
    return token


def signed_in(token: str | None) -> bool:
    if not token:
        return False
    conn = connect()
    row = conn.execute("SELECT 1 FROM logins WHERE token_hash = ? AND expires_at > ?", (_hash(token), now())).fetchone()
    conn.close()
    return row is not None


def log_out(token: str | None) -> None:
    if not token:
        return
    conn = connect()
    with conn:
        conn.execute("DELETE FROM logins WHERE token_hash = ?", (_hash(token),))
    conn.close()


def _stored() -> str | None:
    return _setting(SETTING)


def _setting(name: str) -> str | None:
    conn = connect()
    row = conn.execute("SELECT value FROM settings WHERE name = ?", (name,)).fetchone()
    conn.close()
    return row["value"] if row else None


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
