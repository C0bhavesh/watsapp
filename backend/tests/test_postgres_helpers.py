import pytest

from app.store.postgres import _rows_affected


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("INSERT 0 1", 1),
        ("INSERT 0 0", 0),
        ("INSERT 0 10", 10),   # would be misread by endswith("0")
        ("UPDATE 3", 3),
        ("garbage", 0),
        ("", 0),
    ],
)
def test_rows_affected(tag: str, expected: int) -> None:
    assert _rows_affected(tag) == expected
