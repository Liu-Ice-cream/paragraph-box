"""Persistent, dependency-free paragraph search for local Markdown and text files.

The index contains only explicitly configured roots.  Symlinks and Windows
reparse points are skipped rather than followed.  Markdown front matter,
headings, horizontal rules, and fenced code do not become searchable paragraphs.
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from typing import Callable, Iterator

from .matching import normalize as _normalize


_SUFFIXES = frozenset({".md", ".markdown", ".txt"})
_ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+|$)(.*?)\s*$")
_SETEXT_HEADING = re.compile(r"^ {0,3}(=+|-+)\s*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_HORIZONTAL_RULE = re.compile(r"^ {0,3}(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$")


@dataclass(frozen=True)
class Root:
    alias: str
    path: Path
    exclude_dirs: tuple[str, ...] = ()


def is_excluded(path: Path, root: Path, excludes: tuple[str, ...]) -> bool:
    relative = path.relative_to(root).as_posix().lower()
    return any(relative == item.lower() or relative.startswith(item.lower() + "/") for item in excludes)


def index_signature(roots: list[Root], max_file_bytes: int) -> str:
    data = [[root.alias, os.path.normcase(str(root.path.resolve())), list(root.exclude_dirs)] for root in roots]
    return hashlib.sha256(json.dumps([data, max_file_bytes], ensure_ascii=False).encode("utf-8")).hexdigest()


class _Diagnostics:
    def __init__(self):
        self.warnings = []
        self.issues = []
        self.count = 0
        self.reasons = {}

    def add(self, path: Path, reason: str, message: str) -> None:
        self.count += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1
        if len(self.issues) < 50:
            self.issues.append({"path": str(path), "reason": reason, "message": message})
            self.warnings.append(f"{message}：{path}")


class _Timings:
    def __init__(self):
        self.seconds = {name: 0.0 for name in ("lock", "scan", "read", "parse", "database")}

    @contextmanager
    def measure(self, name):
        started = time.monotonic()
        try:
            yield
        finally:
            self.seconds[name] += time.monotonic() - started


@dataclass(frozen=True)
class SearchHit:
    alias: str
    path: Path
    heading: str
    text: str
    line_start: int
    line_end: int
    score: int


@dataclass(frozen=True)
class _Paragraph:
    heading: str
    text: str
    line_start: int
    line_end: int


def _decode(data: bytes) -> str:
    # A UTF-16 BOM must be checked first: GB18030 can otherwise accept some of
    # these bytes and silently produce garbled text.
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        result = data.decode("utf-16")
    else:
        try:
            result = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            result = data.decode("gb18030")
    if "\x00" in result:
        raise ValueError("文本包含空字节，可能是二进制文件或缺少编码标记")
    return result


def _paragraphs(content: str, markdown: bool) -> list[_Paragraph]:
    lines = content.splitlines()
    result: list[_Paragraph] = []
    headings: list[str] = []
    buffer: list[str] = []
    buffer_start = 0
    buffer_end = 0
    fence_char = ""
    fence_length = 0
    index = 0

    def flush() -> None:
        nonlocal buffer
        if buffer:
            text = "\n".join(buffer).strip()
            if text:
                result.append(_Paragraph(" > ".join(filter(None, headings)), text, buffer_start, buffer_end))
            buffer = []

    def set_heading(level: int, title: str) -> None:
        nonlocal headings
        flush()
        headings = headings[: level - 1]
        while len(headings) < level - 1:
            headings.append("")
        headings.append(title.strip())

    if markdown and lines and lines[0].strip() == "---":
        for closing in range(1, len(lines)):
            if lines[closing].strip() in {"---", "..."}:
                index = closing + 1
                break

    while index < len(lines):
        line = lines[index]
        line_number = index + 1
        if markdown:
            fence = _FENCE.match(line)
            if fence_char:
                if (
                    fence
                    and fence.group(1)[0] == fence_char
                    and len(fence.group(1)) >= fence_length
                    and not fence.group(2).strip()
                ):
                    fence_char = ""
                    fence_length = 0
                index += 1
                continue
            if fence:
                flush()
                fence_char = fence.group(1)[0]
                fence_length = len(fence.group(1))
                index += 1
                continue
            atx = _ATX_HEADING.match(line)
            if atx:
                # Closing hashes count as markup only when preceded by space.
                title = re.sub(r"[ \t]+#+[ \t]*$", "", atx.group(2))
                set_heading(len(atx.group(1)), title)
                index += 1
                continue
            if line.strip() and index + 1 < len(lines):
                setext = _SETEXT_HEADING.match(lines[index + 1])
                if setext:
                    # Setext headings may span several adjacent text lines.
                    title = " ".join(buffer + [line])
                    buffer = []
                    set_heading(1 if setext.group(1)[0] == "=" else 2, title)
                    index += 2
                    continue
            if _HORIZONTAL_RULE.match(line):
                flush()
                index += 1
                continue
        if not line.strip():
            flush()
        else:
            if not buffer:
                buffer_start = line_number
            buffer.append(line)
            buffer_end = line_number
        index += 1
    flush()
    return result


def _is_reparse_point(metadata: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(metadata, "st_file_attributes", 0) & flag)


class ParagraphIndex:
    """A reusable SQLite index; each launcher process may open its own instance.

    ``sync`` scans configured roots and reparses only changed files.  Counts for
    ``files`` and ``paragraphs`` are totals after synchronizing; ``updated`` is
    the number of newly indexed or reparsed files, and ``skipped`` counts
    supported files rejected by a size, decoding, read, or link check.  An
    unreadable directory preserves its cached files and emits a warning.
    """

    def __init__(self, cache_path: Path, roots: list[Root], max_file_bytes: int = 2_000_000):
        if max_file_bytes < 1:
            raise ValueError("max_file_bytes must be positive")
        self.roots = [Root(root.alias, Path(root.path), root.exclude_dirs) for root in roots]
        self.max_file_bytes = max_file_bytes
        cache_path = Path(cache_path)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(cache_path), timeout=15, isolation_level=None)
        try:
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA busy_timeout = 15000")
            self._connection.execute("PRAGMA foreign_keys = ON")
            # PRAGMA journal_mode can reject simultaneous first-use requests
            # immediately, even with busy_timeout.  Retry only lock failures.
            deadline = time.monotonic() + 15
            while True:
                try:
                    self._connection.execute("PRAGMA journal_mode = WAL")
                    break
                except sqlite3.OperationalError as exc:
                    code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
                    # Error-code attributes/constants arrived in Python 3.11;
                    # numeric SQLite codes keep the Python 3.10 path supported.
                    locked = code in {5, 6} or "locked" in str(exc).casefold()
                    if not locked or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            self._connection.execute("PRAGMA synchronous = NORMAL")
            self._connection.executescript(
                """
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY,
                root_key TEXT NOT NULL,
                root_path TEXT NOT NULL,
                alias TEXT NOT NULL,
                alias_key TEXT NOT NULL,
                path TEXT NOT NULL,
                mtime_ns INTEGER NOT NULL,
                size INTEGER NOT NULL,
                UNIQUE (root_key, path)
            );
            CREATE INDEX IF NOT EXISTS files_root_key ON files(root_key);
            CREATE TABLE IF NOT EXISTS paragraphs (
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                heading TEXT NOT NULL,
                text TEXT NOT NULL,
                normalized_heading TEXT NOT NULL,
                normalized_text TEXT NOT NULL,
                line_start INTEGER NOT NULL,
                line_end INTEGER NOT NULL,
                PRIMARY KEY (file_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS index_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                """
            )
        except BaseException:
            self._connection.close()
            raise

    def __enter__(self) -> ParagraphIndex:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    @staticmethod
    def _root_key(root: Root, resolved_path: Path) -> str:
        return root.alias + "\x1f" + os.path.normcase(str(resolved_path))

    def _walk(
        self, root: Path, diagnostics: _Diagnostics, protected: list[Path], skipped: list[int],
        excludes: tuple[str, ...], excluded: list[int]
    ) -> Iterator[tuple[Path, os.stat_result]]:
        pending = [root]
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    # Sorting ensures warnings and file handling remain reproducible.
                    ordered = sorted(entries, key=lambda entry: entry.name.casefold())
            except OSError as exc:
                protected.append(directory)
                diagnostics.add(directory, "scan_failed", "无法读取子目录，保留已有索引")
                continue
            for entry in ordered:
                path = Path(entry.path)
                if is_excluded(path, root, excludes):
                    excluded[0] += 1
                    continue
                supported = path.suffix.casefold() in _SUFFIXES
                try:
                    metadata = entry.stat(follow_symlinks=False)
                    if entry.is_symlink() or _is_reparse_point(metadata):
                        if supported:
                            skipped[0] += 1
                            diagnostics.add(path, "link", "跳过符号链接或目录联接文件")
                        continue
                    resolved = path.resolve()
                    if not resolved.is_relative_to(root):
                        if supported:
                            skipped[0] += 1
                        diagnostics.add(path, "outside_root", "路径超出配置目录")
                        continue
                    if stat.S_ISDIR(metadata.st_mode):
                        pending.append(resolved)
                    elif supported and stat.S_ISREG(metadata.st_mode):
                        yield resolved, metadata
                except OSError as exc:
                    if supported:
                        skipped[0] += 1
                    protected.append(path)
                    diagnostics.add(path, "unreadable", "无法读取文件或目录状态")

    @staticmethod
    def _is_protected(path: Path, protected: list[Path]) -> bool:
        return any(path == parent or path.is_relative_to(parent) for parent in protected)

    def _recent_report(self, min_interval: float, signature: str) -> dict | None:
        row = self._connection.execute("SELECT value FROM index_metadata WHERE key='last_sync'").fetchone()
        try:
            report = json.loads(row[0]) if row else None
            checked = report.get("checked_at") if isinstance(report, dict) else None
            if (
                isinstance(report, dict) and report.get("report_version") == 2
                and report.get("config_signature") == signature
                and isinstance(checked, (int, float)) and not isinstance(checked, bool)
                and math.isfinite(checked) and 0 <= time.time() - checked < min_interval
            ):
                return dict(report, cache_reused=True)
        except (ValueError, TypeError):
            pass
        return None

    def sync(self, *, force: bool = False, min_interval: float = 0,
             progress: Callable[[dict], None] | None = None) -> dict:
        """Refresh immediately by default; queries may reuse a recent persisted scan.

        Reuse never advances the last scan timestamp. Config changes, force and
        invalid/older reports bypass reuse; waiting writers check again under
        the transaction lock so simultaneous launcher processes share one scan.
        """
        started = time.monotonic()
        timings = _Timings()
        signature = index_signature(self.roots, self.max_file_bytes)
        processed = 0
        diagnostics = _Diagnostics()
        skipped = [0]
        excluded = [0]
        updated = 0
        deleted = 0

        def notify(stage):
            if progress:
                progress({"stage": stage, "processed": processed, "updated": updated,
                          "skipped": skipped[0], "elapsed_ms": round((time.monotonic() - started) * 1000)})

        if not force and min_interval > 0:
            recent = self._recent_report(min_interval, signature)
            if recent:
                notify("cached")
                return recent
        configured: list[tuple[Root, Path, str]] = []
        with timings.measure("scan"):
            for root in self.roots:
                try:
                    resolved = root.path.expanduser().resolve()
                    configured.append((root, resolved, self._root_key(root, resolved)))
                except (OSError, RuntimeError) as exc:
                    diagnostics.add(root.path, "resolve_failed", "无法解析资料目录")

        connection = self._connection
        def execute(sql, parameters=()):
            with timings.measure("database"):
                return connection.execute(sql, parameters)

        notify("waiting")
        with timings.measure("lock"):
            connection.execute("BEGIN IMMEDIATE")
        try:
            if not force and min_interval > 0:
                recent = self._recent_report(min_interval, signature)
                if recent:
                    notify("cached")
                    connection.execute("COMMIT")
                    return recent
            notify("scan")
            root_keys = {item[2] for item in configured}
            for row in execute("SELECT DISTINCT root_key FROM files").fetchall():
                if row["root_key"] not in root_keys:
                    deleted += execute("DELETE FROM files WHERE root_key = ?", (row["root_key"],)).rowcount
            for root, root_path, root_key in configured:
                old_rows = execute(
                    "SELECT id, path, mtime_ns, size FROM files WHERE root_key = ?", (root_key,)
                ).fetchall()
                old = {row["path"]: row for row in old_rows}
                # New exclusions must remove stale hits even when an ancestor
                # cannot be scanned and its other cached files are protected.
                for path_key, previous in list(old.items()):
                    if is_excluded(Path(path_key), root_path, root.exclude_dirs):
                        deleted += execute("DELETE FROM files WHERE id = ?", (previous["id"],)).rowcount
                        del old[path_key]
                seen: set[str] = set()
                protected: list[Path] = []
                try:
                    with timings.measure("scan"):
                        root_stat = root_path.stat()
                    if not stat.S_ISDIR(root_stat.st_mode):
                        diagnostics.add(root_path, "not_directory", "配置路径不是文件夹")
                        deleted += execute("DELETE FROM files WHERE root_key = ?", (root_key,)).rowcount
                        continue
                except FileNotFoundError:
                    diagnostics.add(root_path, "root_missing", "资料目录不存在")
                    deleted += execute("DELETE FROM files WHERE root_key = ?", (root_key,)).rowcount
                    continue
                except OSError as exc:
                    diagnostics.add(root_path, "unreadable", "无法访问资料目录，保留已有索引")
                    continue
                walker = self._walk(root_path, diagnostics, protected, skipped, root.exclude_dirs, excluded)
                while True:
                    notify("scan")
                    with timings.measure("scan"):
                        entry = next(walker, None)
                    if entry is None:
                        break
                    path, metadata = entry
                    processed += 1
                    path_key = str(path)
                    seen.add(path_key)
                    previous = old.get(path_key)
                    if metadata.st_size > self.max_file_bytes:
                        skipped[0] += 1
                        diagnostics.add(path, "too_large", f"文件超过大小上限（{metadata.st_size} 字节）")
                        deleted += execute(
                            "DELETE FROM files WHERE root_key = ? AND path = ?", (root_key, path_key)
                        ).rowcount
                        continue
                    if not force and previous and previous["mtime_ns"] == metadata.st_mtime_ns and previous["size"] == metadata.st_size:
                        continue
                    try:
                        # Cap the actual read as well: a file can grow after stat.
                        notify("read")
                        with timings.measure("read"):
                            with path.open("rb") as stream:
                                data = stream.read(self.max_file_bytes + 1)
                            after = path.stat()
                        if len(data) > self.max_file_bytes:
                            raise ValueError("文件读取时超过大小上限")
                        if after.st_mtime_ns != metadata.st_mtime_ns or after.st_size != metadata.st_size:
                            raise ValueError("文件在索引过程中发生变化，下次检索会重试")
                        notify("parse")
                        with timings.measure("parse"):
                            paragraphs = _paragraphs(_decode(data), path.suffix.casefold() != ".txt")
                            prepared = [
                                (ordinal, paragraph.heading, paragraph.text,
                                 _normalize(paragraph.heading), _normalize(paragraph.text),
                                 paragraph.line_start, paragraph.line_end)
                                for ordinal, paragraph in enumerate(paragraphs)
                            ]
                    except (OSError, UnicodeError, ValueError) as exc:
                        skipped[0] += 1
                        reason = "encoding" if isinstance(exc, UnicodeError) else "unreadable" if isinstance(exc, OSError) else "content"
                        detail = "无法识别文本编码" if isinstance(exc, UnicodeError) else "无法读取文件" if isinstance(exc, OSError) else str(exc)
                        diagnostics.add(path, reason, detail)
                        deleted += execute(
                            "DELETE FROM files WHERE root_key = ? AND path = ?", (root_key, path_key)
                        ).rowcount
                        continue
                    notify("database")
                    execute(
                        """INSERT INTO files(root_key, root_path, alias, alias_key, path, mtime_ns, size)
                           VALUES (?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(root_key, path) DO UPDATE SET
                               alias = excluded.alias, alias_key = excluded.alias_key,
                               mtime_ns = excluded.mtime_ns, size = excluded.size""",
                        (root_key, str(root_path), root.alias, _normalize(root.alias), path_key, metadata.st_mtime_ns, metadata.st_size),
                    )
                    file_id = execute(
                        "SELECT id FROM files WHERE root_key = ? AND path = ?", (root_key, path_key)
                    ).fetchone()["id"]
                    execute("DELETE FROM paragraphs WHERE file_id = ?", (file_id,))
                    with timings.measure("database"):
                        connection.executemany(
                            """INSERT INTO paragraphs(file_id, ordinal, heading, text,
                                   normalized_heading, normalized_text, line_start, line_end)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            [(file_id, *row) for row in prepared],
                        )
                    updated += 1
                for path_key, previous in old.items():
                    if path_key not in seen and not self._is_protected(Path(path_key), protected):
                        deleted += execute("DELETE FROM files WHERE id = ?", (previous["id"],)).rowcount
            notify("database")
            files = execute("SELECT COUNT(*) FROM files").fetchone()[0]
            paragraphs = execute("SELECT COUNT(*) FROM paragraphs").fetchone()[0]
            duration = time.monotonic() - started
            measured = dict(timings.seconds, other=max(0, duration - sum(timings.seconds.values())))
            report = {"files": files, "paragraphs": paragraphs, "updated": updated, "skipped": skipped[0],
                      "deleted": deleted, "excluded": excluded[0], "warnings": diagnostics.warnings,
                      "issues": diagnostics.issues, "issue_count": diagnostics.count, "reasons": diagnostics.reasons,
                      "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "duration_ms": round(duration * 1000), "rebuilt": force,
                      "timings_ms": {key: round(value * 1000, 2) for key, value in measured.items()},
                      "timing_scope": "sync_before_commit", "processed": processed,
                      "report_version": 2, "checked_at": time.time(), "cache_reused": False,
                      "config_signature": signature}
            connection.execute("INSERT OR REPLACE INTO index_metadata(key, value) VALUES ('last_sync', ?)",
                               (json.dumps(report, ensure_ascii=False),))
            notify("commit")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        notify("done")
        return report

    def scope_stats(self, alias: str | None = None) -> dict[str, int]:
        where, parameters = (" WHERE f.alias_key = ?", (_normalize(alias),)) if alias is not None else ("", ())
        row = self._connection.execute(
            "SELECT COUNT(DISTINCT f.id), COUNT(p.file_id) FROM files f "
            "LEFT JOIN paragraphs p ON p.file_id = f.id" + where, parameters
        ).fetchone()
        return {"files": row[0], "paragraphs": row[1]}

    def search(self, query: str, alias: str | None = None, limit: int = 30,
               *, metrics: dict | None = None) -> list[SearchHit]:
        started = time.perf_counter()
        normalized_query = _normalize(query)
        if not normalized_query or limit < 1:
            if metrics is not None:
                metrics.update(matched=0, returned=0, query_ms=0, ranking_ms=0, result_ms=0, duration_ms=0)
            return []
        tokens = list(dict.fromkeys(normalized_query.split()))
        conditions = ["(instr(p.normalized_text, ?) > 0 OR instr(p.normalized_heading, ?) > 0)" for _ in tokens]
        parameters: list[str] = [token for token in tokens for _ in range(2)]
        if alias is not None:
            conditions.append("f.alias_key = ?")
            parameters.append(_normalize(alias))
        fetching = time.perf_counter()
        rows = self._connection.execute(
            """SELECT f.id AS file_id, f.alias, f.path, p.heading, p.text, p.line_start, p.line_end,
                      p.normalized_heading, p.normalized_text
                 FROM paragraphs AS p JOIN files AS f ON f.id = p.file_id
                WHERE """ + " AND ".join(conditions),
            parameters,
        )
        query_seconds = time.perf_counter() - fetching
        matched = 0
        sort_prefixes = {}

        def candidates():
            nonlocal query_seconds, matched
            while True:
                fetching = time.perf_counter()
                batch = rows.fetchmany(256)
                query_seconds += time.perf_counter() - fetching
                if not batch:
                    break
                for row in batch:
                    matched += 1
                    body, heading = row["normalized_text"], row["normalized_heading"]
                    # Preserve the original score, tie-breakers and stable order.
                    score = (100 if normalized_query in body else 0) + (40 if normalized_query in heading else 0)
                    score += sum(min(body.count(token), 10) * 10 + min(heading.count(token), 5) * 5 for token in tokens)
                    prefix = sort_prefixes.get(row["file_id"])
                    if prefix is None:
                        prefix = (_normalize(row["alias"]), os.path.normcase(row["path"]))
                        sort_prefixes[row["file_id"]] = prefix
                    yield (-score, *prefix, row["line_start"]), row

        # A bounded heap retains only the best candidates. Paths and result
        # objects are created after selection, rather than for every match.
        selected = heapq.nsmallest(limit, candidates(), key=lambda item: item[0])
        ranked = time.perf_counter()
        hits = [SearchHit(row["alias"], Path(row["path"]), row["heading"], row["text"],
                          row["line_start"], row["line_end"], -key[0]) for key, row in selected]
        finished = time.perf_counter()
        if metrics is not None:
            metrics.update(matched=matched, returned=len(hits), query_ms=round(query_seconds * 1000, 2),
                           ranking_ms=round(max(0, ranked - started - query_seconds) * 1000, 2),
                           result_ms=round((finished - ranked) * 1000, 2),
                           duration_ms=round((finished - started) * 1000, 2))
        return hits

    def paragraph_context(self, path: Path, alias: str, line_start: int, line_end: int,
                          text_hash: str) -> dict[str, SearchHit | None]:
        """Read adjacent parsed paragraphs from one consistent SQLite snapshot.

        A digest and line range bind the request to the displayed paragraph. If
        indexing changed it, callers must search again instead of mixing sources.
        """
        connection = self._connection
        connection.execute("BEGIN")
        try:
            selected = connection.execute(
                """SELECT f.id AS file_id, p.ordinal, p.heading, p.text FROM files AS f
                     JOIN paragraphs AS p ON p.file_id=f.id
                     WHERE f.path=? AND f.alias=? AND p.line_start=? AND p.line_end=?""",
                (str(path), alias, line_start, line_end),
            ).fetchone()
            if selected is None or hashlib.sha256((selected["heading"] + "\0" + selected["text"]).encode("utf-8")).hexdigest() != text_hash:
                raise ValueError("该段落已变化或不在当前索引中，请重新检索后查看前后文。")
            ordinal = selected["ordinal"]
            rows = connection.execute(
                """SELECT f.alias, f.path, p.ordinal, p.heading, p.text, p.line_start, p.line_end
                     FROM paragraphs AS p JOIN files AS f ON f.id=p.file_id
                     WHERE p.file_id=? AND p.ordinal BETWEEN ? AND ? ORDER BY p.ordinal""",
                (selected["file_id"], ordinal - 1, ordinal + 1),
            ).fetchall()
            result = {"before": None, "selected": None, "after": None}
            for row in rows:
                name = "before" if row["ordinal"] < ordinal else "after" if row["ordinal"] > ordinal else "selected"
                result[name] = SearchHit(row["alias"], Path(row["path"]), row["heading"], row["text"],
                                         row["line_start"], row["line_end"], 0)
            connection.execute("COMMIT")
            return result
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
