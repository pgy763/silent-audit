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

from silentaudit import expect, judge, replay, runner, snapshot, trace  # noqa: E402
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


# ---------------------------------------------------------------- 换行
def test_wrap():
    print("\n[换行]")
    from silentaudit.report import _wrap

    # 英文单词不能被劈开：check=True 曾断成 check=T / rue
    lines = _wrap("除非写了 check=True。于是「转换失败」变成了「完成」。", 30)
    check("英文单词没被劈开", all("check=T" != l.strip() for l in lines), f"-> {lines}")
    check("check=True 完整出现在某一行",
          any("check=True" in l for l in lines), f"-> {lines}")

    # 中文之间可以断，且每行宽度不超限
    long = "这是一段很长的中文" * 6
    ls = _wrap(long, 20)
    widths = [sum(2 if ord(c) > 0x2E80 else 1 for c in l) for l in ls]
    check("中文按宽度断行", all(w <= 20 for w in widths), f"-> {widths}")
    check("中文没丢字", "".join(ls) == long, f"-> {len(''.join(ls))} vs {len(long)}")

    # 超长路径（单段就超宽）要硬切而不是溢出
    path = "D:/" + "x" * 60 + "/f.txt"
    pls = _wrap(path, 20)
    check("超长路径被硬切", all(len(l) <= 20 for l in pls), f"-> {[len(l) for l in pls]}")
    check("超长路径没丢字符", "".join(pls) == path, f"-> {''.join(pls)}")


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


# ---------------------------------------------------------------- 写错地方
def _snap(d):
    """把 {path: (size, digest)} 变成 Snapshot。mtime 一律给 0。"""
    return snapshot.Snapshot({k: (v[0], 0, v[1]) for k, v in d.items()})


def test_shrink():
    NL = chr(10)
    print("\n[写空壳与体积骤减]")

    before = _snap({"a.html": (40000, "old"), "b.txt": (200000, "big"),
                    "c.txt": (10, "tiny")})
    after = _snap({"a.html": (0, "empty"), "b.txt": (100, "cut"),
                   "c.txt": (0, "gone")})
    d = snapshot.compare(before, after)
    check("写空壳被单独列出来", d.emptied == ["a.html"], f"-> {d.emptied}")
    check("体积骤减被单独列出来", d.shrunk == ["b.txt"], f"-> {d.shrunk}")
    check("小文件变小不算空壳", "c.txt" not in d.emptied, f"-> {d.emptied}")
    check("空壳同时也在 changed 里", "a.html" in d.changed)

    # 正常转换：只新建、不改旧文件，不该有任何告警
    before = _snap({"in.csv": (20000, "csv")})
    after = _snap({"in.csv": (20000, "csv"), "out.json": (50000, "json")})
    d = snapshot.compare(before, after)
    check("新建产物不产生空壳告警",
          not d.emptied and not d.shrunk and d.created == ["out.json"])

    # 就地压缩：体积掉到 1/2000，只提醒，不能判 fail
    # 真实 Diff 里的路径都是绝对路径，roots 也是；这里必须一致，
    # 否则会额外命中"写到目录之外"那条，把分数多扣 1.5。
    root = str(ROOT)
    big = os.path.join(root, "big.txt")
    before = _snap({big: (200000, "big")})
    after = _snap({big: (100, "cut")})
    d2 = snapshot.compare(before, after)
    v = judge.judge(0, "压缩完成", "", trace.TraceSummary(), d2,
                   [root], "", True)
    check("体积骤减只判 warn", any(f.level == "warn" for f in v.findings),
          f"-> {[f.level for f in v.findings]}")
    check("体积骤减扣得比空壳轻", v.score >= 7.0, f"-> {v.score}")

    # 路径大小写不一致，不该被当成"写到目录之外"
    mixed = str(ROOT).upper() + os.sep + "MixCase.txt"
    before = _snap({mixed: (50000, "a")})
    after = _snap({mixed: (40000, "b")})
    dm = snapshot.compare(before, after)
    v = judge.judge(0, "更新完成", "", trace.TraceSummary(), dm,
                    [str(ROOT)], "", True)
    check("盘符大小写不同不算写到目录之外",
          not any("目录之外" in f.title for f in v.findings),
          f"-> {[f.title for f in v.findings]}")

    # 空壳要重罚
    page = os.path.join(root, "r.html")
    before = _snap({page: (40000, "old")})
    after = _snap({page: (0, "empty")})
    d3 = snapshot.compare(before, after)
    v = judge.judge(0, "报告已生成", "", trace.TraceSummary(), d3,
                   [root], "", True)
    check("空壳判 fail", any(f.level == "fail" for f in v.findings),
          f"-> {[f.level for f in v.findings]}")
    check("空壳扣分明显", v.score <= 4.0, f"-> {v.score}")

    # 端到端：真的跑一个把文件清空的脚本
    with Temp() as t:
        t.write("report.html", "<html>" + "x" * 40000 + "</html>")
        src = t.write("gen.py", NL.join([
            'with open("report.html", "w", encoding="utf-8") as f:',
            '    f.write("")',
            'print("报告已生成")', '']) )
        r = runner.run(src, watch=[str(t.path)], cwd=str(t.path), quiet=True)
        check("端到端：抓到写空壳",
              any("空壳" in f.title for f in r.verdict.findings),
              f"-> {[f.title for f in r.verdict.findings]}")
        check("端到端：分数被压下来", r.verdict.score <= 4.0,
              f"-> {r.verdict.score}")

    # 端到端：正常的压缩脚本不该被冤枉
    with Temp() as t:
        t.write("big.txt", "a" * 200000)
        src = t.write("min.py", NL.join([
            'src = open("big.txt", encoding="utf-8").read()',
            'open("big.min.txt", "w", encoding="utf-8").write(src[:200])',
            'print("压缩完成")', '']) )
        r = runner.run(src, watch=[str(t.path)], cwd=str(t.path), quiet=True)
        check("端到端：正常压缩不扣分", r.verdict.score >= 9.0,
              f"-> {r.verdict.score} {[f.title for f in r.verdict.findings]}")


