"""Classic per-request Flow Launcher JSON RPC entry point, without an SDK."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from paragraph_search.engine import ParagraphIndex
from paragraph_search.plugin import ParagraphPlugin, message
from paragraph_search.settings import load_settings


BASE = Path(__file__).resolve().parent


def write_log(exc: BaseException) -> None:
    # Flow treats stderr as a failed request, so diagnostics belong in a file.
    try:
        cache = Path(os.environ.get("PARAGRAPH_SEARCH_HOME", str(BASE / ".cache")))
        cache.mkdir(parents=True, exist_ok=True)
        log = cache / "plugin.log"
        if log.exists() and log.stat().st_size > 1_000_000:
            log.replace(cache / "plugin.previous.log")
        with log.open("a", encoding="utf-8") as stream:
            stream.write(f"\n{datetime.now().isoformat(timespec='seconds')}\n")
            traceback.print_exception(exc, file=stream)
    except OSError:
        pass


def dispatch(request: Any, plugin: ParagraphPlugin) -> dict[str, Any] | None:
    if not isinstance(request, dict):
        raise ValueError("请求必须是 JSON 对象。")
    method, params = request.get("method"), request.get("parameters", [])
    if not isinstance(params, list):
        raise ValueError("parameters 必须是数组。")
    if method == "query" and len(params) == 1:
        return {"result": plugin.query(params[0])}
    if method == "context_menu" and len(params) == 1:
        return {"result": plugin.context_menu(params[0])}
    if method == "open_file" and len(params) == 1:
        plugin.open_file(params[0])
        return None
    if method == "open_location" and len(params) == 1:
        plugin.open_location(params[0])
        return None
    if method == "open_config" and not params:
        plugin.open_config()
        return None
    if method == "open_diagnostics" and not params:
        plugin.open_config(diagnostics=True)
        return None
    raise ValueError(f"不支持的方法或参数数量：{method}")


def output(data: Any, *, pretty: bool = False) -> None:
    # ASCII escapes are safe on every console encoding; Flow decodes JSON text.
    print(json.dumps(data, ensure_ascii=True, indent=2 if pretty else None))


def main() -> int:
    plugin = ParagraphPlugin(BASE)
    if len(sys.argv) >= 2 and not sys.argv[1].startswith("--"):
        request: Any = None
        try:
            request = json.loads(sys.argv[1])
            result = dispatch(request, plugin)
            if result is not None:
                output(result)
        except Exception as exc:
            write_log(exc)
            method = request.get("method") if isinstance(request, dict) else None
            if method in {None, "query", "context_menu"}:
                output({"result": [message("段落搜索暂时无法运行", f"{exc}；回车编辑配置。") ]})
            else:
                output({"method": "Flow.Launcher.ShowMsgError", "parameters": ["段落搜索操作失败", str(exc)]})
            # Action output must be empty unless invoking a Flow public API.
        return 0

    parser = argparse.ArgumentParser(description="Flow Launcher 本地段落搜索原型")
    commands = parser.add_mutually_exclusive_group(required=True)
    commands.add_argument("--query", metavar="关键词", help="模拟 Flow 查询，不打开文件")
    commands.add_argument("--index", action="store_true", help="更新磁盘索引并报告统计")
    commands.add_argument("--check", action="store_true", help="检查配置和 Python 环境")
    commands.add_argument("--doctor", action="store_true", help="运行脱敏检查；不扫描真实资料")
    args = parser.parse_args()
    if args.doctor:
        from paragraph_search.diagnostics import diagnostic_summary, run_checks
        report = run_checks(BASE)
        print(diagnostic_summary(report))
        return 1 if report["automatic_status"] == "fail" else 0
    try:
        if args.query is not None:
            output({"result": plugin.query(args.query)}, pretty=True)
        elif args.check:
            settings = load_settings(BASE)
            output({"python": sys.version.split()[0], "config": str(settings.config_path),
                    "using_samples": not settings.config_path.exists(),
                    "cache": str(settings.cache_path),
                    "roots": [{"alias": root.alias, "path": str(root.path), "exists": root.path.is_dir()} for root in settings.roots]}, pretty=True)
        else:
            settings = load_settings(BASE)
            index = ParagraphIndex(settings.cache_path, settings.roots, settings.max_file_bytes)
            try:
                output(index.sync(), pretty=True)
            finally:
                index.close()
        return 0
    except Exception as exc:
        write_log(exc)
        output({"error": str(exc)}, pretty=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
