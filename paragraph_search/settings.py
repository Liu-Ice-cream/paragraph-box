"""Small, validated configuration layer for a dependency-free Flow plugin."""

from __future__ import annotations

import json
import os
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path

from .engine import Root
from .matching import normalize


class ConfigValidationError(ValueError):
    def __init__(self, issues: list[dict]):
        self.issues = issues
        super().__init__("；".join(dict.fromkeys(issue["message"] for issue in issues)))


@dataclass(frozen=True)
class Settings:
    roots: list[Root]
    max_results: int
    max_file_bytes: int
    config_path: Path
    cache_path: Path
    editor: str = "default"


def config_path(base: Path) -> Path:
    configured = os.environ.get("PARAGRAPH_SEARCH_CONFIG")
    return Path(configured).expanduser().resolve() if configured else base / "config.json"


def load_settings(base: Path) -> Settings:
    target = config_path(base)
    source = target if target.is_file() else base / "config.example.json"
    if os.environ.get("PARAGRAPH_SEARCH_CONFIG") and not target.is_file():
        raise ValueError(f"配置文件不存在：{target}")
    try:
        data = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取配置 {source.name}：{exc}") from exc
    roots, max_results, max_file_bytes = validate_document(data, source)
    cache_home = Path(os.environ.get("PARAGRAPH_SEARCH_HOME", str(base / ".cache"))).expanduser().resolve()
    return Settings(roots, max_results, max_file_bytes, target, cache_home / "index.sqlite3", data.get("editor", "default"))


def validate_document(data: object, source: Path, *, check_paths: bool = False) -> tuple[list[Root], int, int]:
    if not isinstance(data, dict):
        raise ValueError("配置必须是 JSON 对象。")
    raw_roots = data.get("roots")
    if not isinstance(raw_roots, list) or not raw_roots:
        raise ValueError("roots 必须包含至少一个目录。")
    roots: list[Root] = []
    seen: dict[str, int] = {}
    issues: list[dict] = []
    def issue(index: int | None, field: str, message: str) -> None:
        issues.append({"index": index, "field": field, "message": message})

    for index, entry in enumerate(raw_roots):
        if not isinstance(entry, dict):
            issue(index, "path", "目录项必须包含别名和文件夹路径。")
            continue
        alias, location = entry.get("alias"), entry.get("path")
        raw_excludes = entry.get("exclude_dirs", [])
        excludes = []
        if not isinstance(raw_excludes, list) or len(raw_excludes) > 100:
            issue(index, "exclude_dirs", "排除子目录应为列表，最多 100 项。")
        else:
            for item in raw_excludes:
                value = item.strip().replace("\\", "/").rstrip("/") if isinstance(item, str) else ""
                parts = value.split("/")
                if not value or len(value) > 500 or any(part in {"", ".", ".."} or part.endswith((" ", ".")) for part in parts) or any(c in '<>:"|?*' or ord(c) < 32 for c in value):
                    issue(index, "exclude_dirs", "请填写相对子目录，如 备份 或 导出/旧版；不支持绝对路径、.. 或通配符。")
                    continue
                if value.lower() not in {other.lower() for other in excludes}:
                    excludes.append(value)
        normalized_alias = unicodedata.normalize("NFKC", alias) if isinstance(alias, str) else ""
        if not normalized_alias or any(c.isspace() for c in normalized_alias) or normalized_alias.startswith("@"):
            issue(index, "alias", "目录别名不能为空，不能包含空白或以 @ 开头。")
        else:
            key = normalize(alias)
            if key in seen:
                message = f"目录别名重复：{alias}"
                issue(seen[key], "alias", message)
                issue(index, "alias", message)
            else:
                seen[key] = index
        if not isinstance(location, str) or not location.strip():
            issue(index, "path", "请填写文件夹路径。")
            continue
        try:
            path = Path(os.path.expandvars(location)).expanduser()
            if not path.is_absolute():
                path = source.parent / path
            path = path.resolve()
            if check_paths and not path.is_dir():
                issue(index, "path", "目录不存在或无法访问，请检查文件夹路径。")
            roots.append(Root(alias=alias, path=path, exclude_dirs=tuple(excludes)))
        except (OSError, ValueError, RuntimeError) as exc:
            issue(index, "path", f"无法读取文件夹路径：{exc}")

    def integer(name: str, default: int, minimum: int, maximum: int) -> int:
        value = data.get(name, default)
        if type(value) is not int or not minimum <= value <= maximum:
            issue(None, name, f"{name} 必须是 {minimum} 至 {maximum} 之间的整数。")
            return default
        return value

    max_results = integer("max_results", 30, 1, 100)
    max_file_bytes = integer("max_file_bytes", 2_000_000, 1024, 100_000_000)
    if data.get("editor", "default") not in ("default", "vscode"):
        issue(None, "editor", "打开方式请选择系统默认程序或 VS Code。")
    if issues:
        raise ConfigValidationError(issues)
    return roots, max_results, max_file_bytes


def read_document(base: Path) -> dict:
    target = config_path(base)
    source = target if target.is_file() else base / "config.example.json"
    data = json.loads(source.read_text(encoding="utf-8-sig"))
    validate_document(data, source)
    return data


def save_document(base: Path, data: object) -> dict:
    target = config_path(base)
    roots, max_results, max_bytes = validate_document(data, target, check_paths=True)
    document = {
        "roots": [{"alias": item["alias"], "path": item["path"], **({"exclude_dirs": list(root.exclude_dirs)} if root.exclude_dirs else {})}
                  for item, root in zip(data["roots"], roots)],
        "max_results": max_results,
        "max_file_bytes": max_bytes,
    }
    if "editor" in data:
        document["editor"] = data["editor"]
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return document


def ensure_config(base: Path) -> Path:
    target = config_path(base)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation avoids replacing a config created by another query.
        try:
            with target.open("x", encoding="utf-8") as stream:
                stream.write((base / "config.example.json").read_text(encoding="utf-8"))
        except FileExistsError:
            pass
    return target
