"""Flow Launcher result formatting and actions; stdout is reserved for JSON RPC."""

from __future__ import annotations

import os
import hashlib
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

from .engine import ParagraphIndex, SearchHit, is_excluded
from .settings import load_settings
from .matching import normalize, match_details


ICON = "Images/app.svg"


def find_vscode() -> Path | None:
    """Find a real VS Code executable; never execute a shell command template."""
    candidates = []
    for variable, suffix in (("LOCALAPPDATA", "Programs/Microsoft VS Code/Code.exe"),
                             ("ProgramFiles", "Microsoft VS Code/Code.exe"),
                             ("ProgramFiles(x86)", "Microsoft VS Code/Code.exe")):
        if os.environ.get(variable):
            candidates.append(Path(os.environ[variable]) / suffix)
    for name in ("Code.exe", "code.cmd"):
        located = shutil.which(name)
        if located:
            path = Path(located)
            candidates.append(path if path.suffix.lower() == ".exe" else path.parent.parent / "Code.exe")
    return next((path.resolve() for path in candidates if path.is_file()), None)


def location_data(data: dict) -> dict:
    return {key: data[key] for key in ("path", "alias", "line_start", "line_end")} | {
        "text_hash": hashlib.sha256((data.get("heading", "") + "\0" + data["text"]).encode("utf-8")).hexdigest()}


