from fastapi.testclient import TestClient

from looma.api.app import create_app


def client():
    return TestClient(create_app(), base_url="http://127.0.0.1")


def test_preferences_start_with_defaults_and_ask_about_uncommitted_work():
    prefs = client().get("/api/preferences").json()
    assert prefs["theme"] == "dark"
    assert prefs["uncommitted"] is None  # not picked yet: the dashboard asks
    assert prefs["shortcuts"]["view:home"] == "1" and prefs["shortcuts"]["theme"] == "t"


def test_changing_one_setting_keeps_the_others():
    api = client()
    assert api.put("/api/preferences", json={"uncommitted": True}).status_code == 200
    changed = api.put("/api/preferences", json={"theme": "light", "shortcuts": {"sync": "r"}}).json()
    assert changed["theme"] == "light" and changed["uncommitted"] is True
    assert changed["shortcuts"]["sync"] == "r" and changed["shortcuts"]["theme"] == "t"
    assert api.get("/api/preferences").json() == changed


def test_bad_settings_and_clashing_keys_are_refused():
    api = client()
    assert api.put("/api/preferences", json={"theme": "pink"}).status_code == 422
    assert api.put("/api/preferences", json={"shortcuts": {"fly": "f"}}).status_code == 400
    clash = api.put("/api/preferences", json={"shortcuts": {"sync": "t"}})
    assert clash.status_code == 400 and "theme" in clash.json()["detail"]
    # Turning a shortcut off frees its key.
    assert api.put("/api/preferences", json={"shortcuts": {"theme": "", "sync": "t"}}).status_code == 200


def test_number_keys_follow_the_menu_and_old_saved_defaults_move_with_them():
    import json

    from looma.store import connect

    # Saved by the first version: every key stored, numbers in the old menu order, plus one
    # key the person picked.
    old = {"view:home": "1", "view:overview": "4", "view:flow": "5", "view:map": "6", "view:constellation": "7",
           "view:changes": "8", "view:commits": "9", "theme": "t", "view:planner": "m"}
    conn = connect()
    with conn:
        conn.execute("INSERT INTO settings (name, value) VALUES ('preferences', ?)",
                     (json.dumps({"theme": "light", "uncommitted": True, "shortcuts": old}),))
    conn.close()
    prefs = client().get("/api/preferences").json()
    keys = prefs["shortcuts"]
    assert prefs["theme"] == "light" and prefs["uncommitted"] is True
    assert keys["view:kt"] == "4" and keys["view:ask"] == "5"
    assert keys["view:changes"] == "6" and keys["view:commits"] == "7" and keys["view:project"] == "8"
    assert keys["view:overview"] == "" and keys["view:map"] == ""  # sections of Home now
    assert "view:constellation" not in keys and "view:planner" not in keys  # screens since removed


def test_home_widgets_can_be_placed_sized_styled_and_reset():
    api = client()
    home = api.get("/api/preferences").json()["home"]
    kinds = [widget["type"] for widget in home["widgets"]]
    assert kinds == ["greeting", "project", "ai-today", "total-changes", "latest-commit", "ai-prompts", "changed-files", "branches", "activity"]
    dial = {"id": "dial", "type": "total-changes", "x": 6, "y": 0, "w": 6, "h": 4, "style": "outline",
            "accent": "teal", "title": "Size of this branch", "options": {"view": "number"}}
    note = {"id": "n1", "type": "commits", "x": 0, "y": 0, "w": 3, "h": 2, "options": {"text": "Ship Friday"}}
    changed = api.put("/api/preferences", json={"home": {"widgets": [dial, note]}}).json()
    assert changed["home"]["widgets"][0] == dial
    # Left out: the usual style, no accent, the widget's own title.
    assert changed["home"]["widgets"][1] == {**note, "style": "glass", "accent": "", "title": ""}
    assert api.get("/api/preferences").json()["home"] == changed["home"]
    assert api.post("/api/preferences/reset-home", json={}).json()["home"] == home


