"""找"最可能解释这次静默失败"的那一行源码。

这里**不做静态分析**，只做一件事：当已经知道"读取了 N 行但一条都没写"时，
在源码里找出那些"看起来会拿到空值"的取值表达式，把行号和原文报给用户。

判定规则很窄，宁可漏报也不要瞎指：

* ``.get("x", <默认值>)`` —— 字典里可能没有这个 key；
* ``os.environ.get(..., "")`` / ``.get(...) or ""`` —— 环境变量没设；
* 循环里的 ``continue`` / ``return`` —— 大量静默跳过；
* ``except: pass`` —— 错误被吞掉。

这些模式只有在"确实发生了静默失败"时才值得报，所以由 judge 决定要不要调用它。
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass

_EMPTY_DEFAULTS = ("", [], {}, (), None, 0, False, "''", '""')


@dataclass
class Hint:
    line: int
    code: str
    reason: str


def _slice(lines: list[str], node: ast.AST) -> str:
    try:
        seg = ast.get_source_segment("\n".join(lines), node)
        return (seg or "").strip()[:120]
    except Exception:
        return ""


def hints(source: str, keys: list[str] | None = None,
          limit: int = 6) -> list[Hint]:
    """keys：从数据里读到的真实字段名，用来指认"是不是拼错了"。"""
    lines = source.splitlines()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    out: list[Hint] = []
    seen: set[int] = set()
    lower_keys = {k.lower(): k for k in (keys or [])}

    def add(node, reason):
        ln = getattr(node, "lineno", 0)
        if not ln or ln in seen or len(out) >= limit:
            return
        seen.add(ln)
        out.append(Hint(ln, _slice(lines, node), reason))

    for node in ast.walk(tree):
        # dict.get("key", <空默认值>)
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and len(node.args) >= 2):
            default = node.args[1]
            empty = (isinstance(default, ast.Constant)
                     and default.value in _EMPTY_DEFAULTS)
            is_null = (isinstance(default, ast.Constant) and default.value is None)
            if empty or is_null:
                key = ""
                if isinstance(node.args[0], ast.Constant):
                    key = str(node.args[0].value)
                if key and key in lower_keys and lower_keys[key] != key:
                    # 大小写对不上 —— 这是最常见的"永远取到空值"
                    why = (f'代码里写 "{key}"，数据里的字段是 '
                           f'"{lower_keys[key]}"，取值会永远失败')
                elif key:
                    why = f'取字段 "{key}"，字段不存在时静默返回空值'
                else:
                    why = "带默认值的取值，字段不存在时会静默返回空值"
                add(node, why)

        # except: pass
        if isinstance(node, ast.ExceptHandler) and len(node.body) == 1:
            body = node.body[0]
            if isinstance(body, ast.Pass):
                add(body, "异常被 pass 掉，出问题也不会有任何输出")

        # 循环里的 continue
        if isinstance(node, (ast.For, ast.While)):
            for child in ast.walk(node):
                if isinstance(child, ast.Continue) and \
                        getattr(child, "lineno", 0) > getattr(node, "lineno", 0):
                    add(child, "循环里跳过当前项，跳过了多少只有计数器知道")
                    break

    # 字段名拼写：脚本里出现的字符串字面量 vs 数据里真实存在的 key
    if keys:
        lower_keys = {k.lower(): k for k in keys}
        lit = re.compile(r"""["']([A-Za-z_][A-Za-z0-9_]{1,30})["']""")
        for i, text in enumerate(lines, 1):
            if i in seen:
                continue
            for m in lit.finditer(text):
                s = m.group(1)
                if s.lower() in lower_keys and lower_keys[s.lower()] != s:
                    real = lower_keys[s.lower()]
                    if len(out) < limit:
                        out.append(Hint(
                            i, text.strip()[:120],
                            f'代码里写 "{s}"，数据里的字段是 "{real}"'
                            f'（大小写不一致，取值会永远失败）'))
                    break
    return out[:limit]


def collect_keys(header_line: str) -> list[str]:
    """从 CSV 表头猜出字段名，用于大小写比对。"""
    if not header_line:
        return []
    for sep in (",", "\t", ";"):
        if sep in header_line:
            return [c.strip().strip('"\'') for c in header_line.split(sep)
                    if c.strip()]
    return []
