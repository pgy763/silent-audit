"""运行前后的文件快照 —— 判断"文件到底变没变"的物证。

粒度是个取舍：整个磁盘做 md5 太慢，只看被写过的文件又漏掉"该写却没写"。
折中方案是**只快照可能相关的范围**（当前目录 + 显式给的目录），并限制文件数、
限制单文件大小，保证在几万文件的仓库里也能跑完。

对超大的目录用 ``(size, mtime_ns)`` 做粗筛，只有在 mtime 变了或需要精确比对时
才算 md5 —— 这是 rsync 的思路，够用且快。
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

_SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    ".next", ".turbo", ".frames", "site-packages",
}
# 注意：**不跳过 out / dist / build**。它们恰恰是脚本最常写的地方，
# 跳过就等于对"构建产物有没有真的更新"视而不见。
_MAX_FILES = 30_000
_MAX_BYTES = 64 * 1024 * 1024


def _md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Snapshot:
    """path -> (size, mtime_ns, md5)"""
    files: dict[str, tuple[int, int, str]]
    truncated: bool = False
    skipped_big: int = 0

    def __len__(self) -> int:
        return len(self.files)


def take(roots: list[str], extra_ignore: set[str] | None = None,
         max_files: int = _MAX_FILES) -> Snapshot:
    ignore = _SKIP_DIRS | {d.lower() for d in (extra_ignore or set())}
    out: dict[str, tuple[int, int, str]] = {}
    big = 0
    truncated = False

    for root in roots:
        root = os.path.abspath(root)
        if os.path.isfile(root):
            try:
                st = os.stat(root)
                out[os.path.normcase(root)] = (st.st_size, st.st_mtime_ns,
                                               _md5(root))
            except OSError:
                pass
            continue
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in ignore and not d.startswith(".frames")]
            for name in filenames:
                if len(out) >= max_files:
                    truncated = True
                    break
                p = os.path.join(dirpath, name)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if st.st_size > _MAX_BYTES:
                    big += 1
                    out[os.path.normcase(p)] = (st.st_size, st.st_mtime_ns, "")
                    continue
                try:
                    digest = _md5(p)
                except OSError:
                    continue
                out[os.path.normcase(p)] = (st.st_size, st.st_mtime_ns, digest)
            if truncated:
                break
        if truncated:
            break

    return Snapshot(out, truncated, big)


@dataclass
class Diff:
    changed: list[str]       # 内容真的变了
    touched: list[str]       # 写过了，但内容一样
    created: list[str]
    deleted: list[str]
    unchanged: int
    files_seen: int = 0      # 快照里看到的文件数；0 表示没做过快照

    @property
    def real_change(self) -> int:
        return len(self.changed) + len(self.created) + len(self.deleted)


def compare(before: Snapshot, after: Snapshot) -> Diff:
    changed, touched, created, deleted = [], [], [], []
    for path, (size, mtime, digest) in after.files.items():
        if path not in before.files:
            created.append(path)
            continue
        b_size, b_mtime, b_digest = before.files[path]
        if size != b_size or (digest and b_digest and digest != b_digest):
            changed.append(path)
        elif not digest or not b_digest:
            # 大文件没算 md5，只能靠 mtime 判断
            if mtime != b_mtime:
                changed.append(path)
            else:
                touched.append(path)
        elif mtime != b_mtime:
            touched.append(path)      # 写入过，但字节完全一致
    for path in before.files:
        if path not in after.files:
            deleted.append(path)
    unchanged = len(after.files) - len(changed) - len(touched) - len(created)
    return Diff(sorted(changed), sorted(touched), sorted(created),
                sorted(deleted), max(0, unchanged), len(after.files))