def test_home_widgets_that_dont_fit_are_refused():
    api = client()
    good = {"id": "a", "type": "branches", "x": 0, "y": 0, "w": 2, "h": 2}
    for bad in ({**good, "type": "sky"}, {**good, "x": 11, "w": 2}, {**good, "h": 0}, {**good, "style": "neon"},
                {**good, "accent": "gold"}, {**good, "title": "x" * 61}, {**good, "options": {"text": "x" * 2001}}):
        assert api.put("/api/preferences", json={"home": {"widgets": [bad]}}).status_code == 400, bad
    assert api.put("/api/preferences", json={"home": {"widgets": [good, good]}}).status_code == 400  # same id twice
    assert api.put("/api/preferences", json={"home": {"order": ["ai"], "hidden": []}}).status_code == 422


def test_shortcuts_go_back_to_the_defaults():
    api = client()
    api.put("/api/preferences", json={"shortcuts": {"sync": "r"}})
    assert api.post("/api/preferences/reset-shortcuts", json={}).json()["shortcuts"]["sync"] == "s"


def test_a_home_layout_from_before_widgets_could_be_placed_goes_back_to_the_default():
    from looma import preferences

    old = {"order": ["pulse", "ai", "branch", "flow"], "hidden": ["calendar"]}
    assert preferences._layout(old) == preferences._default_layout()
    # A widget of a kind this version doesn't have (or no longer has, like the calendar) is
    # dropped; the rest stay where they were.
    saved = {"widgets": [{"id": "x", "type": "gone", "x": 0, "y": 0, "w": 1, "h": 1},
                         {"id": "cal", "type": "calendar", "x": 0, "y": 0, "w": 2, "h": 2},
                         {"id": "c", "type": "branches", "x": 0, "y": 0, "w": 2, "h": 2}]}
    assert [widget["id"] for widget in preferences._layout(saved)["widgets"]] == ["c"]


def test_keys_saved_before_ask_joined_the_menu_move_down_one():
    import json

    from looma.store import connect

    saved = {"view:changes": "4", "view:commits": "5", "view:project": "6", "sync": "r"}
    conn = connect()
    with conn:
        conn.execute("INSERT INTO settings (name, value) VALUES ('preferences', ?)",
                     (json.dumps({"shortcuts": saved, "shortcuts_version": 3}),))
    conn.close()
    keys = client().get("/api/preferences").json()["shortcuts"]
    assert (keys["view:ask"], keys["view:changes"], keys["view:project"]) == ("5", "6", "8")
    assert keys["sync"] == "r"  # picked by the person: kept


def test_kt_has_a_key_and_keys_saved_before_it_move_down_one():
    import json

    from looma.store import connect

    saved = {"view:ask": "4", "view:changes": "5", "view:project": "7", "theme": "x"}
    conn = connect()
    with conn:
        conn.execute("INSERT INTO settings (name, value) VALUES ('preferences', ?)",
                     (json.dumps({"shortcuts": saved, "shortcuts_version": 4}),))
    conn.close()
    keys = client().get("/api/preferences").json()["shortcuts"]
    assert (keys["view:kt"], keys["view:ask"], keys["view:changes"], keys["view:project"]) == ("4", "5", "6", "8")
    assert keys["theme"] == "x"
    # The Settings screen offers "Go to KT", so its key can be changed.
    changed = client().put("/api/preferences", json={"shortcuts": {**keys, "view:kt": "q"}})
    assert changed.status_code == 200 and changed.json()["shortcuts"]["view:kt"] == "q"


def test_keep_diffs_days_defaults_to_ten_and_checks_its_value():
    from fastapi.testclient import TestClient
    from looma.api.app import create_app

    api = TestClient(create_app(), base_url="http://127.0.0.1")
    assert api.get("/api/preferences").json()["keep_diffs_days"] == 10
    assert api.put("/api/preferences", json={"keep_diffs_days": 30}).json()["keep_diffs_days"] == 30
    assert api.get("/api/preferences").json()["keep_diffs_days"] == 30
    assert api.put("/api/preferences", json={"keep_diffs_days": -1}).status_code == 422


def test_the_walkthrough_shows_until_it_is_finished_once():
    api = client()
    assert api.get("/api/preferences").json()["tour_done"] is False
    assert api.put("/api/preferences", json={"tour_done": True}).json()["tour_done"] is True
    assert api.get("/api/preferences").json()["tour_done"] is True
