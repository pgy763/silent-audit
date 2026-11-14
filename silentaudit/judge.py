"""把三路证据拼成一份判决。

三路证据：

1. **声称** —— 脚本打印了什么（``print`` / logging / 进度条）；
2. **调用** —— 审计钩子记下的真实 ``open(...,"w")`` / ``rename`` / ``remove``；
3. **物证** —— 运行前后的文件 md5。

判决的核心不是"你有没有 bug"，而是"**这个脚本的自我报告可不可信**"。
所以输出里最重要的那个数字叫「可信度」，不叫「通过/失败」。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Iterable

from .snapshot import Diff
from .trace import TraceSummary

# ---- 从 stdout 里读出"它声称干了什么" -------------------------------------

_COUNT_PATTERNS = (
    re.compile(r"(?:处理|修改|更新|迁移|转换|替换|删除|清理|复制|移动|写入|生成|"
               r"processed|updated|migrated|converted|replaced|removed|cleaned|"
               r"copied|moved|wrote|generated|changed|fixed)"
               r"[^\d\n]{0,12}?(\d[\d,]*)\s*(?:个|行|条|处|项|files?|rows?|lines?|items?)?",
               re.I),
    re.compile(r"(\d[\d,]*)\s*(?:个|行|条|处|项)\s*"
               r"(?:文件|行|记录|条目|项)?[^\n]{0,8}"
               r"(?:已|被)?(?:处理|修改|更新|迁移|转换|替换|删除|清理|复制|移动|写入|生成)",
               re.I),
)

_SUCCESS_WORDS = (
    "完成", "成功", "已修复", "已更新", "已修改", "已清理", "已迁移", "已生成",
    "done", "success", "succeeded", "complete", "completed", "fixed", "updated",
    "finished", "ok",
)
_FAILURE_WORDS = (
    "失败", "错误", "无法", "跳过", "未找到", "找不到", "没有匹配", "0 处",
    "failed", "error", "traceback", "not found", "no match", "skipped", "cannot",
)

_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _num(s: str) -> int:
    return int(s.replace(",", "").split(".")[0])


@dataclass
class Claim:
    line: str
    kind: str            # "count" | "success" | "failure"
    value: int | None = None


def _has_words(text: str, words: tuple[str, ...]) -> bool:
    low = (text or "").lower()
    return any(w in low for w in words)


def read_claims(stdout: str, stderr: str) -> list[Claim]:
    claims: list[Claim] = []
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line or len(line) > 400:
            continue
        low = line.lower()
        hit = None
        for pat in _COUNT_PATTERNS:
            m = pat.search(line)
            if m:
                hit = _num(m.group(1))
                break
        if hit is not None:
            claims.append(Claim(line, "count", hit))
            # 同一行里也可能带着"完成"——不能因为先匹配了数字就漏掉
            if any(w in low for w in _SUCCESS_WORDS):
                claims.append(Claim(line, "success"))
            continue
        if any(w in low for w in _SUCCESS_WORDS):
            claims.append(Claim(line, "success"))
            continue
        if any(w in low for w in _FAILURE_WORDS):
            claims.append(Claim(line, "failure"))
    for raw in (stderr or "").splitlines():
        low = raw.strip().lower()
        if any(w in low for w in _FAILURE_WORDS):
            claims.append(Claim(raw.strip(), "failure"))
    return claims[:80]


# ---- 判决 -----------------------------------------------------------------

@dataclass
class Finding:
    level: str          # "fail" | "warn" | "info"
    title: str
    detail: str
    evidence: list[str] = field(default_factory=list)
    penalty: float = 0.0


@dataclass
class Verdict:
    score: float
    findings: list[Finding]
    claimed_total: int | None
    written_files: int
    real_change: int
    exit_code: int
    hints: list = field(default_factory=list)

    @property
    def headline(self) -> str:
        return {
            (0, 3): "不可信：它说的话和事实对不上",
            (3, 6): "可疑：有实打实的写入，但报告偏乐观",
            (6, 9): "大致可信：改动是真的，细节有偏差",
            (9, 11): "可信：声称的和证据一致",
        }[next((k for k in ((0, 3), (3, 6), (6, 9), (9, 11))
                if k[0] <= self.score < k[1]), (9, 11))]


def _short(p: str, base: str) -> str:
    try:
        r = os.path.relpath(p, base)
        return p if r.startswith("..") else r
    except Exception:
        return p


def _real_failures(procs: list) -> list:
    """从子进程记录里挑出"真的失败了"的，排除退出码 1 有特殊含义的命令。

    ``git diff --quiet`` / ``grep`` / ``find`` / ``diff`` / ``cmp`` 用 1 表示
    "有差异 / 没匹配"，这不算失败。判错的后果是冤枉一个正常脚本，
    比漏判更伤 —— 用户一旦见过一次误报，就会关掉整个工具。
    """
    tolerated = ("git", "grep", "egrep", "fgrep", "find", "findstr", "diff",
                 "cmp", "fc", "where", "which", "test", "[")
    out = []
    for p in procs:
        if not p.failed:
            continue
        head = ""
        if p.argv:
            head = os.path.basename(str(p.argv[0]).replace("\\", "/")).lower()
            head = head[:-4] if head.endswith(".exe") else head
        # git 的子命令要看第二个词：git diff --quiet 用 1 表示"有差异"，
        # 而 git push 用 1 表示"真的失败了"
        if head in tolerated:
            sub = ""
            if head == "git" and len(p.argv) > 1:
                sub = str(p.argv[1]).lower()
            if head == "git":
                if sub in ("diff", "grep", "status", "rev-parse", "merge-base",
                           "diff-index", "diff-tree", "ls-files", "show-ref"):
                    continue
            elif p.code == 1:
                continue
        out.append(p)
    return out


def judge(exit_code: int, stdout: str, stderr: str, trace: TraceSummary,
          diff: Diff, roots: list[str], base: str,
          snapshot_taken: bool = True,
          allow_new: list[str] | None = None,
          procs: list | None = None) -> Verdict:
    findings: list[Finding] = []
    score = 10.0

    claims = read_claims(stdout, stderr)
    counted = [c for c in claims if c.kind == "count"]
    claimed_total = max((c.value for c in counted), default=None)

    written = len(trace.write_paths) or len(set(diff.changed) | set(diff.created))
    real = diff.real_change
    says_done = any(c.kind == "success" for c in claims)
    says_bad = any(c.kind == "failure" for c in claims)

    # 1) 核心判据：声称成功，但世界没变
    #    没拍快照时不能说"磁盘没变"—— 那只是没看，不是没变。
    if exit_code == 0 and says_done and real == 0 and snapshot_taken:
        findings.append(Finding(
            "fail",
            "退出码 0，报告成功，但磁盘上没有任何文件发生变化",
            "它跑完了、没报错、打印了成功信息，但所有文件的 md5 与运行前一致。"
            "这是最典型的静默失败：逻辑根本没进循环，或者写回的内容和原来一样。",
            [f"运行期间 open(...,'w') 次数：{trace.write_count}",
             f"内容真正变化的文件：0",
             f"读取的文件：{len(trace.reads)}"],
            penalty=7.0,
        ))

    # 1b) 它自己就说了"处理 0 项"，却还是当成成功收尾
    if claimed_total == 0 and says_done and exit_code == 0:
        findings.append(Finding(
            "fail",
            "它自己说处理了 0 项，却把它当成成功来报告",
            "这是最容易被忽略的一种：数字就在屏幕上，只是没人看。"
            "实际处理数不为 0 时才该打印\"完成\"，否则应该显式失败或至少警告。",
            [f"声称：{counted[0].line[:120]}"],
            penalty=6.0,
        ))

    # 2) 说了数量，但实际处理数对不上
    if claimed_total and claimed_total > 0:
        produced = real if real else written
        if produced == 0:
            findings.append(Finding(
                "fail",
                f"它说处理了 {claimed_total:,} 项，实际写入 0 个文件",
                "数字是从输出里读出来的。既然一项都没落盘，这个数字只能来自"
                "计数器被错误初始化，或者循环体压根没执行。",
                [f"声称：{counted[0].line[:120]}"],
                penalty=5.0,
            ))
        elif produced * 20 < claimed_total:
            findings.append(Finding(
                "warn",
                f"它说处理了 {claimed_total:,} 项，实际只动了 {produced:,} 个文件",
                "一个文件里可能装多行，所以这不一定错；但如果是一行一条记录的场景，"
                "差这么远通常意味着大部分记录被静默跳过了。",
                [f"声称：{counted[0].line[:120]}"],
                penalty=2.5,
            ))

    # 3) 写过但内容一模一样
    if diff.touched:
        findings.append(Finding(
            "warn",
            f"{len(diff.touched)} 个文件被写过，但内容与运行前逐字节相同",
            "脚本确实执行了写入，只是写回去的东西和原来一样。"
            "常见原因：要设的值本来就是当前值；替换词根本没出现在文本里；"
            "或者格式化输出恰好还原了原始排版。",
            [_short(p, base) for p in diff.touched[:8]],
            penalty=2.0,
        ))

    # 3b) 写出来了，但写成了另一个文件 —— "本该覆盖、结果另存"
    if diff.created:
        from .expect import classify

        made = classify(diff.created, stdout + "\n" + stderr, allow=allow_new)
        dups = [c for c in made if c.kind == "dup"]
        if dups:
            findings.append(Finding(
                "fail",
                f"{len(dups)} 个新建文件看起来是某个已有文件的副本，而不是覆盖它",
                "文件名带 (1) / copy / -1 这类后缀，同目录下还留着原名那份。"
                "脚本会报告\"已保存\"，你的下游脚本却还在读那个没被更新的旧文件。",
                [f"{_short(c.path, base)}  ← 本应写入 "
                 f"{_short(c.shadow, base)}" for c in dups[:6]],
                penalty=6.5,
            ))
        stray = [c for c in made if c.kind == "stray"]
        if stray and not dups:
            findings.append(Finding(
                "info",
                f"新建了 {len(stray)} 个文件，输出里一次都没提到",
                "生成报告的脚本天生会新建文件，所以这里不扣分。但如果这几个不是"
                "预期产物，说明路径拼错了 —— 脚本照样会成功退出。",
                [_short(c.path, base) for c in stray[:6]],
                penalty=0.0,
            ))

    # 4) 读了却一行没写
    if (exit_code == 0 and snapshot_taken and trace.read_count >= 50
            and written == 0 and not diff.touched):
        findings.append(Finding(
            "fail",
            f"读了 {trace.read_count:,} 次文件，一次都没写",
            "典型的\"读进去了、什么都没做\"。检查过滤条件是不是恒为 False，"
            "或者字段名拼错了（大小写、下划线）。".replace('"', '"'),
            [_short(p, base) for p in list(trace.reads)[:6]],
            penalty=4.5,
        ))

    # 5) 写到了预期之外的地方
    #   有快照时以"真实变化的文件"为准 —— trace 记录的是 open(...,"w") 的尝试，
    #   失败的 open（路径不存在、权限不足）也会被记下来，只看 trace 会误报。
    if diff.files_seen:
        # 快照里的路径统一 normcase 过，roots 也必须同样处理，否则 Windows 上
        # 大小写不一致会把"就在目录里"误判成"跑到外面去了"。
        nr = [os.path.normcase(r) for r in roots]
        outside = [p for p in (diff.changed + diff.created)
                   if not any(p == r or p.startswith(r + os.sep) for r in nr)]
        basis = "确认写入"
    else:
        outside = list(trace.outside)
        basis = "尝试写入"
    if outside:
        findings.append(Finding(
            "warn",
            f"{len(outside)} 个文件的{basis}发生在你指定的目录之外",
            "可能是正常的（写缓存、日志），也可能是路径拼错了。"
            "如果脚本号称\"就地修改\"，这些就是可疑的。",
            [_short(p, base) for p in outside[:8]],
            penalty=1.5,
        ))

    # 6) 自称失败但退出码 0
    if says_bad and exit_code == 0 and real > 0:
        findings.append(Finding(
            "info",
            "输出里有失败/跳过的字样，但退出码是 0，而且确实改了东西",
            "部分失败被吞掉了。要不要返回非 0 是设计选择，但下游的自动化脚本"
            "（CI、cron）会把它当成全部成功。",
            [c.line[:110] for c in claims if c.kind == "failure"][:4],
            penalty=0.8,
        ))

    # 7) 退出码非 0 —— 这是"好消息"，说明它至少敢报错
    if exit_code != 0:
        findings.append(Finding(
            "info",
            f"退出码 {exit_code}，脚本自己报告了失败",
            "这类失败反而好排查：堆栈或者错误信息会直接指到出问题的地方。"
            "本工具主要针对的是另一种——退出码 0 的那种。",
            [(stderr or "").strip().splitlines()[-1][:120]] if (stderr or "").strip() else [],
            penalty=0.0,
        ))

    # 8) 子进程：退出码非零，但脚本照样报告成功
    #    这是"退出码 0 的静默失败"最大的一个变种：主进程一切正常，
    #    干活的那个子进程失败了，而 subprocess.run 忘了 check=True。
    procs = list(procs or [])
    if procs:
        bad = _real_failures(procs)
        if bad:
            lines = [f"{p.stamp()}  {p.short}   → 退出码 {p.code}"
                     for p in bad[:5]]
            if says_done and exit_code == 0:
                findings.append(Finding(
                    "fail",
                    f"{len(bad)} 个子进程以非零码退出，脚本仍然报告成功",
                    "主进程没抛异常，因为 subprocess.run 不会因为子进程失败而抛 —— "
                    "除非写了 check=True。于是「转换失败」变成了「✓ 完成」，"
                    "而产物根本没生成。",
                    lines,
                    penalty=5.0,
                ))
            elif exit_code != 0:
                findings.append(Finding(
                    "info",
                    f"{len(bad)} 个子进程以非零码退出",
                    "主脚本自己也报了失败，所以这是一次诚实的失败，而不是静默失败。",
                    lines,
                    penalty=0.0,
                ))
            else:
                findings.append(Finding(
                    "warn",
                    f"{len(bad)} 个子进程以非零码退出，脚本没有任何表示",
                    "脚本没说要干什么，所以不能确定这是问题。但如果它接下来"
                    "还要用这些子进程的产物，那些产物可能是不完整的。",
                    lines,
                    penalty=1.5,
                ))
        unfinished = [p for p in procs if not p.finished]
        if unfinished:
            findings.append(Finding(
                "warn",
                f"{len(unfinished)} 个子进程在脚本结束时还没退出",
                "脚本没等它结束就走了。它可能还在后台写文件，此时拍下的"
                "快照是不完整的 —— 产物有可能在报告生成之后才落盘。",
                [f"{p.stamp()}  {p.short}" for p in unfinished[:4]],
                penalty=1.0,
            ))
        if not bad and not unfinished:
            tolerated = [p for p in procs if p.failed]
            if tolerated:
                findings.append(Finding(
                    "info",
                    f"调用了 {len(procs)} 次子进程，其中 {len(tolerated)} 次退出码非零",
                    "这几个命令的退出码 1 有别的含义（git diff 表示\"有差异\"、"
                    "grep/find 表示\"没匹配\"），不算失败，所以没有扣分。",
                    [f"{p.stamp()}  {p.short}  → {p.code}" for p in tolerated[:4]],
                    penalty=0.0,
                ))
            else:
                findings.append(Finding(
                    "info",
                    f"调用了 {len(procs)} 次子进程，全部正常退出",
                    "退出码都拿到了。注意子进程**内部**的读写仍然看不见，"
                    "只有当前 Python 进程受审计钩子覆盖。",
                    [f"{p.stamp()}  {p.short}  ({p.duration:.2f}s)"
                     for p in procs[:4]],
                    penalty=0.0,
                ))
    elif trace.subprocs:
        # 有审计事件但没抓到最后状态：多半走的是 os.system / spawn
        findings.append(Finding(
            "info",
            f"调用了 {len(trace.subprocs)} 次子进程，但拿不到它们的退出码",
            "这些调用没走 subprocess.Popen（可能是 os.system / os.spawn*），"
            "所以无法判断它们成功与否。它们的读写只能靠文件快照兜底。",
            [c[:100] for c in trace.subprocs[:5]],
            penalty=0.4,
        ))

    if not findings:
        findings.append(Finding(
            "info",
            "没发现自相矛盾的地方",
            "声称、系统调用、文件变化三边对得上。注意这只说明\"报告可信\"，"
            "不说明业务逻辑是对的。",
            [],
            penalty=0.0,
        ))

    for f in findings:
        score -= f.penalty
    score = max(0.0, min(10.0, score))

    return Verdict(score, findings, claimed_total, written, real, exit_code)


def add_source_hints(verdict: Verdict, source: str,
                     keys: list[str] | None = None) -> Verdict:
    """只有在"报告不可信"时才去源码里找线索 —— 否则满屏噪音。"""
    from .hints import hints as _hints
    if verdict.score >= 9.0:
        return verdict
    verdict.hints = _hints(source, keys=keys)
    return verdict
