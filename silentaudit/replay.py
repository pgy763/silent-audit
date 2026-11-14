"""在副本里跑两遍，比较两次的结果。

为什么必须用副本：如果直接在原目录跑两遍，第二次的输入已经被第一次改过了，
"两次结果不同"是必然的，什么也证明不了。所以先把盯住的目录镜像成 pristine，
每次运行前从 pristine 重置 work —— 两次的起点严格相同。

副作用是整个过程中**不碰用户的原文件**：脚本写坏了最多写坏副本。
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import runner as runner_mod
from .snapshot import _SKIP_DIRS

REPLAY_DIR = ".silent_audit_replay"

_MAX_FILES = 20_000
_MAX_TOTAL = 512 * 1024 * 1024
_MAX_FILE = 64 * 1024 * 1024


@dataclass
class Replay:
    a: object                      # 第一次的 RunResult
    b: object                      # 第二次的 RunResult
    findings: list = field(default_factory=list)
    penalty: float = 0.0
    note: str = ""
    directory: str = ""
    script_in_copy: str = ""

    @property
    def score(self) -> float:
        """取两次里更差的，再扣掉"两次不一致"的分。"""
        base = min(self.a.verdict.score, self.b.verdict.score)
        return max(0.0, min(10.0, base - self.penalty))

    @property
    def diverged(self) -> bool:
        return any(f.level in ("fail", "warn") for f in self.findings)


def _under(path: Path, root: str) -> bool:
    p = os.path.normcase(str(path))
    r = os.path.normcase(os.path.abspath(root))
    return p == r or p.startswith(r + os.sep)


def _mirror(src: str, dst: Path) -> tuple[int, bool]:
    """把 src 复制进 dst。返回 (文件数, 是否触顶)。"""
    count = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(src):
        # 别把副本目录自己复制进自己（--cwd 落在被盯目录里时会发生）
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in _SKIP_DIRS
                       and not d.startswith(".frames")
                       and d != REPLAY_DIR]
        rel = os.path.relpath(dirpath, src)
        target = dst if rel == "." else dst / rel
        target.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            if count >= _MAX_FILES or total >= _MAX_TOTAL:
                return count, True
            s = os.path.join(dirpath, name)
            try:
                st = os.stat(s)
            except OSError:
                continue
            if st.st_size > _MAX_FILE:
                continue                      # 超大文件不进副本，会说明
            try:
                shutil.copy2(s, target / name)
            except OSError:
                continue
            count += 1
            total += st.st_size
    return count, False


def _reset(pristine: Path, work: Path) -> None:
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(pristine, work)


def _norm_out(text: str, work: str) -> str:
    """把输出里跟具体路径/耗时有关的部分抹掉，剩下的才可比。"""
    s = text or ""
    for needle in (work, os.path.normcase(work)):
        s = s.replace(needle, "<dir>")
    s = re.sub(r"\d+\.\d+\s*s\b", "<t>", s)
    return "\n".join(ln.rstrip() for ln in s.splitlines()).strip()


def _describe(res) -> str:
    d = res.diff
    return (f"声称 {res.verdict.claimed_total if res.verdict.claimed_total is not None else '?'}"
            f" · 变了 {d.real_change} 个 · 退出码 {res.exit_code}")


def _first_diff(x: str, y: str) -> tuple[int, str, str]:
    """返回第一处不同的行号（从 1 起）和两边的内容。"""
    la, lb = x.splitlines(), y.splitlines()
    for i in range(max(len(la), len(lb))):
        a = la[i] if i < len(la) else "<没有这一行>"
        b = lb[i] if i < len(lb) else "<没有这一行>"
        if a != b:
            return i + 1, a, b
    return 0, "", ""


def compare(a, b, work: str = "") -> tuple[list, float]:
    """比较两次运行的判决。只报告**有把握**的差异，宁可漏说不瞎说。"""
    from .judge import Finding

    out: list = []
    penalty = 0.0

    if a.exit_code == 0 and b.exit_code != 0:
        out.append(Finding(
            "fail",
            "同一个起点，第一次成功，第二次失败",
            "两次运行的输入完全一样（副本每次重置），差别只能来自脚本以外的"
            "东西：时间、网络、随机数，或者它依赖了某次运行留下的痕迹。",
            [f"第 1 次：退出码 {a.exit_code}", f"第 2 次：退出码 {b.exit_code}"],
            penalty=5.0,
        ))
        penalty += 5.0
    elif a.exit_code != b.exit_code:
        out.append(Finding(
            "warn",
            f"两次运行的退出码不一样（{a.exit_code} → {b.exit_code}）",
            "输入相同而退出码不同，说明结果不受输入控制。",
            [], penalty=2.5,
        ))
        penalty += 2.5

    ca, cb = a.verdict.claimed_total, b.verdict.claimed_total
    if ca is not None and cb is not None and ca != cb:
        out.append(Finding(
            "fail",
            f"同样的输入，它两次报的数字不一样（{ca:,} → {cb:,}）",
            "这是最直接的证据：这个数字不是从数据里算出来的。可能来自随机数、"
            "时间窗口、上一次运行的残留，或者是一段没有真实计数的固定文案。",
            [f"第 1 次：{_describe(a)}", f"第 2 次：{_describe(b)}"],
            penalty=4.0,
        ))
        penalty += 4.0
    elif (ca is not None and ca == cb
          and a.verdict.exit_code == 0 and b.verdict.exit_code == 0):
        ra, rb = a.diff.real_change, b.diff.real_change
        if ra != rb:
            if rb == 0 < ra:
                out.append(Finding(
                    "warn",
                    f"两次都说处理了 {ca:,} 项，第一次改了 {ra} 个文件，第二次一个都没改",
                    "要么这个脚本是幂等的（第二次确实无事可做，那这是对的），"
                    "要么第二次的处理被整体跳过了。分辨方法：看第二次的输出里"
                    "有没有失败/跳过字样；没有的话更可能是被跳过了。",
                    [f"第 1 次：{_describe(a)}", f"第 2 次：{_describe(b)}"],
                    penalty=3.0,
                ))
                penalty += 3.0
            else:
                out.append(Finding(
                    "warn",
                    f"两次运行声称一样，落盘结果不一样（{ra} → {rb} 个文件）",
                    "同样的起点得到不同的改动量，说明有非确定性在里面。",
                    [f"第 1 次：{_describe(a)}", f"第 2 次：{_describe(b)}"],
                    penalty=3.5,
                ))
                penalty += 3.5

    na = sum(1 for p in (a.procs or []) if p.code is not None and p.code != 0)
    nb = sum(1 for p in (b.procs or []) if p.code is not None and p.code != 0)
    if na != nb:
        out.append(Finding(
            "warn",
            f"两次运行的子进程失败次数不一样（{na} 次 → {nb} 次）",
            "同样的输入，子进程一次成功一次失败 —— 通常是网络、临时文件或"
            "并发冲突，而不是脚本逻辑。",
            [], penalty=2.0,
        ))
        penalty += 2.0

    # 数字和落盘结果都一样，但输出文字不一样。排除掉路径和耗时之后还能差，
    # 说明结果里混进了时间戳、随机后缀、内存地址这类东西。
    # 这条只警告不重罚：正常的脚本也会打印当前时间。
    if not out:
        ta, tb = _norm_out(a.stdout, work), _norm_out(b.stdout, work)
        if ta != tb:
            ln, la, lb = _first_diff(ta, tb)
            out.append(Finding(
                "warn",
                f"两次的输出文字不一样（第 {ln} 行）",
                "数字和落盘结果都一致，所以这次差异多半是时间戳、随机后缀或者"
                "内存地址。它不影响结论，但如果你要拿这份输出做比对或者写进报告，"
                "它会让每次的 diff 都是脏的。",
                [f"第 1 次：{la[:100]}", f"第 2 次：{lb[:100]}"],
                penalty=1.5,
            ))
            penalty += 1.5

    if not out:
        out.append(Finding(
            "info",
            "两次运行从同一个起点出发，结果一致",
            "这是能拿到的最强证据之一：同样的输入，同样的输出，同样的落盘结果。"
            "它不能证明业务逻辑对，但能证明结论是稳定的、可复现的。",
            [f"第 1 次：{_describe(a)}", f"第 2 次：{_describe(b)}"],
            penalty=0.0,
        ))
    return out, penalty


def run_replay(script, args=None, watch=None, cwd=None,
               allow_new=None, quiet=True, keep=False) -> Replay:
    """把脚本连跑两遍，中间重置副本，然后比较。"""
    script = Path(script).resolve()
    if not script.is_file():
        raise SystemExit(f"找不到脚本：{script}")

    roots = [os.path.abspath(w) for w in (watch or [])] or [str(script.parent)]
    if not any(_under(script, r) for r in roots):
        # 脚本不在盯住的目录里：把它自己那层也加进来，否则副本里没有它，
        # __file__ 会指回真实路径，读到的就不是副本了。
        roots.append(str(script.parent))

    base = Path(cwd or os.getcwd()).resolve() / REPLAY_DIR
    pristine, work = base / "pristine", base / "work"
    shutil.rmtree(base, ignore_errors=True)
    pristine.mkdir(parents=True, exist_ok=True)

    files, truncated = 0, False
    for r in roots:
        n, t = _mirror(r, pristine) if os.path.isdir(r) else (0, False)
        files += n
        truncated = truncated or t
    note = f"{files} 个文件"
    if truncated:
        note += "（已达上限，超出部分未进副本）"

    holder = next((r for r in roots if _under(script, r)), str(script.parent))
    rel = os.path.relpath(str(script), holder)
    script_in_copy = work / rel

    results = []
    for _ in range(2):
        _reset(pristine, work)
        results.append(runner_mod.run(
            script_in_copy, args=args, watch=[str(work)], cwd=str(work),
            take_snapshot=True, quiet=quiet, allow_new=allow_new))

    findings, penalty = compare(results[0], results[1], str(work))
    rp = Replay(results[0], results[1], findings, penalty, note,
                str(base), str(script_in_copy))
    if not keep:
        shutil.rmtree(base, ignore_errors=True)
    return rp
