"""命令行。

用法：

    silent-audit run script.py [脚本自己的参数...]
    silent-audit run script.py --watch ./data --no-snapshot
    silent-audit run script.py --json report.json

``--`` 之后的参数会原样传给被测脚本，和 `python script.py ...` 的语义一致。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import __version__, report as report_mod, runner


def _split_forward(argv: list[str]) -> tuple[list[str], list[str]]:
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="silent-audit",
        description="判断一个脚本的「我干完了」到底可不可信。",
        epilog="例：silent-audit run migrate.py --watch ./data --json out.json",
    )
    p.add_argument("-V", "--version", action="version",
                   version=f"silent-audit {__version__}")
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="跑一个脚本并审计它")
    r.add_argument("script", help="要审计的 .py 文件")
    r.add_argument("--watch", "-w", action="append", metavar="DIR",
                   help="盯住哪些目录（可重复）。默认盯脚本所在目录")
    r.add_argument("--cwd", help="在哪个目录下执行（默认脚本所在目录）")
    r.add_argument("--no-snapshot", action="store_true",
                   help="不拍文件快照，只看系统调用（很快，但判断不了\"内容有没有真变\"）")
    r.add_argument("--json", metavar="PATH", help="把结构化结果写到文件")
    r.add_argument("--quiet", "-q", action="store_true", help="少打点进度")
    r.add_argument("--no-color", action="store_true", help="不要颜色")
    r.add_argument("--verbose", "-v", action="store_true", help="列出所有文件明细")
    r.add_argument("--allow-new", action="append", metavar="GLOB",
                   help="允许新建的文件（可重复，支持 * 通配）。"
                        "用来避免把正常生成的报告误判成\"写错地方\"")
    r.add_argument("--fail-under", type=float, metavar="N",
                   help="可信度低于 N 就以非 0 退出（给 CI 用）")

    rp = sub.add_parser(
        "replay",
        help="同一个起点连跑两遍，看两次结果是否一致")
    rp.add_argument("script", help="要审计的 .py 文件")
    rp.add_argument("--watch", "-w", action="append", metavar="DIR",
                    help="盯住哪些目录（可重复）。默认盯脚本所在目录")
    rp.add_argument("--cwd", help="副本放在哪个目录下（默认当前目录）")
    rp.add_argument("--allow-new", action="append", metavar="GLOB",
                    help="允许新建的文件（可重复，支持 * 通配）")
    rp.add_argument("--json", metavar="PATH", help="把结构化结果写到文件")
    rp.add_argument("--no-color", action="store_true")
    rp.add_argument("--verbose", "-v", action="store_true", help="列出文件明细")
    rp.add_argument("--keep", action="store_true",
                    help="跑完保留副本目录，方便自己进去看")
    rp.add_argument("--fail-under", type=float, metavar="N",
                    help="可信度低于 N 就以非 0 退出（给 CI 用）")

    s = sub.add_parser("demo", help="跑内置的五个失败样本，看看它到底抓什么")
    s.add_argument("--no-color", action="store_true")
    return p


DEMO_DIR_NAME = "_silent_audit_demo"


def _demo() -> int:
    """造四个真会静默失败的脚本，逐个审计。

    每次运行都先把样本目录整个重建 —— 否则上一轮产物还在，脚本第二次
    "新建" data(1).csv 就变成了"覆盖同名文件"，第四个样本会失效。
    """
    base = Path.cwd() / DEMO_DIR_NAME
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)

    (base / "rows.csv").write_text(
        "id,URL\n" + "".join(f"{i},http://old.example.com/{i}\n" for i in range(1000)),
        encoding="utf-8")
    (base / "conf.json").write_text(
        '{\n    "timeout": 30,\n    "retries": 3\n}\n', encoding="utf-8")
    (base / "notes.txt").write_text("hello\n" * 200, encoding="utf-8")
    # 样本 6 要用到：一份已经存在、有内容的报告。没有它，
    # "写空壳"就退化成"新建了一个空文件"，判据本来就不该报。
    (base / "report.html").write_text(
        "<html><body>\n" + "<p>旧内容</p>\n" * 600 + "</body></html>\n",
        encoding="utf-8")

    (base / "1_field_case.py").write_text('''\
"""字段名大小写错了：1000 行全部静默跳过。"""
import csv

src = list(csv.DictReader(open("rows.csv", encoding="utf-8")))
n = 0
with open("out.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["id", "URL"])
    for r in src:
        if "old.example.com" in r.get("url", ""):   # 真实字段是 "URL"
            w.writerow([r["id"], r["URL"].replace("old", "new")])
            n += 1
print(f"迁移完成：处理 {n} 行 / 共 {len(src)} 行")
''', encoding="utf-8")

    (base / "2_noop_write.py").write_text('''\
"""把一个配置项设成它本来就是的值，然后原样写回。"""
import json

cfg = json.load(open("conf.json", encoding="utf-8"))
cfg["timeout"] = 30
with open("conf.json", "w", encoding="utf-8") as f:
    f.write(json.dumps(cfg, ensure_ascii=False, indent=4) + "\\n")
print("timeout 已设为 30，配置已更新")
''', encoding="utf-8")

    (base / "3_wrong_newline.py").write_text('''\
"""看似清洗了文本，其实写回去的字节和原来一模一样。"""
p = "notes.txt"
text = open(p, encoding="utf-8").read()
cleaned = [ln.strip() for ln in text.splitlines()]
open(p, "w", encoding="utf-8").write("\\n".join(cleaned) + "\\n")
print(f"清洗完成，共处理 {len(cleaned)} 行")
''', encoding="utf-8")

    # 4：写是写了，但写成了另一个文件
    (base / "data.csv").write_text(
        "id,value\n" + "".join(f"{i},{i * 2}\n" for i in range(50)),
        encoding="utf-8")
    (base / "4_wrong_target.py").write_text('''"""路径拼成了副本名：本该覆盖 data.csv，结果另存成 data(1).csv。"""
rows = open("data.csv", encoding="utf-8").read().splitlines()
with open("data(1).csv", "w", encoding="utf-8") as f:
    for line in rows:
        f.write(line.replace(",", "\\t") + "\\n")
print("已保存到 data(1).csv，共", len(rows), "行")
''', encoding="utf-8")

    # 5：子进程失败了，但忘了 check=True
    (base / "5_failed_subproc.py").write_text('''# 调用的子进程失败了，但忘了 check=True，于是照样报告成功。
import subprocess
import sys

with open("slides.txt", "w", encoding="utf-8") as f:
    f.write("")

subprocess.call([sys.executable, "-c", "import sys; sys.exit(3)"])
print("转换完成：1 个文件 → slides.txt")
''', encoding="utf-8")

    # 6：打开就清空，写入那行被跳过，文件成了空壳
    (base / "6_emptied.py").write_text("""# open(..., "w") 在打开那一刻就清空文件。
