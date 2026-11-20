from pathlib import Path

import pytest

from webskrap.driver import targets


def test_pointer_is_saved_per_page_and_dropped_with_closed_pages(tmp_path: Path) -> None:
    path = tmp_path / targets.POINTER_FILE

    targets.save_pointer(path, "a", (10.0, 20.5), {"a", "b"})
    targets.save_pointer(path, "b", (3.0, 4.0), {"a", "b"})
    targets.save_pointer(path, "b", (5.0, 6.0), {"b"})

    assert targets.saved_pointer(path, "a") is None
    assert targets.saved_pointer(path, "b") == (5.0, 6.0)


@pytest.mark.parametrize(
    "content", ["not json", "[]", '{"a": [1]}', '{"a": ["x", 2]}', '{"a": [1, NaN]}']
)
def test_invalid_saved_pointer_is_ignored(tmp_path: Path, content: str) -> None:
    path = tmp_path / targets.POINTER_FILE
    path.write_text(content, encoding="utf-8")

    assert targets.saved_pointer(path, "a") is None
