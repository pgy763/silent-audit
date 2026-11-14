"""判断"新建出来的文件"是不是本该覆盖某个已有文件。

静默失败的第二种形态：**写是写了，但写错了地方**。

```
本意：覆盖 data.csv
实际：data.csv 旁边多出一个 data(1).csv
```

脚本照样退出码 0，还打印"已保存到 data(1).csv"。人一眼看不出问题，因为
文件确实生成了 —— 只是你的下游脚本还在读那个没人更新的 data.csv。

这里只做两件很窄的事，宁可漏报也不瞎报：

1. **名字像副本**：`x(1).csv` / `x copy.csv` / `x-1.csv`，且同目录下存在
   `x.csv`。这类命名几乎总是"本该覆盖却写成了新文件"。
2. **没人提过的新文件**：创建了，但输出里一次都没提到。只作为提示，不扣分 ——
   生成报告的脚本天生就会新建文件。
"""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass

# x(1).csv / x copy.csv / x copy 2.csv / x-1.csv / x_1.csv
_DUP = re.compile(
    r"^(?P<base>.+?)(?:\s*\(\d+\)|\s+copy(?:\s+\d+)?|\s*[_-]\d+)$",
    re.IGNORECASE,
)


@dataclass
class Created:
    path: str
    kind: str        # "dup" | "mentioned" | "stray" | "allowed"
    shadow: str = ""  # dup 时，本应被覆盖的那个文件
    note: str = ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def _mentioned(path: str, output: str) -> bool:
    if not output:
        return False
    name = os.path.basename(path)
    hay = _norm(output)
    return _norm(name) in hay or _norm(path) in hay


def _dup_target(path: str) -> str:
    """如果 path 看起来是某个已存在文件的副本，返回那个文件的路径。"""
    d, name = os.path.split(path)
    stem, ext = os.path.splitext(name)
    m = _DUP.match(stem)
    if not m:
        return ""
    base = m.group("base").strip()
    if not base:
        return ""
    cand = os.path.join(d, base + ext)
    # 只有"本尊"确实存在时，这个副本才值得怀疑
    return cand if os.path.exists(cand) else ""


def classify(created: list[str], output: str,
             allow: list[str] | None = None) -> list[Created]:
    allow = [a for a in (allow or []) if a]
    out: list[Created] = []
    for p in created:
        if any(fnmatch.fnmatch(os.path.basename(p).lower(), a.lower())
               or fnmatch.fnmatch(p.lower(), a.lower()) for a in allow):
            out.append(Created(p, "allowed"))
            continue
        shadow = _dup_target(p)
        if shadow:
            out.append(Created(
                p, "dup", shadow,
                f"同目录下已有 {os.path.basename(shadow)}，这个更像是没覆盖成"))
            continue
        if _mentioned(p, output):
            out.append(Created(p, "mentioned"))
            continue
        out.append(Created(
            p, "stray", "",
            "输出里没提到这个文件；如果它不是预期产物，说明路径写错了"))
    return out