# 写入被跳过、异常又被吞掉时，你得到的不是旧文件，而是一个空的"新"文件。
import json

rows = []
try:
    rows = json.load(open("rows.csv", encoding="utf-8"))
except Exception:
    pass  # 读失败就算了 —— 这里把异常吞掉了

with open("report.html", "w", encoding="utf-8") as f:
    if rows:
        f.write("<html><body>")
        for r in rows:
            f.write(f"<p>{r}</p>")
        f.write("</body></html>")

print("报告已生成：report.html")
""", encoding="utf-8")

    report_mod.setup(False)
    print(f"\n  在 {base} 生成了六个样本，它们的共同点是："
          f"退出码 0、打印成功、但结果不是你想要的。\n")

    rc = 0
    for name in ("1_field_case.py", "2_noop_write.py", "3_wrong_newline.py",
                 "4_wrong_target.py", "5_failed_subproc.py", "6_emptied.py"):
        script = base / name
        res = runner.run(script, watch=[str(base)], quiet=True)
        print(report_mod.C.w(f"━━ {name} ", report_mod.C.BOLD) +
              report_mod.C.w("━" * 50, report_mod.C.GREY))
        print(report_mod.render(res, base=str(base)))
    print(f"  六个样本都在 {base}，可以直接打开看。"
          f"删掉它：rm -rf {DEMO_DIR_NAME}\n")
    return rc


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        build_parser().print_help()
        return 0

    head, rest = argv[0], argv[1:]
    if head in ("-V", "--version"):
        print(f"silent-audit {__version__}")
        return 0

    # 允许 `silent-audit script.py` 省略 run
    if head not in ("run", "demo", "-h", "--help") and head.endswith(".py"):
        argv = ["run"] + argv

    before, forwarded = _split_forward(argv)
    ns = build_parser().parse_args(before)

    if ns.cmd == "replay":
        report_mod.setup(ns.no_color)
        from .replay import run_replay

        rp = run_replay(Path(ns.script), args=forwarded, watch=ns.watch,
                        cwd=ns.cwd, allow_new=ns.allow_new, keep=ns.keep)
        print(report_mod.render_replay(rp, verbose=ns.verbose,
                                       base=os.path.abspath(ns.cwd or ".")))
        if ns.json:
            payload = {
                "script": str(Path(ns.script).resolve()),
                "score": round(rp.score, 2),
                "diverged": rp.diverged,
                "copy": rp.note,
                "runs": [{
                    "exit_code": r.exit_code,
                    "claimed_total": r.verdict.claimed_total,
                    "real_change": r.diff.real_change,
                    "score": round(r.verdict.score, 2),
                    "changed": r.diff.changed[:100],
                    "created": r.diff.created[:100],
                    "deleted": r.diff.deleted[:100],
                } for r in (rp.a, rp.b)],
                "findings": [
                    {"level": f.level, "title": f.title, "detail": f.detail,
                     "evidence": f.evidence}
                    for f in rp.findings
                ],
            }
            Path(ns.json).parent.mkdir(parents=True, exist_ok=True)
            Path(ns.json).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  {report_mod.C.w('结果已写入', report_mod.C.GREY)} {ns.json}")
        if ns.fail_under is not None and rp.score < ns.fail_under:
            return 2
        return 0

    if ns.cmd == "demo":
        report_mod.setup(ns.no_color)
        return _demo()
    if ns.cmd != "run":
        build_parser().print_help()
        return 0

    report_mod.setup(ns.no_color)
    res = runner.run(Path(ns.script), args=forwarded, watch=ns.watch,
                     cwd=ns.cwd, take_snapshot=not ns.no_snapshot,
                     quiet=ns.quiet, allow_new=ns.allow_new)
    print(report_mod.render(res, verbose=ns.verbose,
                            base=os.path.abspath(ns.cwd or
                                                 str(Path(ns.script).resolve().parent))))

    if ns.json:
        payload = {
            "script": str(Path(ns.script).resolve()),
            "exit_code": res.exit_code,
            "score": round(res.verdict.score, 2),
            "headline": res.verdict.headline,
            "duration": round(res.duration, 3),
            "claims": {
                "total": res.verdict.claimed_total,
            },
            "trace": {
                "writes": res.trace.write_count,
                "reads": res.trace.read_count,
                "files_written": len(res.trace.write_paths),
                "renames": len(res.trace.renames),
                "removes": len(res.trace.removes),
                "subprocs": len(res.trace.subprocs),
                "outside": res.trace.outside[:40],
            },
            "diff": {
                "changed": res.diff.changed[:200],
                "touched": res.diff.touched[:200],
                "created": res.diff.created[:200],
                "deleted": res.diff.deleted[:200],
                "real_change": res.diff.real_change,
            },
            "findings": [
                {"level": f.level, "title": f.title, "detail": f.detail,
                 "evidence": f.evidence}
                for f in res.verdict.findings
            ],
            "hints": [
                {"line": h.line, "code": h.code, "reason": h.reason}
                for h in res.verdict.hints
            ],
            "stdout": res.stdout[-8000:],
            "stderr": res.stderr[-8000:],
        }
        Path(ns.json).parent.mkdir(parents=True, exist_ok=True)
        Path(ns.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        print(f"  {report_mod.C.w('结果已写入', report_mod.C.GREY)} {ns.json}")

    if ns.fail_under is not None and res.verdict.score < ns.fail_under:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