def open_text_file(path: Path) -> None:
    """Use the user's chosen app, with Notepad for unassociated text formats."""
    try:
        os.startfile(str(path))
    except OSError as exc:
        code = getattr(exc, "winerror", None)
        # ShellExecute may report ERROR_NO_ASSOCIATION or CO_E_APPNOTFOUND.
        # https://learn.microsoft.com/en-us/windows/win32/com/com-error-codes-1
        if code != 1155 and (not isinstance(code, int) or code & 0xFFFFFFFF != 0x800401F5):
            raise
        subprocess.Popen(
            ["notepad.exe", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )


def action(method: str, *parameters: Any) -> dict[str, Any]:
    return {"method": method, "parameters": list(parameters)}


def message(title: str, subtitle: str, *, method: str = "open_config") -> dict[str, Any]:
    return {
        "Title": title,
        "SubTitle": subtitle,
        "IcoPath": ICON,
        "JsonRPCAction": action(method),
    }


def excerpt(text: str, keywords: str, width: int = 110) -> str:
    """Keep the first match visible even when it is deep inside a paragraph."""
    flat = " ".join(text.split())
    if len(flat) <= width:
        return flat
    normalized, positions = "", []
    for index, char in enumerate(flat):
        part = unicodedata.normalize("NFKC", char).casefold()
        normalized += part
        positions.extend([index] * len(part))
    tokens = unicodedata.normalize("NFKC", keywords).casefold().split()
    matches = [normalized.find(token) for token in tokens]
    found = [p for p in matches if p >= 0]
    anchor = positions[min(found)] if found else 0
    start = min(max(0, anchor - width // 3), max(0, len(flat) - width))
    return ("…" if start else "") + flat[start : start + width] + ("…" if start + width < len(flat) else "")


def context_data(hit: SearchHit) -> dict[str, Any]:
    return {
        "alias": hit.alias,
        "path": str(hit.path),
        "heading": hit.heading,
        "text": hit.text,
        "line_start": hit.line_start,
        "line_end": hit.line_end,
    }


def hit_result(hit: SearchHit, keywords: str, *, editor: str = "default") -> dict[str, Any]:
    section = f" · {hit.heading}" if hit.heading else ""
    line = str(hit.line_start) if hit.line_start == hit.line_end else f"{hit.line_start}–{hit.line_end}"
    snippet = excerpt(hit.text, keywords)
    context = context_data(hit)
    context.update(match_details(hit.text, hit.heading, snippet, keywords))
    return {
        "Title": snippet,
        "SubTitle": f"[{hit.alias}] {hit.path.name}{section} · 第 {line} 行",
        "IcoPath": ICON,
        "Score": hit.score,
        "ContextData": context,
        "JsonRPCAction": action("open_location", location_data(context)) if editor == "vscode" else action("open_file", str(hit.path)),
    }


class ParagraphPlugin:
    def __init__(self, base: Path):
        self.base = base

    def query(self, query: str, *, progress=None, metrics: dict | None = None) -> list[dict[str, Any]]:
        started = time.perf_counter()
        if not isinstance(query, str):
            raise ValueError("query 参数必须是字符串。")
        query = unicodedata.normalize("NFKC", query).strip()
        if not query:
            return [
                message("段落匣 · 让资料有处可寻", "输入 sn 关键词，查找笔记、素材和工作记录。"),
                message("按目录检索 · sn @写作 雨夜", "目录别名使用 @ 前缀；回车编辑配置。"),
                message("配置检索目录", "打开可视化设置页，添加目录、调整检索数量。"),
            ]
        settings = load_settings(self.base)
        alias = None
        if query.startswith("@"):
            parts = query.split(maxsplit=1)
            selector, query = parts[0], parts[1] if len(parts) > 1 else ""
            requested = selector[1:]
            alias = next((root.alias for root in settings.roots if normalize(root.alias) == normalize(requested)), None)
            if alias is None:
                available = "、".join(root.alias for root in settings.roots)
                return [message(f"未知目录别名：{requested}", f"可用别名：{available}；回车编辑配置。")]
            query = query.strip()
        if not query:
            return [message(f"请输入关键词 · @{alias}", "例如 sn @写作 雨夜；回车编辑配置。")]
        index = ParagraphIndex(settings.cache_path, settings.roots, settings.max_file_bytes)
        prepared = time.perf_counter()
        search_metrics = {}
        try:
            stats = index.sync(min_interval=2, progress=progress)
            synced = time.perf_counter()
            hits = index.search(query, alias=alias, limit=settings.max_results, metrics=search_metrics)
            searched = time.perf_counter()
        finally:
            index.close()
        closed = time.perf_counter()
        formatting = time.perf_counter()
        results = [hit_result(hit, query, editor=settings.editor) for hit in hits]
        if not results:
            results.append(message("没有找到匹配段落", f"已索引 {stats['files']} 个文件、{stats['paragraphs']} 个段落；试试更短的关键词。"))
        warnings = stats.get("warnings", [])
        if warnings:
            results.append(message("部分资料未能索引", "；".join(warnings)[:240]))
        if metrics is not None:
            finished = time.perf_counter()
            metrics.update(prepare_ms=round((prepared - started) * 1000, 2),
                           index_ms=round((synced - prepared) * 1000, 2),
                           index_reused=bool(stats.get("cache_reused")), search=search_metrics,
                           close_ms=round((closed - searched) * 1000, 2),
                           format_ms=round((finished - formatting) * 1000, 2),
                           duration_ms=round((finished - started) * 1000, 2))
        return results

    def surrounding(self, data: dict) -> dict:
        if not isinstance(data, dict) or not isinstance(data.get("alias"), str):
            raise ValueError("请提供有效的段落出处。")
        start, end, digest = data.get("line_start"), data.get("line_end"), data.get("text_hash")
        if (type(start) is not int or type(end) is not int or not 1 <= start <= end <= 2_147_483_647
                or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)):
            raise ValueError("段落行号或内容标识无效，请重新检索。")
        candidate, settings = self._document_path(data.get("path"))
        root = next((root for root in settings.roots if root.alias == data["alias"]
                     and candidate.is_relative_to(root.path.resolve())
                     and not is_excluded(candidate, root.path.resolve(), root.exclude_dirs)), None)
        if root is None:
            raise ValueError("该段落不在当前配置的资料目录中。")
        with ParagraphIndex(settings.cache_path, settings.roots, settings.max_file_bytes) as index:
            # Context is requested explicitly; check changes immediately so old
            # search results cannot silently acquire another paragraph's context.
            index.sync()
            nearby = index.paragraph_context(candidate, root.alias, start, end, digest)
        return {name: context_data(hit) if hit is not None else None for name, hit in nearby.items()}

    def context_menu(self, data: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not isinstance(data, dict) or not {"path", "text", "line_start", "line_end"} <= data.keys():
            return []
        path, text = data["path"], data["text"]
        if not isinstance(path, str) or not isinstance(text, str):
            raise ValueError("段落上下文无效。")
        heading = str(data.get("heading", ""))
        start, end = data["line_start"], data["line_end"]
        line = str(start) if start == end else f"{start}–{end}"
        source = f"出处：{path}" + (f" · {heading}" if heading else "") + f" · 第 {line} 行"
        cited = f"{text}\n\n{source}"
        # The classic bridge reflects exact parameter types: pass all three args.
        def copy_result(title: str, value: str, subtitle: str) -> dict[str, Any]:
            return {
                "Title": title,
                "SubTitle": subtitle,
                "IcoPath": ICON,
                "JsonRPCAction": action("Flow.Launcher.CopyToClipboard", value, False, True),
            }
        results = [
            copy_result("复制段落", text, "保留原段落的换行。"),
            copy_result("复制文件路径", path, path),
            copy_result("复制带出处的摘录", cited, f"{Path(path).name} · 第 {line} 行"),
            {"Title": "打开原文件", "SubTitle": path, "IcoPath": ICON, "JsonRPCAction": action("open_file", path)},
        ]
        try:
            editor = load_settings(self.base).editor
        except (OSError, ValueError):
            # Existing excerpts remain copyable while configuration is edited.
            editor = "default"
        if editor == "vscode" and "alias" in data:
            results.append({"Title": "在 VS Code 中定位到命中行", "SubTitle": f"{Path(path).name} · 第 {start} 行",
                            "IcoPath": ICON, "JsonRPCAction": action("open_location", location_data(data))})
        return results

    def open_location(self, data: dict) -> dict:
        # Revalidate both the source identity and the current directory rules
        # before using a line number from an earlier search result.
        selected = self.surrounding(data)["selected"]
        settings = load_settings(self.base)
        candidate, _ = self._document_path(selected["path"])
        if settings.editor == "vscode":
            executable = find_vscode()
            if executable is None:
                raise ValueError("未找到 VS Code，请在检索偏好中改用系统默认程序，或安装后重新打开设置。")
            subprocess.Popen([str(executable), "--reuse-window", "--goto", f"{candidate}:{selected['line_start']}:1"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True)
            return {"editor": "vscode", "located": True}
        open_text_file(candidate)
        return {"editor": "default", "located": False}

    def open_file(self, path: str) -> None:
        candidate, _ = self._document_path(path)
        open_text_file(candidate)

    def _document_path(self, path: str):
        if not isinstance(path, str):
            raise ValueError("文件路径必须是字符串。")
        candidate = Path(path).resolve()
        settings = load_settings(self.base)
        if candidate.suffix.casefold() not in {".md", ".markdown", ".txt"}:
            raise ValueError("只能打开配置目录中的 Markdown/TXT 文件。")
        if not any(candidate.is_relative_to(root.path.resolve()) and not is_excluded(candidate, root.path.resolve(), root.exclude_dirs) for root in settings.roots):
            raise ValueError("文件不在配置的检索目录中。")
        if not candidate.is_file():
            raise FileNotFoundError(f"原文件已移走或删除：{candidate}")
        return candidate, settings

    def open_config(self) -> None:
        # A separate process keeps the HTTP server alive after Flow's action exits.
        subprocess.Popen(
            [sys.executable, str(self.base / "settings_ui.py")],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
