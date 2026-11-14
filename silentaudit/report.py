"""终端输出。没有依赖，颜色可以关掉（``--no-color`` 或者在管道里自动关）。"""
from __future__ import annotations

import os
import sys

_ENABLE = True


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":
        # Windows 10 1511+ 的 conhost 支持 ANSI，但需要显式打开 VT 处理
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                k.SetConsoleMode(h, mode.value | 0x0004)
                return True
            return False
        except Exception:
            return False
    return True


class C:
    on = True
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    GREEN = "\033[92m"
    CYAN = "\033[96m"
    GREY = "\033[90m"

    @classmethod
    def w(cls, text: str, color: str) -> str:
        if not (cls.on and _ENABLE):
            return text
        return f"{color}{text}{cls.RESET}"


def setup(force_off: bool = False) -> None:
    global _ENABLE
    if force_off:
        _ENABLE = False
    else:
        _ENABLE = _supports_color()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


_BAR_HALF = "▌"
_BAR_FULL = "█"


def score_bar(score: float, width: int = 20) -> str:
    filled = int(round(score / 10 * width))
    if score >= 9:
        col = C.GREEN
    elif score >= 6:
        col = C.CYAN
    elif score >= 3:
        col = C.YELLOW
    else:
        col = C.RED
    return C.w(_BAR_FULL * filled, col) + C.w("░" * (width - filled), C.GREY)


_ICON = {"fail": "✗", "warn": "!", "info": "·"}
_COLOR = {"fail": C.RED, "warn": C.YELLOW, "info": C.CYAN}


def _replay_rows(res) -> str:
    """一次运行的摘要，用于两行对照。"""
    v = res.verdict
    claim = "?" if v.claimed_total is None else f"{v.claimed_total:,}"
    return (f"声称 {claim} · 变了 {res.diff.real_change} 个 · "
            f"退出码 {res.exit_code} · {v.score:.1f}/10")


def render_replay(rp, *, verbose: bool = False, base: str | None = None) -> str:
    base = base or os.getcwd()
    score = rp.score
    col = C.GREEN if score >= 9 else (C.CYAN if score >= 6 else
                                      (C.YELLOW if score >= 3 else C.RED))
    if rp.diverged:
        head = "不可复现：同样的起点，两次结果不一样"
    else:
        head = "可复现：同样的起点，两次结果一致"

    lines: list[str] = ["", f"  {C.w(head, col + C.BOLD)}", ""]
    lines.append(f"  可信度  {score_bar(score)}  {C.w(f'{score:.1f}/10', col + C.BOLD)}")
    lines.append(f"  {C.w('两次里更差的一次，再扣掉不一致的分', C.GREY)}")
    lines.append("")
    lines.append(f"  {C.w('副本'.ljust(6), C.GREY)}  {rp.note}（不碰你的原文件）")
    lines.append("")
    lines.append(f"  {C.w('第 1 次', C.GREY)}  {_replay_rows(rp.a)}")
    lines.append(f"  {C.w('第 2 次', C.GREY)}  {_replay_rows(rp.b)}")
    lines.append("")

    for f in rp.findings:
        icon = _ICON.get(f.level, "·")
        c = _COLOR.get(f.level, C.CYAN)
        lines.append(f"  {C.w(icon, c + C.BOLD)} {C.w(f.title, C.BOLD)}")
        for seg in _wrap(f.detail, 78):
            lines.append(f"    {C.w(seg, C.DIM)}")
        for e in f.evidence:
            lines.append(f"      {C.w('·', C.GREY)} {C.w(e, C.GREY)}")
        lines.append("")

    # 每次运行自己的主要问题各列一条，避免把两份完整报告糊在一起
    for tag, res in (("第 1 次", rp.a), ("第 2 次", rp.b)):
        top = [f for f in res.verdict.findings if f.penalty > 0]
        if not top:
            continue
        lines.append(f"  {C.w(tag + '自己的问题', C.YELLOW + C.BOLD)}")
        for f in top[:2]:
            lines.append(f"    {C.w('·', C.GREY)} {f.title}")
        lines.append("")

    if verbose:
        _file_section(lines, rp.b, base)

    lines.append(f"  {C.w(f'副本目录 {rp.directory}', C.GREY)}")
    lines.append("")
    return "\n".join(lines)


