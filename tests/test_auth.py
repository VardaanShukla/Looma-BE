from fastapi.testclient import TestClient

from looma import auth, cli
from looma.api.app import create_app
from looma.store import connect


def client():
    return TestClient(create_app(), base_url="http://127.0.0.1")


def test_no_password_leaves_the_dashboard_open():
    api = client()
    assert api.get("/api/auth").json() == {"password_set": False, "signed_in": False}
    assert api.get("/api/repos").status_code == 200


def test_first_password_signs_in_and_locks_everything_else():
    me = client()
    assert me.put("/api/auth/password", json={"username": "vardaan", "password": "short"}).status_code == 400
    # The first time needs a username too.
    assert me.put("/api/auth/password", json={"password": "correct horse"}).status_code == 400
    assert me.put("/api/auth/password", json={"username": " Vardaan ", "password": "correct horse"}).json()["signed_in"] is True
    assert auth.username() == "Vardaan"
    assert me.get("/api/repos").status_code == 200

    stranger = client()
    assert stranger.get("/api/repos").status_code == 401
    assert stranger.get("/openapi.json").status_code == 401
    assert stranger.get("/api/auth").json() == {"password_set": True, "signed_in": False}
    # Nobody else can replace the password.
    assert stranger.put("/api/auth/password", json={"username": "me", "password": "mine now!"}).status_code == 401

    assert stranger.post("/api/auth/login", json={"username": "vardaan", "password": "wrong one"}).status_code == 401
    wrong_name = stranger.post("/api/auth/login", json={"username": "someone", "password": "correct horse"})
    assert wrong_name.status_code == 401 and "username or password" in wrong_name.json()["detail"]
    # Usernames ignore case.
    signed = stranger.post("/api/auth/login", json={"username": "VARDAAN", "password": "correct horse"})
    assert signed.status_code == 200 and "HttpOnly" in signed.headers["set-cookie"]
    cookie = stranger.cookies.get(auth.COOKIE)
    assert stranger.get("/api/repos").status_code == 200

    stranger.post("/api/auth/logout", json={})
    assert stranger.get("/api/repos").status_code == 401
    # The old token is dead even if someone kept it.
    kept = client()
    kept.cookies.set(auth.COOKIE, cookie)
    assert kept.get("/api/repos").status_code == 401


def test_changing_the_password_needs_the_current_one_and_signs_others_out():
    me = client()
    me.put("/api/auth/password", json={"username": "me", "password": "first password"})
    other = client()
    other.post("/api/auth/login", json={"username": "me", "password": "first password"})
    assert me.put("/api/auth/password", json={"password": "second password", "current": "nope"}).status_code == 400
    assert me.put("/api/auth/password", json={"password": "second password", "current": "first password"}).status_code == 200
    assert me.get("/api/repos").status_code == 200
    assert other.get("/api/repos").status_code == 401


def test_guessing_is_slowed_down(monkeypatch):
    monkeypatch.setattr(auth, "_failures", [])
    client().put("/api/auth/password", json={"username": "me", "password": "correct horse"})
    api = client()
    codes = [api.post("/api/auth/login", json={"username": "me", "password": f"guess {i}"}).status_code for i in range(6)]
    assert codes == [401] * 5 + [429]
    assert api.post("/api/auth/login", json={"username": "me", "password": "correct horse"}).status_code == 429


def test_password_from_the_terminal(monkeypatch, capsys):
    client().put("/api/auth/password", json={"username": "me", "password": "forgotten one"})
    answers = iter(["fresh password", "fresh password"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    # Enter keeps the current username.
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert cli.main(["password"]) == 0
    assert auth.verify("fresh password") and not auth.verify("forgotten one")
    assert auth.username() == "me"


def test_a_password_from_before_usernames_keeps_the_first_name_signed_in_with():
    # Older versions stored only the password.
    auth.set_password("older password", check_current=False, name="temporary")
    conn = connect()
    with conn:
        conn.execute("DELETE FROM settings WHERE name = ?", (auth.USERNAME,))
    conn.close()
    api = client()
    assert api.post("/api/auth/login", json={"username": "  ", "password": "older password"}).status_code == 401
    assert api.post("/api/auth/login", json={"username": "Vardaan", "password": "older password"}).status_code == 200
    assert auth.username() == "Vardaan"
    assert client().post("/api/auth/login", json={"username": "other", "password": "older password"}).status_code == 401
