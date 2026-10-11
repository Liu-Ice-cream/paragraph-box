"""Local checks with an explicit, shareable output schema and synthetic data."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile

from .engine import ParagraphIndex, Root
from .plugin import ParagraphPlugin, find_vscode, hit_result
from .settings import load_settings


DEMO_TEXT = "段落匣人工样例\n\n雨夜的车站亮着一盏灯。这是用于运行检查的人工段落。\n"
DEMO_NAME = "人工样例.txt"
DEMO_CITATION = f"{DEMO_TEXT.splitlines()[2]}\n\n出处：{DEMO_NAME} · 第 3 行"
MANUAL_IDS = ("open_file", "paste_excerpt", "flow_entry")


def demo_path(base: Path) -> Path:
    # No path supplied by a request is ever used for this action.
    candidate = base / "samples" / "diagnostics" / DEMO_NAME
    for part in (candidate, *candidate.parents):
        if part == base:
            break
        if part.is_symlink() or getattr(part.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("人工样例不可用，请重新安装插件。")
    if not candidate.resolve().is_relative_to(base.resolve()):
        raise ValueError("人工样例不可用，请重新安装插件。")
    if candidate.stat().st_size > 1024 or candidate.read_text(encoding="utf-8") != DEMO_TEXT:
        raise ValueError("人工样例已变化，请重新安装插件。")
    return candidate.resolve()


def sample_result(base: Path) -> dict:
    demo_path(base)
    # Never use personal roots, their database, or process-wide env overrides.
    with tempfile.TemporaryDirectory(prefix="paragraphbox-demo-") as directory:
        home = Path(directory)
        notes = home / "notes"
        notes.mkdir()
        (notes / DEMO_NAME).write_text(DEMO_TEXT, encoding="utf-8")
        (notes / "章节.md").write_text("# 人工章节\n\n中文检索保留完整段落。\n", encoding="utf-8")
        with ParagraphIndex(home / "index.sqlite3", [Root("样例", notes)], 2_000_000) as index:
            index.sync()
            hits = index.search("雨夜 车站", limit=5)
            headings = index.search("人工章节 中文", limit=5)
        if (len(hits) != 1 or hits[0].text != DEMO_TEXT.splitlines()[2]
                or hits[0].line_start != 3 or hits[0].line_end != 3
                or len(headings) != 1 or headings[0].heading != "人工章节"):
            raise ValueError("人工样例检索失败。")
        result = hit_result(hits[0], "雨夜 车站")
        menus = ParagraphPlugin(base).context_menu(result["ContextData"])
        wire = json.loads(json.dumps({"result": [result]}, ensure_ascii=True))
        copy = menus[2]["JsonRPCAction"]
        if (wire["result"][0]["ContextData"]["line_start"] != 3
                or copy["method"] != "Flow.Launcher.CopyToClipboard"
                or copy["parameters"] != [f"{hits[0].text}\n\n出处：{hits[0].path} · 第 3 行", False, True]):
            raise ValueError("人工样例协议检查失败。")
    # UI data is synthetic and carries no temporary path or real source context.
    return {"query": "雨夜 车站", "text": DEMO_TEXT.splitlines()[2],
            "source": f"{DEMO_NAME} · 第 3 行", "citation": DEMO_CITATION}


def _error_kind(exc: Exception) -> str:
    for kind in (PermissionError, FileNotFoundError, sqlite3.Error, UnicodeError, OSError, ValueError):
        if isinstance(exc, kind):
            return kind.__name__
    return "RuntimeError"


def run_checks(base: Path, *, active_version: str | None = None) -> dict:
    """Check availability only; never index or open configured documents."""
    checks = []

    def add(identifier, label, status, code, hint="", error_type=None):
        checks.append({"id": identifier, "label": label, "status": status,
                       "code": code, "hint": hint, **({"error_type": error_type} if error_type else {})})

    def check(identifier, label, callback, code, hint):
        try:
            callback()
        except Exception as exc:
            add(identifier, label, "fail", code, hint, _error_kind(exc))
        else:
            add(identifier, label, "pass", "OK")

    version = "unknown"
    try:
        raw = json.loads((base / "plugin.json").read_text(encoding="utf-8-sig"))["Version"]
        if not isinstance(raw, str) or not re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", raw):
            raise ValueError()
        version = raw
        if active_version is not None and active_version != version:
            raise ValueError()
    except Exception as exc:
        add("manifest", "插件版本", "fail", "MANIFEST_INVALID_OR_STALE", "重新安装插件并重新打开设置页。", _error_kind(exc))
    else:
        add("manifest", "插件版本", "pass", "OK")
    add("python", "当前 Python ≥ 3.10", "pass" if sys.version_info >= (3, 10) else "fail",
        "OK" if sys.version_info >= (3, 10) else "PYTHON_TOO_OLD", "Flow 中也需配置 Python 3.10 或更高版本。")

    def probe_cache():
        home = Path(os.environ.get("PARAGRAPH_SEARCH_HOME", str(base / ".cache"))).expanduser().resolve()
        home.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="doctor-", dir=home) as directory:
            connection = sqlite3.connect(Path(directory) / "probe.sqlite3")
            try:
                connection.execute("CREATE TABLE probe (value INTEGER)")
                connection.execute("INSERT INTO probe VALUES (1)")
                connection.commit()
            finally:
                connection.close()
    check("cache", "缓存写入与 SQLite", probe_cache, "CACHE_UNWRITABLE", "检查插件缓存目录的写入权限和磁盘空间；确认未被安全软件拦截。")

    try:
        settings = load_settings(base)
    except Exception as exc:
        add("config", "资料配置", "fail", "CONFIG_INVALID", "用页尾的“编辑配置文件”修复 JSON 和目录设置，再重新检查。", _error_kind(exc))
        add("roots", "资料目录", "pending", "CONFIG_REQUIRED", "配置修复后才能检查资料目录。")
    else:
        add("config", "资料配置", "pass", "OK")
        for number, root in enumerate(settings.roots, 1):
            def probe_root(path=root.path):
                if not stat.S_ISDIR(path.stat().st_mode):
                    raise NotADirectoryError()
                with os.scandir(path):
                    pass
            check(f"root_{number}", f"资料目录 {number}", probe_root, "ROOT_UNAVAILABLE", f"在资料目录中检查第 {number} 项的路径与访问权限；连接磁盘后再试。")
        if settings.editor == "vscode":
            try:
                found = find_vscode() is not None
            except Exception:
                found = False
            add("editor", "VS Code 打开方式", "pass" if found else "fail", "OK" if found else "EDITOR_UNAVAILABLE",
                "安装 VS Code 后重新打开设置，或在检索偏好改用系统默认程序。")
        else:
            add("editor", "系统默认打开方式", "pending", "MANUAL_REQUIRED", "需实际打开人工样例，确认本机的文本文件关联可用。")

    check("sample", "人工样例检索与 JSON 格式", lambda: sample_result(base), "SAMPLE_FAILED", "重新安装完整插件；若仍失败，复制诊断摘要报告问题。")
    status = "fail" if any(item["status"] == "fail" for item in checks) else "pass"
    return {"schema": 1, "version": version, "python": sys.version.split()[0],
            "automatic_status": status, "checks": checks}


def diagnostic_summary(report: dict) -> str:
    # A fresh projection excludes paths, labels, advice, raw exceptions and tokens.
    document = {"schema": 1, "plugin_version": report["version"], "python": report["python"],
                "automatic_status": report["automatic_status"],
                "checks": [{key: item[key] for key in ("id", "status", "code", "error_type") if key in item}
                           for item in report["checks"]],
                "manual": {identifier: "pending" for identifier in MANUAL_IDS}}
    return json.dumps(document, ensure_ascii=False, indent=2)
