"""系统调用级别的行为记录。

Python 3.8+ 的 ``sys.addaudithook()`` 可以在**不改目标脚本一个字符**的前提下，
拿到 ``open`` / ``os.rename`` / ``os.remove`` / ``subprocess.Popen`` 这些真实事件。

两个诚实的限制，工具里会明确告诉用户：

* 只有**当前进程**可见 —— 子进程里做的事它看不见；
* 只有 **Python** 能拿到这一层证据 —— 别的语言只能退回到"文件变了没有"。
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from dataclasses import dataclass, field

# 只关心这几类事件，别的（compile/exec/import…）一律不记，避免拖慢被测脚本
_FILE_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_MAX_EVENTS = 50_000


def _norm(path) -> str:
    if isinstance(path, (bytes, bytearray)):
        try:
            path = os.fsdecode(path)
        except Exception:
            return ""
    if not isinstance(path, str):
        return ""
    try:
        return os.path.normcase(os.path.abspath(path))
    except Exception:
        return ""


@dataclass
class TraceSummary:
    available: bool = True
    reads: Counter = field(default_factory=Counter)
    writes: Counter = field(default_factory=Counter)
    renames: list = field(default_factory=list)
    removes: list = field(default_factory=list)
    mkdirs: list = field(default_factory=list)
    subprocs: list = field(default_factory=list)
    sockets: int = 0
    outside: list = field(default_factory=list)
    overflow: bool = False
    events: int = 0

    # ---- 聚合视图，给 judge 用 ----
    @property
    def write_paths(self) -> set[str]:
        return set(self.writes)

    @property
    def read_count(self) -> int:
        return sum(self.reads.values())

    @property
    def write_count(self) -> int:
        return sum(self.writes.values())

    def inside(self, roots: list[str]) -> None:
        """把不在 roots 里的路径单独拎出来提醒（很常见的 bug：写到了别处）。"""
        bad = set()
        for p in list(self.writes) + [d for _, d in self.renames] + list(self.removes):
            if not any(p == r or p.startswith(r + os.sep) for r in roots):
                bad.add(p)
        self.outside = sorted(bad)


class Trace:
    """一次运行的记录器。用 ``with Trace(roots) as t:`` 包住被测代码。"""

    def __init__(self, roots: list[str], ignore: set[str] | None = None):
        self.roots = [os.path.normcase(os.path.abspath(r)) for r in roots]
        self.ignore = ignore or set()
        self.summary = TraceSummary()
        self._busy = False

    # ---------- 审计回调 ----------
    def _on(self, event: str, args) -> None:
        if self._busy:
            return
        s = self.summary
        if s.events >= _MAX_EVENTS:
            s.overflow = True
            return
        self._busy = True
        try:
            self._dispatch(event, args)
        except Exception:
            pass
        finally:
            self._busy = False

    def _dispatch(self, event: str, args) -> None:
        s = self.summary
        s.events += 1

        if event == "open":
            path, mode = args[0], args[1]
            flags = args[2] if len(args) > 2 else 0
            key = _norm(path)
            if not key:
                return
            writing = False
            if isinstance(mode, str):
                writing = any(c in mode for c in "wax+")
            elif isinstance(flags, int):
                writing = bool(flags & _FILE_WRITE_FLAGS)
            if writing:
                s.writes[key] += 1
            else:
                s.reads[key] += 1
            return

        if event == "os.rename":
            src, dst = _norm(args[0]), _norm(args[1])
            s.renames.append((src, dst))
            s.writes[dst] += 1
            return

        if event in ("os.remove", "os.unlink"):
            s.removes.append(_norm(args[0]))
            return

        if event == "os.rmdir":
            s.removes.append(_norm(args[0]))
            return

        if event in ("os.mkdir", "os.makedirs"):
            s.mkdirs.append(_norm(args[0]))
            return

        if event == "subprocess.Popen":
            try:
                cmd = args[1] if len(args) > 1 else args[0]
                if isinstance(cmd, (list, tuple)):
                    cmd = " ".join(str(c) for c in cmd)
                s.subprocs.append(str(cmd)[:200])
            except Exception:
                s.subprocs.append("?")
            return

        if event in ("socket.connect", "socket.getaddrinfo"):
            s.sockets += 1
            return


# --------------------------------------------------------------------------
# 全局单例：addaudithook 无法卸载，所以只装一次，用 _ACTIVE 开关控制。
# --------------------------------------------------------------------------
_ACTIVE: Trace | None = None
_INSTALLED = False
_SKIP = {"open", "os.rename", "os.remove", "os.unlink", "os.rmdir", "os.mkdir",
         "os.makedirs", "subprocess.Popen", "socket.connect", "socket.getaddrinfo"}


def _hook(event: str, args) -> None:
    t = _ACTIVE
    if t is None or event not in _SKIP:
        return
    t._on(event, args)


def install() -> None:
    global _INSTALLED
    if not _INSTALLED:
        sys.addaudithook(_hook)
        _INSTALLED = True


class capture:
    """``with capture():`` —— 期间的系统调用都会记进 trace。"""

    def __init__(self, trace: Trace):
        self.trace = trace

    def __enter__(self) -> Trace:
        global _ACTIVE
        install()
        _ACTIVE = self.trace
        return self.trace

    def __exit__(self, *exc) -> None:
        global _ACTIVE
        _ACTIVE = None
        return None
