"""The dashboard's settings: its look, whether uncommitted work shows from the start, the
keyboard shortcuts and Home's widgets: which, where, how big, in which style and with which
options. Kept in the database, so every browser that signs in gets them.
Unknown names and bad values are refused rather than stored."""

import json

from looma.store import connect

SETTING = "preferences"
THEMES = ("dark", "light")
# What a shortcut can do. Going to a screen is "view:<screen>".
# The side menu's screens in order, numbered 1, 2, … by default; then the screens that are
# sections of Home (This branch, Branch flow, Project map), which have no key unless set.
MENU = ("home", "digest", "summary", "kt", "ask", "changes", "commits", "project")
VIEWS = (*MENU, "overview", "flow", "map", "help", "settings")
ACTIONS = (*(f"view:{view}" for view in VIEWS), "theme", "uncommitted", "sync", "next-file", "previous-file")
DEFAULT_SHORTCUTS = {
    **{f"view:{view}": str(n) for n, view in enumerate(MENU, start=1)},
    "view:settings": ",",
    "theme": "t",
    "uncommitted": "u",
    "sync": "s",
    "next-file": "j",
    "previous-file": "k",
}
# The defaults before This branch, Branch flow and Project map moved onto Home. A saved key
# equal to one of these was never picked by the person, so it follows the new defaults.
OLD_DEFAULTS = {
    1: {**{f"view:{view}": str(n) for n, view in enumerate(
        ("home", "digest", "summary", "overview", "flow", "map", "constellation", "changes", "commits"), start=1)},
        "view:settings": ",", "theme": "t", "uncommitted": "u", "sync": "s", "next-file": "j", "previous-file": "k"},
    # Before Constellation and My day were removed.
    2: {**{f"view:{view}": str(n) for n, view in enumerate(
        ("home", "digest", "summary", "constellation", "changes", "commits", "planner", "project"), start=1)},
        "view:settings": ",", "theme": "t", "uncommitted": "u", "sync": "s", "next-file": "j", "previous-file": "k"},
    # Before Ask joined the menu, under Summary.
    3: {**{f"view:{view}": str(n) for n, view in enumerate(
        ("home", "digest", "summary", "changes", "commits", "project"), start=1)},
        "view:settings": ",", "theme": "t", "uncommitted": "u", "sync": "s", "next-file": "j", "previous-file": "k"},
    # Before KT joined the menu, under Summary.
    4: {**{f"view:{view}": str(n) for n, view in enumerate(
        ("home", "digest", "summary", "ask", "changes", "commits", "project"), start=1)},
        "view:settings": ",", "theme": "t", "uncommitted": "u", "sync": "s", "next-file": "j", "previous-file": "k"},
}
SHORTCUTS_VERSION = 5
# The kinds of widget Home can show (each can be placed more than once), and how they're laid
# out: on a grid COLUMNS wide, each at a column and row, so many columns wide and rows tall.
# Only what tracks git and AI changes: branch flow, the project map, projects, recording and
# pull requests have screens of their own, and saved widgets of other kinds are dropped.
WIDGETS = (
    "greeting", "project", "ai-today", "total-changes", "activity", "latest-commit", "branches",
    "changed-files", "commits", "ai-prompts",
)
COLUMNS = 12
MAX_ROWS = 12  # the tallest a widget can be
MAX_ROW = 400  # the lowest row a widget can start on
MAX_WIDGETS = 60
# How a widget's panel looks, and the colour its highlights take ("" is the theme's own).
STYLES = ("glass", "solid", "outline", "tinted", "bare")
ACCENTS = ("", "blue", "teal", "green", "amber", "red", "violet", "pink")
MAX_TITLE = 60
MAX_ID = 40
# Each widget's own options: a few short values, or a longer text (a note's).
MAX_OPTIONS = 20
MAX_OPTION = 2000
# Home as it first opens: the greeting and the project on show, today's AI work, then three
# across: the branch's size, its latest commits and today's AI prompts; then the changed
# files, the branches and who committed today. Each panel is a third or half of the width, so
# none of them stands mostly empty. The greeting and the project's name sit on the page itself.
DEFAULT_WIDGETS = (
    ("greeting", 0, 0, 8, 2, "bare"), ("project", 8, 0, 4, 2, "bare"),
    ("ai-today", 0, 2, 12, 2, "glass"),
    ("total-changes", 0, 4, 4, 7, "glass"), ("latest-commit", 4, 4, 4, 7, "glass"), ("ai-prompts", 8, 4, 4, 7, "glass"),
    ("changed-files", 0, 11, 6, 6, "glass"), ("branches", 6, 11, 3, 6, "glass"), ("activity", 9, 11, 3, 6, "glass"),
)
DEFAULTS = {
    "theme": "dark",
    # None until the person picks: the dashboard asks once.
    "uncommitted": None,
    "shortcuts": DEFAULT_SHORTCUTS,
    # Days a change's lines are kept; older ones keep their prompt and counts. 0: for good.
    "keep_diffs_days": 10,
}
MAX_KEEP_DAYS = 3650
MAX_KEY = 20
# The AIs that can write explanations (see providers.py), and the longest model name taken.
AI_PROVIDERS = ("claude", "codex", "ollama", "antigravity", "hosted")
MAX_MODEL = 100


