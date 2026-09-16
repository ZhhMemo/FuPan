"""数据访问层包（访问 DuckDB 的唯一入口）。

红线落点：
- ① 前视偏差 → ``visibility.py``
- ② 复权三口径 → ``adjuster.py``
- ⑥ 幸存者偏差 → ``universe.py``
"""

from app.data.models import AdjustMode

__all__ = ["AdjustMode"]
