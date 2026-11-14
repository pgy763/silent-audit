"""子进程观测：谁被启动了、跑了多久、退出码是多少。

审计钩子只能告诉我们"调了一次 ``subprocess.Popen``"，看不到**结果**。
但静默失败最爱藏的地方恰恰是这里::

    subprocess.run(["ffmpeg", "-i", "a.mp4", "out.mp3"])   # 忘了 check=True
    print("✓ 转码完成")

ffmpeg 返回 1、什么都没产出，脚本却报告成功。所以这里换一条路：在运行期间
**临时包一层** ``subprocess.Popen``，记下命令行、起止时间和退出码。

三个诚实的限制：

* 只看得到"启动了"和"退出码"，子进程**内部**的读写仍然不可见
  （除非它也是 Python 并且开了 ``--trace-children``）；
* ``os.system`` / ``os.spawn*`` 走的是另外的路径，这里抓不到，只能靠审计钩子
  那条"调用了子进程"的提示兜底；
* 退出码非零不等于失败 —— ``git diff --quiet`` 用 1 表示"有差异"，
  ``find`` 用 1 表示"没找到"。判据里对此做了区分，也会在输出里提醒。
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field

# 这些名字的进程按 Python 解释器对待（可选地再往下追一层）
_PY_NAMES = {"python", "python3", "pythonw", "py", "py.exe",
             "python.exe", "python3.exe", "pythonw.exe"}


def _basename(argv: list[str]) -> str:
    if not argv:
        return ""
    head = os.path.basename(str(argv[0]).replace("\\", "/")).lower()
    return head


@dataclass
class ProcRec:
    """一次子进程调用的观测结果。"""

    cmd: str
    argv: list[str] = field(default_factory=list)
    start: float = 0.0
    end: float = 0.0
    code: int | None = None
    pid: int = 0
    shell: bool = False
    is_python: bool = False
    error: str = ""
    t0: float = 0.0
    _proc: object = field(default=None, repr=False, compare=False)

    @property
    def duration(self) -> float:
        if not self.end or not self.start:
            return 0.0
        return max(0.0, self.end - self.start)

    @property
    def failed(self) -> bool:
        return self.code is not None and self.code != 0

    @property
    def finished(self) -> bool:
        return self.code is not None

    @property
    def short(self) -> str:
        s = self.cmd
        return s if len(s) <= 96 else s[:93] + "..."

    def stamp(self) -> str:
        """给时间轴用的 `+1.24s` 相对时刻（相对整次运行开始）。"""
        if not self.start:
            return "?"
        return f"+{max(0.0, self.start - self.t0):.2f}s"


def _join(argv) -> tuple[list[str], str]:
    if isinstance(argv, (bytes, bytearray)):
        argv = os.fsdecode(argv)
    if isinstance(argv, str):
        return [argv], argv
    try:
        items = [str(a) for a in argv]
    except TypeError:
        return [], str(argv)
    return items, " ".join(items)


class ProcWatch:
    """``with ProcWatch() as w: ...`` —— 退出时恢复被替换的方法。

    只替换四个方法，不换类：``isinstance(x, subprocess.Popen)`` 依然成立，
    被测脚本不会因为我们的观测而走岔路。
    """

    def __init__(self) -> None:
        self.recs: list[ProcRec] = []
        self._saved: list = []
        self.t0 = 0.0

    # ---------- 安装 / 卸载 ----------
    def __enter__(self) -> "ProcWatch":
        P = subprocess.Popen
        self._saved = [P.__init__, P.wait, P.communicate, P.poll]
        self.t0 = time.perf_counter()
        watch = self

        def __init__(proc, args, *a, **kw):  # noqa: N807
            argv, joined = _join(args)
            rec = ProcRec(
                cmd=joined,
                argv=argv,
                start=time.perf_counter(),
                shell=bool(kw.get("shell")),
                is_python=_basename(argv) in _PY_NAMES,
                t0=watch.t0,
            )
            watch.recs.append(rec)
            try:
                self._saved[0](proc, args, *a, **kw)
            except BaseException as e:
                # 起不来（命令不存在、权限不足）本身就是很有价值的证据
                rec.code = -1
                rec.end = time.perf_counter()
                rec.error = f"{type(e).__name__}: {e}"
                raise
            rec.pid = getattr(proc, "pid", 0) or 0
            rec._proc = proc
            return None

        def wait(proc, *a, **kw):
            try:
                return self._saved[1](proc, *a, **kw)
            finally:
                watch._settle(proc)

        def communicate(proc, *a, **kw):
            try:
                return self._saved[2](proc, *a, **kw)
            finally:
                watch._settle(proc)

        def poll(proc):
            code = self._saved[3](proc)
            if code is not None:
                watch._settle(proc)
            return code

        P.__init__, P.wait, P.communicate, P.poll = __init__, wait, communicate, poll
        return self

    def __exit__(self, *exc) -> None:
        P = subprocess.Popen
        try:
            P.__init__, P.wait, P.communicate, P.poll = self._saved
        except Exception:
            pass
        # 收尾：脚本可能没 wait 就退出了，但对象上其实已经有 returncode
        for rec in self.recs:
            if rec.code is None:
                self._settle(rec._proc)
        return None

    # ---------- 内部 ----------
    def _settle(self, proc) -> None:
        code = getattr(proc, "returncode", None)
        for rec in self.recs:
            if rec._proc is proc:
                if rec.code is None and code is not None:
                    rec.code = code
                    rec.end = time.perf_counter()
                return

    # ---------- 查询 ----------
    def failed(self) -> list[ProcRec]:
        return [r for r in self.recs if r.failed]

    def unfinished(self) -> list[ProcRec]:
        return [r for r in self.recs if not r.finished]


def summarize(recs: list[ProcRec]) -> str:
    if not recs:
        return "无"
    bad = sum(1 for r in recs if r.failed)
    if bad:
        return f"{len(recs)} 次，其中 {bad} 次非零退出"
    return f"{len(recs)} 次，全部正常退出"