class PreferenceError(ValueError):
    pass


def get() -> dict:
    conn = connect()
    row = conn.execute("SELECT value FROM settings WHERE name = ?", (SETTING,)).fetchone()
    conn.close()
    saved = json.loads(row["value"]) if row else {}
    chosen = saved.get("shortcuts", {})
    old = OLD_DEFAULTS.get(saved.get("shortcuts_version", 1), {}) if saved.get("shortcuts_version") != SHORTCUTS_VERSION else {}
    chosen = {action: key for action, key in chosen.items() if action in ACTIONS and old.get(action) != key}
    # Keys the person picked win; a default whose key they gave to something else is off.
    taken = {key for key in chosen.values() if key}
    defaults = {}
    for action in ACTIONS:
        key = DEFAULT_SHORTCUTS.get(action, "")
        defaults[action] = "" if key in taken else key
    ai = saved.get("ai") or {}
    keep = saved.get("keep_diffs_days")
    return {"theme": saved.get("theme", DEFAULTS["theme"]), "uncommitted": saved.get("uncommitted"),
            "tour_done": saved.get("tour_done") is True,
            "keep_diffs_days": keep if _days(keep) else DEFAULTS["keep_diffs_days"],
            "shortcuts": {**defaults, **chosen}, "home": _layout(saved.get("home")),
            "ai": {"provider": ai.get("provider") if ai.get("provider") in AI_PROVIDERS else "claude",
                   "models": {k: v for k, v in (ai.get("models") or {}).items() if k in AI_PROVIDERS and isinstance(v, str)}}}


def update(changes: dict) -> dict:
    """Merges the given settings into the saved ones. Shortcuts merge one action at a
    time; an empty key turns that shortcut off."""
    current = get()
    for name, value in changes.items():
        if name == "theme":
            if value not in THEMES:
                raise PreferenceError(f"theme is one of {', '.join(THEMES)}")
            current["theme"] = value
        elif name == "uncommitted":
            if not isinstance(value, bool):
                raise PreferenceError("uncommitted is true or false")
            current["uncommitted"] = value
        elif name == "tour_done":
            if not isinstance(value, bool):
                raise PreferenceError("tour_done is true or false")
            current["tour_done"] = value
        elif name == "keep_diffs_days":
            if not _days(value):
                raise PreferenceError(f"keep_diffs_days is a whole number of days from 0 (keep for good) to {MAX_KEEP_DAYS}")
            current["keep_diffs_days"] = value
        elif name == "shortcuts":
            current["shortcuts"] = _shortcuts({**current["shortcuts"], **_checked(value)})
        elif name == "home":
            current["home"] = _checked_layout(value)
        elif name == "ai":
            current["ai"] = _ai({**current["ai"], **value} if isinstance(value, dict) else value, current["ai"])
        else:
            raise PreferenceError(f"no setting called {name!r}")
    _save(current)
    return current