def render(res, *, verbose: bool = False, base: str | None = None) -> str:
    base = base or os.getcwd()
    v = res.verdict
    lines: list[str] = []
    head = v.headline
    col = C.GREEN if v.score >= 9 else (C.CYAN if v.score >= 6 else
                                        (C.YELLOW if v.score >= 3 else C.RED))

    lines.append("")
    lines.append(f"  {C.w(head, col + C.BOLD)}")
    lines.append("")
    lines.append(f"  可信度  {score_bar(v.score)}  {C.w(f'{v.score:.1f}/10', col + C.BOLD)}")
    lines.append("")

    # 三栏事实摘要
    if v.claimed_total is None:
        claim = "（没读出数字）"
    else:
        claim = f"处理 {v.claimed_total:,} 项"
    has_snap = bool(res.diff.files_seen)
    if has_snap:
        evidence = (f"{v.written_files} 个文件被写 · "
                    f"其中 {res.diff.real_change} 个真的变了")
    else:
        evidence = f"{v.written_files} 个文件被写（未拍快照，无法判断内容）"
    rows = [
        ("它声称", claim),
        ("系统调用", f"{res.trace.write_count} 次写入 / {res.trace.read_count} 次读取"),
        ("物证", evidence),
        ("退出码", str(v.exit_code)),
    ]
    procs = list(getattr(res, "procs", None) or [])
    if procs:
        n_bad = sum(1 for p in procs if p.code is not None and p.code != 0)
        tail = f"，{n_bad} 次非零" if n_bad else ""
        rows.insert(3, ("子进程", f"{len(procs)} 次{tail}"))
    for k, val in rows:
        lines.append(f"  {C.w(k.ljust(6), C.GREY)}  {val}")
    lines.append("")

    for f in v.findings:
        icon = _ICON.get(f.level, "·")
        c = _COLOR.get(f.level, C.CYAN)
        lines.append(f"  {C.w(icon, c + C.BOLD)} {C.w(f.title, C.BOLD)}")
        for i, seg in enumerate(_wrap(f.detail, 78)):
            lines.append(f"    {C.w(seg, C.DIM)}")
        for e in f.evidence:
            lines.append(f"      {C.w('·', C.GREY)} {C.w(e, C.GREY)}")
        lines.append("")

    if verbose or res.diff.changed or res.diff.created or res.diff.deleted:
        _file_section(lines, res, base)

    if v.hints:
        lines.append(f"  {C.w('最可疑的几行', C.YELLOW + C.BOLD)}")
        for h in v.hints:
            lines.append(f"    {C.w(f'{h.line:>4}', C.GREY)}  {h.code}")
            lines.append(f"          {C.w(h.reason, C.DIM)}")
        lines.append("")

    snap = res.snapshot_note or "未做"
    lines.append(f"  {C.w(f'耗时 {res.duration:.2f}s · 快照 {snap}', C.GREY)}")
    lines.append("")
    return "\n".join(lines)


def _chunks(s: str) -> list[str]:
    """切成不可再分的片段：中日韩字符各自成段，拉丁单词/数字/标点连成一段。

    这样换行时只会落在中文与中文之间、中文与英文之间，不会把
    `check=True` 这种英文单词劈成 `check=T` + `rue`。
    """
    out: list[str] = []
    buf = ""
    for ch in s:
        wide = ord(ch) > 0x2E80
        if wide or ch.isspace():
            if buf:
                out.append(buf)
                buf = ""
            out.append(ch)
        else:
            buf += ch
    if buf:
        out.append(buf)
    return out


def _wrap(s: str, width: int) -> list[str]:
    out: list[str] = []
    for para in s.split("\n"):
        if not para.strip():
            out.append("")
            continue
        cur, cur_w = "", 0
        for piece in _chunks(para):
            w = sum(2 if ord(c) > 0x2E80 else 1 for c in piece)
            if cur_w + w > width and cur:
                out.append(cur.rstrip())
                cur, cur_w = "", 0
                if piece.isspace():
                    continue
            # 单段就超过整行宽（超长路径/URL）时硬切
            if w > width:
                for c in piece:
                    cw = 2 if ord(c) > 0x2E80 else 1
                    if cur_w + cw > width:
                        out.append(cur)
                        cur, cur_w = "", 0
                    cur += c
                    cur_w += cw
                continue
            cur += piece
            cur_w += w
        if cur.strip():
            out.append(cur.rstrip())
    return out or [""]


def _file_section(lines: list[str], res, base: str) -> None:
    def rel(p: str) -> str:
        try:
            r = os.path.relpath(p, base)
            return p if r.startswith("..") else r
        except Exception:
            return p

    d = res.diff
    if d.changed:
        lines.append(f"  {C.w('内容真的变了', C.GREEN + C.BOLD)} {C.w(f'({len(d.changed)})', C.GREY)}")
        for p in d.changed[:12]:
            lines.append(f"    {C.w('+', C.GREEN)} {rel(p)}")
        if len(d.changed) > 12:
            lines.append(f"    {C.w(f'… 还有 {len(d.changed) - 12} 个', C.GREY)}")
        lines.append("")
    if d.created:
        lines.append(f"  {C.w('新建', C.CYAN + C.BOLD)} {C.w(f'({len(d.created)})', C.GREY)}")
        for p in d.created[:12]:
            lines.append(f"    {C.w('+', C.CYAN)} {rel(p)}")
        if len(d.created) > 12:
            lines.append(f"    {C.w(f'… 还有 {len(d.created) - 12} 个', C.GREY)}")
        lines.append("")
    if d.deleted:
        lines.append(f"  {C.w('删除', C.RED + C.BOLD)} {C.w(f'({len(d.deleted)})', C.GREY)}")
        for p in d.deleted[:12]:
            lines.append(f"    {C.w('-', C.RED)} {rel(p)}")
        if len(d.deleted) > 12:
            lines.append(f"    {C.w(f'… 还有 {len(d.deleted) - 12} 个', C.GREY)}")
        lines.append("")
    if d.touched and not d.changed:
        lines.append(f"  {C.w('写过了但没变化', C.YELLOW + C.BOLD)} {C.w(f'({len(d.touched)})', C.GREY)}")
        for p in d.touched[:12]:
            lines.append(f"    {C.w('=', C.YELLOW)} {rel(p)}")
        lines.append("")