def test_expect():
    print("\n[写错地方]")
    with Temp() as t:
        (t.path / "data.csv").write_text("a\n", encoding="utf-8")

        dup = str(t.path / "data(1).csv")
        (t.path / "data(1).csv").write_text("a\n", encoding="utf-8")
        got = expect.classify([dup], "已保存到 data(1).csv")
        check("认出 (1) 副本", got[0].kind == "dup", f"-> {got[0].kind}")
        check("指出本该写谁",
              got[0].shadow.endswith("data.csv"), f"-> {got[0].shadow}")

        copy = str(t.path / "data copy.csv")
        (t.path / "data copy.csv").write_text("a\n", encoding="utf-8")
        check("认出 copy 副本",
              expect.classify([copy], "")[0].kind == "dup")

        # 只有副本名、没有本尊：不该怀疑
        lone = str(t.path / "report-1.csv")
        (t.path / "report-1.csv").write_text("a\n", encoding="utf-8")
        check("没有本尊不算副本",
              expect.classify([lone], "")[0].kind == "stray")

        # 正常新建的报告：提到过 / 白名单
        rep = str(t.path / "report.html")
        (t.path / "report.html").write_text("x\n", encoding="utf-8")
        check("提到过的新文件不报警",
              expect.classify([rep], "报告写好了：report.html")[0].kind
              == "mentioned")
        check("白名单生效",
              expect.classify([rep], "", allow=["*.html"])[0].kind == "allowed")
        check("白名单也认目录形式",
              expect.classify([rep], "", allow=[str(t.path / "*")])[0].kind
              == "allowed")

    print("\n[端到端：写错地方会被扣分]")
    with Temp() as t:
        (t.path / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        bad = t.write("wrong_target.py", '''\\
rows = open("data.csv", encoding="utf-8").read().splitlines()
with open("data(1).csv", "w", encoding="utf-8") as f:
    for line in rows:
        f.write(line.upper() + "\\n")
print("已保存到 data(1).csv")
''')
        res = runner.run(bad, watch=[str(t.path)], quiet=True)
        check("写错地方被扣分", res.verdict.score < 5,
              f"-> {res.verdict.score}")
        check("给出了正确证据",
              any("data.csv" in str(f.evidence) for f in res.verdict.findings),
              f"-> {[f.evidence for f in res.verdict.findings]}")

    print("\n[端到端：白名单能放过正常产出]")
    with Temp() as t:
        (t.path / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        ok = t.write("make_report.py", '''\\
rows = open("data.csv", encoding="utf-8").read().splitlines()
with open("report.html", "w", encoding="utf-8") as f:
    f.write("<h1>%d</h1>" % len(rows))
print("报告生成：report.html")
''')
        res = runner.run(ok, watch=[str(t.path)], quiet=True,
                         allow_new=["*.html"])
        check("白名单里的新文件不扣分", res.verdict.score >= 9,
              f"-> {res.verdict.score} / "
              f"{[f.title for f in res.verdict.findings]}")


# ---------------------------------------------------------------- 两次运行
def test_replay():
    print("\n[两次运行对比]")

    # 纯函数部分：拿两次假的运行结果直接比对
    def fake(exit_code=0, claim=None, changed=0, out="ok", procs=()):
        v = judge.Verdict(score=10.0, findings=[], claimed_total=claim,
                          written_files=changed, real_change=changed,
                          exit_code=exit_code)
        d = snapshot.Diff(changed=["a"] * changed, touched=[], created=[],
                          deleted=[], unchanged=[])
        r = runner.RunResult(exit_code=exit_code, stdout=out, stderr="",
                             duration=0.1, trace=None, diff=d, verdict=v,
                             snapshot_note="")
        r.procs = list(procs)
        return r

    f, pen = replay.compare(fake(), fake())
    check("两次完全一样：不扣分", pen == 0.0 and not f[0].penalty,
          f"-> {pen}")
    check("两次完全一样：有一条 info 说明一致性",
          f[0].level == "info" and "一致" in f[0].title, f"-> {f[0].title}")

    f, pen = replay.compare(fake(exit_code=0), fake(exit_code=3))
    check("第一次成功第二次失败：重罚", pen >= 5.0, f"-> {pen}")
    check("第一次成功第二次失败：判 fail", f[0].level == "fail", f"-> {f[0].level}")

    f, pen = replay.compare(fake(claim=5), fake(claim=2))
    check("两次数字不同：点名两个数字", "5" in f[0].title and "2" in f[0].title,
          f"-> {f[0].title}")
    check("两次数字不同：扣分", pen >= 4.0, f"-> {pen}")

    f, pen = replay.compare(fake(claim=3, changed=4), fake(claim=3, changed=0))
    check("第一次改第二次没改：提醒可能是幂等",
          "幂等" in f[0].detail, f"-> {f[0].detail[:40]}")

    f, pen = replay.compare(fake(out="批次号 1958"), fake(out="批次号 3674"))
    check("只有输出文字不同：轻罚", 0 < pen <= 2.0, f"-> {pen}")
    check("只有输出文字不同：标 warn", f[0].level == "warn", f"-> {f[0].level}")

    # 路径和耗时不该被当成差异
    a = fake(out="写入 D:\\tmp\\x\\a.csv 耗时 0.42s")
    b = fake(out="写入 D:\\tmp\\x\\a.csv 耗时 0.37s")
    f, pen = replay.compare(a, b, work="D:\\tmp\\x")
    check("路径和耗时不产生噪音", pen == 0.0, f"-> {pen} {f[0].title}")

    # 端到端：副本隔离（原文件必须一个字节都不变）
    with Temp() as t:
        src = t.write("script.py",
                      'with open("data.csv", "w", encoding="utf-8") as f:\n'
                      '    f.write("1,2\\n")\n'
                      'print("处理完成：共 1 行")\n')
        t.write("seed.txt", "already here")
        before = (t.path / "seed.txt").stat().st_mtime_ns
        rp = replay.run_replay(src, watch=[str(t.path)], cwd=str(t.path),
                               keep=True)
        check("端到端：跑了两次", rp.a is not None and rp.b is not None)
        check("端到端：原目录的种子文件没被动过",
              (t.path / "seed.txt").stat().st_mtime_ns == before)
        check("端到端：原目录没多出 data.csv",
              not (t.path / "data.csv").exists())
        check("端到端：两次结果一致", not rp.diverged, f"-> {rp.findings}")
        check("端到端：--keep 留下了副本目录",
              (Path(rp.directory) / "work").is_dir())

        # 副本里同一个脚本连跑两遍必须都真的改了文件（说明重置是有效的）
        check("端到端：两次各自的落盘改动都非零",
              rp.a.diff.real_change > 0 and rp.b.diff.real_change > 0,
              f"-> {rp.a.diff.real_change}/{rp.b.diff.real_change}")

    # 端到端：不 keep 时应当清理干净
    with Temp() as t:
        src = t.write("s.py", 'print("nothing")\n')
        rp = replay.run_replay(src, watch=[str(t.path)], cwd=str(t.path))
        check("端到端：不 keep 时副本目录被删掉",
              not (t.path / replay.REPLAY_DIR).exists())

    # 渲染不炸
    with Temp() as t:
        src = t.write("s.py", 'print("处理完成：共 0 行")\n')
        rp = replay.run_replay(src, watch=[str(t.path)], cwd=str(t.path))
        from silentaudit import report as report_mod
        txt = report_mod.render_replay(rp, base=str(t.path))
        check("渲染：有标题", "可复现" in txt or "不可复现" in txt)
        check("渲染：列出两次", "第 1 次" in txt and "第 2 次" in txt)


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


# ---------------------------------------------------------------- 子进程
def test_procs():
    print("\n[子进程观测]")
    from silentaudit import procwatch

    with procwatch.ProcWatch() as w:
        import subprocess
        import sys as _s
        subprocess.run([_s.executable, "-c", "pass"])
        subprocess.run([_s.executable, "-c", "import sys; sys.exit(3)"])

    check("抓到两次调用", len(w.recs) == 2, f"-> {[r.cmd for r in w.recs]}")
    check("退出码被记录", [r.code for r in w.recs] == [0, 3],
          f"-> {[r.code for r in w.recs]}")
    check("失败的那个被认出来", len(w.failed()) == 1,
          f"-> {len(w.failed())}")
    check("recs 按时间排序",
          all(w.recs[i].start <= w.recs[i + 1].start for i in range(len(w.recs) - 1)))
    check("stamp 是相对时刻", w.recs[0].stamp().startswith("+"),
          f"-> {w.recs[0].stamp()}")
    check("退出后方法已恢复", subprocess.Popen.wait.__qualname__.startswith("Popen"),
          f"-> {subprocess.Popen.wait.__qualname__}")

    # 命令不存在：起不来本身也是证据，而且不能把异常吞掉
    with procwatch.ProcWatch() as w2:
        try:
            subprocess.run(["definitely-not-a-real-cmd-xyz"])
        except (OSError, FileNotFoundError):
            pass
    check("起不来的命令被记成失败",
          len(w2.recs) == 1 and w2.recs[0].code == -1 and bool(w2.recs[0].error),
          f"-> {[(r.code, r.error[:40]) for r in w2.recs]}")

    print("\n[子进程：不该冤枉 git diff]")
    from silentaudit import snapshot as _snap
    diff = _snap.Diff([], [], [], [], [])
    ts = trace.TraceSummary()

    class FakeProc:
        def __init__(self, argv, code):
            self.argv, self.code, self.cmd = argv, code, " ".join(argv)
            self.start = self.end = 1.0
            self.finished = True
            self.duration = 0.1

        def stamp(self):
            return "+0.10s"

        @property
        def short(self):
            return self.cmd

        @property
        def failed(self):
            return self.code != 0

    # 这个用例只测子进程判据，所以关掉"没拍快照就别谈磁盘"的无关告警
    quiet = [FakeProc(["git", "diff", "--quiet"], 1)]
    v = judge.judge(0, "完成", "", ts, diff, [str(ROOT)], str(ROOT),
                    snapshot_taken=False, procs=quiet)
    check("git diff --quiet 退出 1 不算失败", v.score >= 9,
          f"-> {v.score} / {[f.title for f in v.findings]}")

    loud = [FakeProc(["git", "push"], 1)]
    v2 = judge.judge(0, "推送完成", "", ts, diff, [str(ROOT)], str(ROOT),
                     snapshot_taken=False, procs=loud)
    check("git push 退出 1 仍算失败", v2.score < 6,
          f"-> {v2.score} / {[f.title for f in v2.findings]}")

    print("\n[端到端：子进程失败但报告成功]")
    with Temp() as t:
        bad = t.write("bad_sub.py", '''\
import subprocess
import sys
subprocess.run([sys.executable, "-c", "import sys; sys.exit(2)"])
print("转换完成")
''')
        res = runner.run(bad, watch=[str(t.path)], quiet=True)
        check("子进程失败被扣分", res.verdict.score < 6,
              f"-> {res.verdict.score} / {[f.title for f in res.verdict.findings]}")
        check("证据里有退出码",
              any("退出码 2" in str(f.evidence) for f in res.verdict.findings),
              f"-> {[f.evidence for f in res.verdict.findings]}")

    print("\n[端到端：子进程成功的不该扣分]")
    with Temp() as t:
        good = t.write("good_sub.py", '''\
import subprocess
import sys
subprocess.run([sys.executable, "-c", "print('hi')"])
with open("out.txt", "w", encoding="utf-8") as f:
    f.write("done")
print("转换完成")
''')
        res = runner.run(good, watch=[str(t.path)], quiet=True,
                         allow_new=["*.txt"])
        check("子进程全成功时得高分", res.verdict.score >= 9,
              f"-> {res.verdict.score} / {[f.title for f in res.verdict.findings]}")


def main() -> int:
    print("silent-audit 自测")
    test_wrap()
    test_claims()
    test_hints()
    test_snapshot()
    test_trace()
    test_expect()
    test_shrink()
    test_procs()
    test_replay()
    test_e2e()
    total = PASS + FAIL
    print(f"\n{total} 项：{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
