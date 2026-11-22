"""Readable prompts. Claude app sessions hand Claude Code the user's message wrapped in
routing markup (<wake>, <relay>, <cited>); the raw text is stored as is, and the person's
own words are pulled out only for display."""

import html
import re

_PATTERNS = [
    # The person's own message, quoted by the app.
    re.compile(r'<cited\b[^>]*\bauthor="user"[^>]*>(.*?)</cited>', re.S),
    re.compile(r'<message\b[^>]*\bfrom="human"[^>]*>(.*?)</message>', re.S),
    # A note from the app's coordinator, which paraphrases the request.
    re.compile(r"<note>(.*?)</note>", re.S),
]
_TAG = re.compile(r"<[^>]+>")


def readable(prompt: str) -> str:
    if not prompt.lstrip().startswith("<"):
        return prompt
    for pattern in _PATTERNS:
        parts = [_clean(m) for m in pattern.findall(prompt)]
        parts = [p for p in parts if p]
        if parts:
            return "\n\n".join(parts)
    return _clean(prompt) or prompt


def _clean(text: str) -> str:
    return html.unescape(_TAG.sub("", text)).strip()
