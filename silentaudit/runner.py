"""把被测脚本跑起来，同时收集三路证据。

在**当前进程内**执行（``exec``），而不是起子进程 —— 这是审计钩子能生效的前提。
代价是被测脚本和本工具共享一个解释器：它如果调用 ``sys.exit()`` 会抛
``SystemExit``，我们捕获即可；但如果它真的把解释器搞崩了（段错误、``os._exit``），
本工具也拿不到结果。README 里明说了这一点。
"""
from __future__ import annotations

import io
import os
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from . import judge as judge_mod
from . import snapshot
from .trace import Trace, capture


@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    duration: float
    trace: object
    diff: object
    verdict: object
    snapshot_note: str


def _user_roots(script: Path, watch: list[str] | None) -> list[str]:
    """默认盯住脚本所在目录；显式 ``--watch`` 可以加，也可以用它替代。"""
    if watch:
        return [os.path.abspath(w) for w in watch]
    return [str(script.resolve().parent)]


def run(script: Path, args: list[str] | None = None,
        watch: list[str] | None = None, cwd: str | None = None,
        take_snapshot: bool = True, quiet: bool = False,
        allow_new: list[str] | None = None) -> RunResult:
    script = Path(script).resolve()
    if not script.is_file():
        raise SystemExit(f"找不到脚本：{script}")

    roots = _user_roots(script, watch)
    work_dir = os.path.abspath(cwd) if cwd else str(script.parent)

    note = ""
    before = snapshot.Snapshot({})
    if take_snapshot:
        if not quiet:
            sys.stderr.write("· 拍快照 ...")
            sys.stderr.flush()
        t0 = time.perf_counter()
        before = snapshot.take(roots)
        note = (f"{len(before)} 个文件 / {time.perf_counter() - t0:.2f}s")
        if before.truncated:
            note += " / 已达上限，超出部分未纳入比对"
        if before.skipped_big:
            note += f" / {before.skipped_big} 个 >64MB 的文件只比 mtime"
        if not quiet:
            sys.stderr.write(f"\r· 快照 {note}\n")
    else:
        if not quiet:
            sys.stderr.write("· 跳过快照（只记录系统调用）\n")

    out_buf, err_buf = io.StringIO(), io.StringIO()
    real_out, real_err = sys.stdout, sys.stderr
    real_argv = sys.argv[:]
    old_cwd = os.getcwd()

    trace = Trace(roots)
    exit_code = 0
    t_start = time.perf_counter()

    code = compile(script.read_text(encoding="utf-8", errors="replace"),
                   str(script), "exec")
    glb = {"__name__": "__main__", "__file__": str(script),
           "__builtins__": __builtins__, "__doc__": None}

    try:
        os.chdir(work_dir)
        sys.argv = [str(script)] + list(args or [])
        sys.stdout, sys.stderr = out_buf, err_buf
        with capture(trace):
            exec(code, glb)
    except SystemExit as e:
        exit_code = 0 if e.code is None else (e.code if isinstance(e.code, int) else 1)
    except BaseException:
        exit_code = 1
        err_buf.write(traceback.format_exc())
    finally:
        duration = time.perf_counter() - t_start
        sys.stdout, sys.stderr = real_out, real_err
        sys.argv = real_argv
        try:
            os.chdir(old_cwd)
        except OSError:
            pass

    trace.summary.inside(trace.roots)

    after = snapshot.Snapshot({})
    if take_snapshot:
        after = snapshot.take(roots)
    diff = snapshot.compare(before, after)

    verdict = judge_mod.judge(exit_code, out_buf.getvalue(), err_buf.getvalue(),
                              trace.summary, diff, roots, work_dir,
                              snapshot_taken=take_snapshot,
                              allow_new=allow_new)

    # 只有在"报告不可信"时才去源码里找线索，避免满屏噪音
    try:
        from .hints import collect_keys

        src = script.read_text(encoding="utf-8", errors="replace")
        keys: list[str] = []
        for p in list(trace.summary.reads)[:40]:
            if not p.lower().endswith((".csv", ".tsv")):
                continue
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    head = fh.readline().strip()
            except OSError:
                continue
            keys.extend(collect_keys(head))
        judge_mod.add_source_hints(verdict, src, keys=keys)
    except Exception:
        pass

    return RunResult(exit_code, out_buf.getvalue(), err_buf.getvalue(), duration,
                     trace.summary, diff, verdict, note)
