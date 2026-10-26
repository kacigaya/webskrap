from __future__ import annotations

import pytest

from webskrap import text_search
from webskrap.errors import ErrorCode, WebSkrapError
from webskrap.text_search import search_lines, validate_search

PAGE = """Lamp Slim  499 €

Bulb only
Lamp Pro 799 €
   Cable 69 €
Lamp Mini 449 €
"""


async def test_matches_come_with_context_on_non_blank_lines() -> None:
    found = await search_lines(PAGE, r"Lamp \w+ \d+", context=1)
    assert found["matches_total"] == 2
    assert not found["matches_truncated"]
    first, second = found["matches"]
    assert first == {
        "line": 3,
        "text": "Lamp Pro 799 €",
        "before": ["Bulb only"],
        "after": ["Cable 69 €"],
    }
    assert second["line"] == 5 and second["after"] == []


async def test_match_count_and_line_length_are_capped() -> None:
    text = "\n".join(f"hit {index} " + "x" * 1000 for index in range(30))
    found = await search_lines(text, "hit", context=0, max_matches=5)
    assert found["matches_total"] == 30
    assert found["matches_truncated"]
    assert len(found["matches"]) == 5
    assert all(len(match["text"]) == text_search.MAX_LINE_CHARS for match in found["matches"])
    assert found["matches"][0]["before"] == []


async def test_backtracking_pattern_is_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(text_search, "PATTERN_TIMEOUT_S", 0.5)
    with pytest.raises(WebSkrapError) as error:
        await search_lines("a" * 40 + "!", r"(a+)+$")
    assert error.value.code is ErrorCode.USAGE
    assert "nested" in str(error.value)


@pytest.mark.parametrize(
    ("pattern", "context", "max_matches", "message"),
    [
        ("  ", 1, 20, "blank"),
        ("a" * 201, 1, 20, "keep it under"),
        ("(unclosed", 1, 20, "invalid pattern"),
        ("ok", 4, 20, "context"),
        ("ok", -1, 20, "context"),
        ("ok", 1, 0, "max_matches"),
        ("ok", 1, 51, "max_matches"),
    ],
)
def test_unusable_requests_are_rejected(pattern, context, max_matches, message) -> None:
    with pytest.raises(WebSkrapError) as error:
        validate_search(pattern, context=context, max_matches=max_matches)
    assert error.value.code is ErrorCode.USAGE
    assert message in str(error.value)