def _days(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_KEEP_DAYS


def reset_shortcuts() -> dict:
    current = get()
    current["shortcuts"] = dict(DEFAULT_SHORTCUTS)
    _save(current)
    return current


def _checked(value) -> dict:
    if not isinstance(value, dict):
        raise PreferenceError("shortcuts maps an action to a key")
    for action, key in value.items():
        if action not in ACTIONS:
            raise PreferenceError(f"no action called {action!r}")
        if not isinstance(key, str) or len(key) > MAX_KEY:
            raise PreferenceError(f"the key for {action} must be text of at most {MAX_KEY} characters")
    return value


def _shortcuts(shortcuts: dict) -> dict:
    taken: dict[str, str] = {}
    for action, key in shortcuts.items():
        if not key:
            continue
        if key in taken:
            raise PreferenceError(f"{key!r} is already the shortcut for {taken[key]}")
        taken[key] = action
    return shortcuts


def _ai(value, current: dict) -> dict:
    """Which AI writes explanations, and the model picked for each; models merge one AI at a
    time, and an empty model means that AI's default."""
    if not isinstance(value, dict):
        raise PreferenceError("ai is {provider, models}")
    provider = value.get("provider", current["provider"])
    if provider not in AI_PROVIDERS:
        raise PreferenceError(f"the AI is one of {', '.join(AI_PROVIDERS)}")
    models = value.get("models", {})
    if not isinstance(models, dict) or any(k not in AI_PROVIDERS or not isinstance(v, str) or len(v) > MAX_MODEL
                                           for k, v in models.items()):
        raise PreferenceError(f"models maps an AI to a model name of at most {MAX_MODEL} characters")
    merged = {**current["models"], **{k: v.strip() for k, v in models.items()}}
    return {"provider": provider, "models": {k: v for k, v in merged.items() if v}}


def _default_layout() -> dict:
    return {"widgets": [{"id": kind, "type": kind, "x": x, "y": y, "w": w, "h": h, "style": style, "accent": "",
                         "title": "", "options": {}} for kind, x, y, w, h, style in DEFAULT_WIDGETS]}


def _layout(saved) -> dict:
    """The saved layout, or the default when there is none or it is from before widgets could
    be placed freely ({order, hidden}). Widgets of a kind this version doesn't have are dropped."""
    if not isinstance(saved, dict) or not isinstance(saved.get("widgets"), list):
        return _default_layout()
    widgets = []
    for widget in saved["widgets"]:
        try:
            widgets.append(_widget(widget))
        except PreferenceError:
            continue
    return {"widgets": widgets}


def _widget(value) -> dict:
    if not isinstance(value, dict):
        raise PreferenceError("a widget is {id, type, x, y, w, h, style, accent, title, options}")
    kind = value.get("type")
    if kind not in WIDGETS:
        raise PreferenceError(f"no widget called {kind!r}; they are {', '.join(WIDGETS)}")
    wid = value.get("id")
    if not isinstance(wid, str) or not wid or len(wid) > MAX_ID:
        raise PreferenceError(f"a widget's id is text of 1 to {MAX_ID} characters")
    place = {}
    for name, low, high in (("x", 0, COLUMNS - 1), ("y", 0, MAX_ROW), ("w", 1, COLUMNS), ("h", 1, MAX_ROWS)):
        number = value.get(name)
        if not isinstance(number, int) or isinstance(number, bool) or not low <= number <= high:
            raise PreferenceError(f"a widget's {name} is a whole number from {low} to {high}")
        place[name] = number
    if place["x"] + place["w"] > COLUMNS:
        raise PreferenceError(f"a widget can't reach past column {COLUMNS}")
    style = value.get("style", "glass")
    if style not in STYLES:
        raise PreferenceError(f"a widget's style is one of {', '.join(STYLES)}")
    accent = value.get("accent", "")
    if accent not in ACCENTS:
        raise PreferenceError(f"a widget's accent is one of {', '.join(a for a in ACCENTS if a)}, or empty")
    title = value.get("title", "")
    if not isinstance(title, str) or len(title) > MAX_TITLE:
        raise PreferenceError(f"a widget's title is text of at most {MAX_TITLE} characters")
    options = value.get("options", {})
    if not isinstance(options, dict) or len(options) > MAX_OPTIONS or any(
            not isinstance(k, str) or len(k) > MAX_ID or isinstance(v, (dict, list)) or v is None
            or (isinstance(v, str) and len(v) > MAX_OPTION) for k, v in options.items()):
        raise PreferenceError(f"a widget's options are at most {MAX_OPTIONS} names, each a number, true or false, "
                              f"or text of at most {MAX_OPTION} characters")
    return {"id": wid, "type": kind, **place, "style": style, "accent": accent, "title": title.strip(),
            "options": options}


def _checked_layout(value) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("widgets"), list):
        raise PreferenceError("home is {widgets}, a list of placed widgets")
    if len(value["widgets"]) > MAX_WIDGETS:
        raise PreferenceError(f"Home holds at most {MAX_WIDGETS} widgets")
    widgets = [_widget(widget) for widget in value["widgets"]]
    ids = [widget["id"] for widget in widgets]
    if len(set(ids)) != len(ids):
        raise PreferenceError("two widgets have the same id")
    return {"widgets": widgets}


def reset_home() -> dict:
    current = get()
    current["home"] = _default_layout()
    _save(current)
    return current


def ai_choice() -> tuple[str, str | None]:
    """The AI that writes explanations and the model picked for it (None: its default)."""
    ai = get()["ai"]
    return ai["provider"], ai["models"].get(ai["provider"]) or None


def _save(preferences: dict) -> None:
    # Only the keys the person changed are stored, so a later version's defaults reach the rest.
    chosen = {action: key for action, key in preferences["shortcuts"].items() if DEFAULT_SHORTCUTS.get(action, "") != key}
    stored = {"theme": preferences["theme"], "uncommitted": preferences["uncommitted"], "tour_done": preferences["tour_done"],
              "keep_diffs_days": preferences["keep_diffs_days"], "shortcuts": chosen,
              "shortcuts_version": SHORTCUTS_VERSION, "ai": preferences["ai"], "home": preferences["home"]}
    conn = connect()
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (name, value) VALUES (?, ?)", (SETTING, json.dumps(stored)))
    conn.close()
