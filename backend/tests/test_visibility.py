"""红线①：VisibilityGuard 前视防火墙。

验证：``mask`` 严格截断于 cutoff；``assert_no_future`` 能击穿未来数据。
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.core.errors import VisibilityViolation
from app.data.visibility import VisibilityGuard, mask_df


@pytest.fixture()
def frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07"]),
            "close": [10.0, 10.5, 10.9, 11.1],
        }
    )


def test_mask_keeps_only_past(frame: pd.DataFrame) -> None:
    """只保留 <= cutoff 的行。"""
    out = VisibilityGuard("2020-01-03").mask(frame)
    assert list(out["date"].dt.strftime("%Y-%m-%d")) == ["2020-01-02", "2020-01-03"]
    assert out["close"].max() == 10.5


def test_mask_is_inclusive_of_cutoff(frame: pd.DataFrame) -> None:
    """cutoff 当天（含）可见。"""
    out = mask_df(frame, "2020-01-06")
    assert out["date"].max() == pd.Timestamp("2020-01-06")


def test_mask_does_not_mutate_input(frame: pd.DataFrame) -> None:
    """不修改入参。"""
    original = frame.copy()
    VisibilityGuard("2020-01-03").mask(frame)
    pd.testing.assert_frame_equal(frame, original)


def test_mask_empty() -> None:
    """空 DataFrame 安全。"""
    assert VisibilityGuard("2020-01-03").mask(pd.DataFrame()).empty


def test_assert_no_future_passes(frame: pd.DataFrame) -> None:
    """无未来数据时不抛异常。"""
    VisibilityGuard("2020-01-07").assert_no_future(frame)


def test_assert_no_future_raises(frame: pd.DataFrame) -> None:
    """存在未来数据时抛 VisibilityViolation。"""
    with pytest.raises(VisibilityViolation):
        VisibilityGuard("2020-01-03").assert_no_future(frame)


def test_mask_by_dt_column() -> None:
    """分钟线用 dt 列截断。"""
    df = pd.DataFrame({"dt": pd.to_datetime(["2024-01-02 09:35", "2024-01-02 10:00"]), "close": [1.0, 2.0]})
    out = VisibilityGuard("2024-01-02 09:40").mask(df, date_col="dt")
    assert len(out) == 1
