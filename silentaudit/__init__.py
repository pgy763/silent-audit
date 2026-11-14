"""silent-audit —— 判断脚本的「我干完了」可不可信。"""
__version__ = "0.4.0"
__all__ = ["run", "runner", "judge", "trace", "snapshot", "expect", "procwatch",
           "replay"]

from .runner import run  # noqa: E402,F401
