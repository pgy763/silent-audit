"""自测。不需要 pytest：``python tools/test_all.py``。

重点不是"能抓出坏的"，而是**"不冤枉好的"** —— 一个诊断工具最大的风险
是天天喊狼来了，最后没人看它。所以下面的用例里一半是正常的脚本。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from silentaudit import judge, runner, snapshot, trace  # noqa: E402
from silentaudit.hints import collect_keys, hints  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


class Temp:
    """沙箱里 tempfile.mkdtemp() 出来的是不可写目录，所以自己 mkdir。"""

    _seq = 0

    def __init__(self):
        base = ROOT / "out" / "testtmp"
        base.mkdir(parents=True, exist_ok=True)
        while True:
            Temp._seq += 1
            p = base / f"t{Temp._seq}"
            if not p.exists():
                break
        p.mkdir(parents=True)
        self.path = p

    def write(self, name: str, text: str) -> Path:
        p = self.path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def __enter__(self):
        return self

    def __exit__(self, *a):
        shutil.rmtree(self.path, ignore_errors=True)


# ---------------------------------------------------------------- 声称解析
def test_claims():
    print("\n[声称解析]")
    cases = [
        ("迁移完成：处理 1284 行", "count", 1284),
        ("processed 42 files", "count", 42),
        ("共 1,234 个文件已更新", "count", 1234),
        ("处理 0 行", "count", 0),
    ]
    for line, kind, val in cases:
        got = judge.read_claims(line, "")
        hit = [c for c in got if c.kind == "count"]
        check(f"读出数量 {line[:22]!r}", bool(hit) and hit[0].value == val,
              f"-> {got}")

    check("成功词被识别", any(c.kind == "success"
                          for c in judge.read_claims("全部完成", "")))
    check("失败词进 stderr",
          any(c.kind == "failure"
              for c in judge.read_claims("", "Error: file not found")))
    check("同一行既有数字又有完成",
          {c.kind for c in judge.read_claims("处理 10 行，完成", "")}
          == {"count", "success"})
    check("空输入不炸", judge.read_claims("", "") == [])


# ---------------------------------------------------------------- 源码提示
def test_hints():
    print("\n[源码提示]")
    src = (
        "import os\n"
        "for i in range(3):\n"
        "    name = cfg.get('url', '')\n"
        "    if not name:\n"
        "        continue\n"
        "try:\n"
        "    go()\n"
        "except Exception:\n"
        "    pass\n"
    )
    hs = hints(src, keys=["URL", "id"])
    lines = {h.line: h.reason for h in hs}
    check("找到 .get 默认值", 3 in lines)
    check("指出大小写不一致", "URL" in lines.get(3, ""), f"-> {lines.get(3)}")
    check("找到循环里的 continue", 5 in lines)
    check("找到 except: pass", 9 in lines)
    check("正常代码不报警",
          hints("x = d['url']\ny = 1 + 2\n", keys=[]) == [])
    check("语法错误不炸", hints("def (:\n") == [])
    check("表头解析", collect_keys("id,URL,note") == ["id", "URL", "note"])


# ---------------------------------------------------------------- 快照对比
def test_snapshot():
    print("\n[快照对比]")
    with Temp() as t:
        (t.path / "a.txt").write_text("one\n", encoding="utf-8")
        (t.path / "b.txt").write_text("two\n", encoding="utf-8")
        before = snapshot.take([str(t.path)])
        check("快照计数", len(before) == 2, f"-> {len(before)}")

        (t.path / "a.txt").write_text("changed\n", encoding="utf-8")  # 真改
        (t.path / "b.txt").write_text("two\n", encoding="utf-8")      # 写同样的
        (t.path / "c.txt").write_text("new\n", encoding="utf-8")      # 新建
        after = snapshot.take([str(t.path)])
        d = snapshot.compare(before, after)

        check("认出真改动", any(p.endswith("a.txt") for p in d.changed),
              f"-> {d.changed}")
        check("认出新建", any(p.endswith("c.txt") for p in d.created))
        check("写同样内容不算 changed",
              not any(p.endswith("b.txt") for p in d.changed))
        check("real_change 计数", d.real_change == 2, f"-> {d.real_change}")

        (t.path / "a.txt").unlink()
        d2 = snapshot.compare(after, snapshot.take([str(t.path)]))
        check("认出删除", any(p.endswith("a.txt") for p in d2.deleted))


# ---------------------------------------------------------------- 追踪
def test_trace():
    print("\n[系统调用追踪]")
    with Temp() as t:
        tr = trace.Trace([str(t.path)])
        target = t.path / "x.txt"
        with trace.capture(tr):
            target.write_text("hi", encoding="utf-8")
            target.read_text(encoding="utf-8")
        s = tr.summary
        check("记录到写入", s.write_count >= 1, f"-> {s.writes}")
        check("记录到读取", s.read_count >= 1)
        check("write_paths 命中", any(p.endswith("x.txt")
                                      for p in s.write_paths))

        tr2 = trace.Trace([str(t.path)])
        with trace.capture(tr2):
            (t.path / "b").mkdir(exist_ok=True)
            os.rename(t.path / "x.txt", t.path / "b" / "y.txt")
        check("记录到 rename", len(tr2.summary.renames) == 1,
              f"-> {tr2.summary.renames}")


# ---------------------------------------------------------------- 端到端
def test_e2e():
    print("\n[端到端：应该被抓到的]")
    with Temp() as t:
        (t.path / "rows.csv").write_text(
            "id,URL\n1,http://old.example.com/1\n2,http://old.example.com/2\n",
            encoding="utf-8")

        bad = t.write("bad.py", '''\
import csv
rows = list(csv.DictReader(open("rows.csv", encoding="utf-8")))
n = 0
with open("out.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["id", "URL"])
    for r in rows:
        if "old.example.com" in r.get("url", ""):
            w.writerow([r["id"], r["URL"]])
            n += 1
print(f"迁移完成，处理 {n} 行")
''')
        res = runner.run(bad, watch=[str(t.path)], quiet=True)
        check("坏脚本被扣分", res.verdict.score < 5,
              f"-> {res.verdict.score}")
        check("认出了 0 项", res.verdict.claimed_total == 0,
              f"-> {res.verdict.claimed_total}")
        check("给出了源码线索", any("URL" in h.reason for h in res.verdict.hints),
              f"-> {[(h.line, h.reason) for h in res.verdict.hints]}")

    print("\n[端到端：不该被冤枉的]")
    with Temp() as t:
        (t.path / "rows.csv").write_text(
            "id,URL\n1,http://old.example.com/1\n", encoding="utf-8")
        good = t.write("good.py", '''\
import csv
rows = list(csv.DictReader(open("rows.csv", encoding="utf-8")))
n = 0
with open("out.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["id", "URL"])
    for r in rows:
        w.writerow([r["id"], r["URL"].replace("old", "new")])
        n += 1
print(f"迁移完成，处理 {n} 行")
''')
        res = runner.run(good, watch=[str(t.path)], quiet=True)
        check("好脚本得高分", res.verdict.score >= 9,
              f"-> {res.verdict.score} / {[f.title for f in res.verdict.findings]}")
        check("好脚本不报源码线索", res.verdict.hints == [],
              f"-> {res.verdict.hints}")
        check("文件真的变了", res.diff.real_change >= 1)

    print("\n[端到端：崩溃的脚本]")
    with Temp() as t:
        boom = t.write("boom.py", 'raise RuntimeError("炸了")\n')
        res = runner.run(boom, watch=[str(t.path)], quiet=True)
        check("崩溃时退出码为 1", res.exit_code == 1, f"-> {res.exit_code}")
        check("traceback 被捕获", "RuntimeError" in res.stderr)
        check("崩溃不扣可信度",
              any("退出码 1" in f.title for f in res.verdict.findings))


def main() -> int:
    print("silent-audit 自测")
    test_claims()
    test_hints()
    test_snapshot()
    test_trace()
    test_e2e()
    total = PASS + FAIL
    print(f"\n{total} 项：{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
