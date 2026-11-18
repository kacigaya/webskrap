"""Translate key combinations into CDP input events."""

from __future__ import annotations

from typing import Any

from webskrap.driver.cdp import CDPPage
from webskrap.errors import ErrorCode, WebSkrapError

#: Non-character keys: CDP ``key``, ``code``, Windows virtual key code, text.
_SPECIAL_KEYS: dict[str, tuple[str, str, int, str]] = {
    "Enter": ("Enter", "Enter", 13, "\r"),
    "Tab": ("Tab", "Tab", 9, ""),
    "Escape": ("Escape", "Escape", 27, ""),
    "Backspace": ("Backspace", "Backspace", 8, ""),
    "Delete": ("Delete", "Delete", 46, ""),
    "Insert": ("Insert", "Insert", 45, ""),
    "Space": (" ", "Space", 32, " "),
    "ArrowLeft": ("ArrowLeft", "ArrowLeft", 37, ""),
    "ArrowUp": ("ArrowUp", "ArrowUp", 38, ""),
    "ArrowRight": ("ArrowRight", "ArrowRight", 39, ""),
    "ArrowDown": ("ArrowDown", "ArrowDown", 40, ""),
    "Home": ("Home", "Home", 36, ""),
    "End": ("End", "End", 35, ""),
    "PageUp": ("PageUp", "PageUp", 33, ""),
    "PageDown": ("PageDown", "PageDown", 34, ""),
    **{f"F{n}": (f"F{n}", f"F{n}", 111 + n, "") for n in range(1, 13)},
}
#: US layout keys with a shifted character: unshifted, shifted, ``code``, key code.
_US_SHIFT_PAIRS: tuple[tuple[str, str, str, int], ...] = (
    ("`", "~", "Backquote", 192),
    ("1", "!", "Digit1", 49),
    ("2", "@", "Digit2", 50),
    ("3", "#", "Digit3", 51),
    ("4", "$", "Digit4", 52),
    ("5", "%", "Digit5", 53),
    ("6", "^", "Digit6", 54),
    ("7", "&", "Digit7", 55),
    ("8", "*", "Digit8", 56),
    ("9", "(", "Digit9", 57),
    ("0", ")", "Digit0", 48),
    ("-", "_", "Minus", 189),
    ("=", "+", "Equal", 187),
    ("[", "{", "BracketLeft", 219),
    ("]", "}", "BracketRight", 221),
    ("\\", "|", "Backslash", 220),
    (";", ":", "Semicolon", 186),
    ("'", '"', "Quote", 222),
    (",", "<", "Comma", 188),
    (".", ">", "Period", 190),
    ("/", "?", "Slash", 191),
)
#: Character: (``code``, key code, the character Shift turns it into).
_US_KEYS: dict[str, tuple[str, int, str]] = {
    **{low: (code, key_code, high) for low, high, code, key_code in _US_SHIFT_PAIRS},
    **{high: (code, key_code, high) for _low, high, code, key_code in _US_SHIFT_PAIRS},
    **{
        letter: (f"Key{letter.upper()}", ord(letter.upper()), letter.upper())
        for letter in "abcdefghijklmnopqrstuvwxyz"
    },
    **{letter: (f"Key{letter}", ord(letter), letter) for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
}
#: Modifier name: (CDP modifier bit, key code, ``code``).
_MODIFIERS: dict[str, tuple[int, int, str]] = {
    "Alt": (1, 18, "AltLeft"),
    "Control": (2, 17, "ControlLeft"),
    "Meta": (4, 91, "MetaLeft"),
    "Shift": (8, 16, "ShiftLeft"),
}


def key_events(combo: str) -> list[dict[str, Any]]:
    """Return the ``Input.dispatchKeyEvent`` calls for a Playwright key combo.

    ``combo`` is Playwright's syntax: modifiers and one key joined by ``+``,
    such as ``Enter``, ``Shift+Tab`` or ``Control+a``. Modifiers go down in
    order and up in reverse around the key. A key with Control, Alt or Meta
    held inserts no text, as on a real keyboard.

    Raises:
        WebSkrapError: ``usage`` for an unknown key name.
    """
    # "+" alone and a trailing "++" (Shift++) name the plus key itself.
    if combo == "+":
        modifier_names, key_name = [], "+"
    elif combo.endswith("++"):
        modifier_names, key_name = combo[:-2].split("+"), "+"
    else:
        *modifier_names, key_name = combo.split("+")
    for modifier in modifier_names:
        if modifier not in _MODIFIERS:
            msg = f"unknown modifier '{modifier}' in key '{combo}'"
            raise WebSkrapError(msg, code=ErrorCode.USAGE)
    if key_name in _SPECIAL_KEYS:
        key, code, key_code, text = _SPECIAL_KEYS[key_name]
    elif len(key_name) == 1:
        key, text = key_name, key_name
        code, key_code, _shifted = _US_KEYS.get(key_name, ("", 0, key_name))
    else:
        msg = f"unknown key '{key_name}': use a single character or a name like Enter"
        raise WebSkrapError(msg, code=ErrorCode.USAGE)
    mask = 0
    events: list[dict[str, Any]] = []
    for modifier in modifier_names:
        bit, modifier_code, modifier_physical = _MODIFIERS[modifier]
        mask |= bit
        events.append(
            {
                "type": "rawKeyDown",
                "key": modifier,
                "code": modifier_physical,
                "windowsVirtualKeyCode": modifier_code,
                "modifiers": mask,
            }
        )
    if mask & 8 and key in _US_KEYS:
        # Shift turns the key into its US-layout partner: a to A, 1 to !.
        key = text = _US_KEYS[key][2]
    if mask & (1 | 2 | 4):
        text = ""
    down = {
        "type": "keyDown" if text else "rawKeyDown",
        "key": key,
        "code": code,
        "windowsVirtualKeyCode": key_code,
        "modifiers": mask,
    }
    if text:
        down["text"] = down["unmodifiedText"] = text
    events.append(down)
    up = {name: value for name, value in down.items() if name not in ("text", "unmodifiedText")}
    events.append({**up, "type": "keyUp"})
    for modifier in reversed(modifier_names):
        bit, modifier_code, modifier_physical = _MODIFIERS[modifier]
        mask &= ~bit
        events.append(
            {
                "type": "keyUp",
                "key": modifier,
                "code": modifier_physical,
                "windowsVirtualKeyCode": modifier_code,
                "modifiers": mask,
            }
        )
    return events


async def press(page: CDPPage, combo: str) -> None:
    """Press a key combo such as ``Enter`` or ``Control+a``; see :func:`key_events`."""
    for event in key_events(combo):
        await page.send("Input.dispatchKeyEvent", event)
