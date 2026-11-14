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


_PKG_DIR = os.path.dirname(os.path.abspath(__file__))


def _site() -> str:
    """返回触发这次系统调用的用户代码位置，形如 ``migrate.py:42``。

    调用链是 被测代码 → _hook → _on → _dispatch → 这里，中间还会夹着
    importlib / contextlib 之类的框架帧。与其数层数，不如一路往上走、
    跳过所有属于把本包的帧 —— 版本升级、加一层包装都不会让这里失准。
    """
    try:
        frame = sys._getframe(1)
    except Exception:
        return ""
    seen = 0
    while frame is not None and seen < 60:
        try:
            name = frame.f_code.co_filename
        except Exception:
            return ""
        if name and name.startswith("<"):
            frame = frame.f_back
            seen += 1
            continue
        try:
            full = os.path.abspath(name)
        except Exception:
            full = name
        if not full.startswith(_PKG_DIR):
            return f"{os.path.basename(full)}:{frame.f_lineno}"
        frame = frame.f_back
        seen += 1
    return ""


def _remember(s, path: str, site: str) -> None:
    if not site:
        return
    bucket = s.sites.get(path)
    if bucket is None:
        bucket = s.sites[path] = Counter()
    bucket[site] += 1


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
    # 写入来自哪一行：{写过的路径: Counter({"script.py:42": 1, ...})}
    # 只对写事件记录 —— 读事件动辄上万次，逐个走栈会把被测脚本拖慢。
    sites: dict = field(default_factory=dict)
    # 读事件的位置：只记每个路径的第一次。读取次数可能是几万次，
    # 每条都走栈会把被测脚本拖慢；而"这行读的"只需知道一次。
    read_sites: dict = field(default_factory=dict)

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

    def site_of(self, path: str) -> str:
        """某个文件是被哪一行写的；多个位置时取最常出现的那个。"""
        bucket = self.sites.get(path)
        if not bucket:
            return ""
        site, n = bucket.most_common(1)[0]
        return site if n == 1 else f"{site}（共 {n} 次）"

    def sites_for(self, paths) -> list[str]:
        """一批路径的写入位置，去重、保持输入顺序。"""
        out, seen = [], set()
        for p in paths:
            t = self.site_of(p)
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return out

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
                _remember(s, key, _site())
            else:
                s.reads[key] += 1
                if key not in s.read_sites:
                    s.read_sites[key] = _site()
            return

        if event == "os.rename":
            src, dst = _norm(args[0]), _norm(args[1])
            s.renames.append((src, dst))
            s.writes[dst] += 1
            _remember(s, dst, _site())
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
