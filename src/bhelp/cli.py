from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .cache import CacheError, TranslationCache
from .capture import CaptureFailure, CaptureResult, capture_command, capture_man, capture_stdin
from .config import (
    ConfigError,
    get_config_path,
    initialize_config,
    load_config,
    redacted_config,
    set_api_key,
)
from .integration import (
    IntegrationError,
    git_status,
    install_git_integration,
    install_man_integration,
    man_status,
    remove_git_integration,
    remove_man_integration,
)
from .provider import ChatCompletionsProvider, ProviderError, resolve_provider_settings
from .render import display_output, render_bilingual, use_color
from .translate import TranslationResult, language_name, segment_text, translate_text


EXIT_TRANSLATION_FAILED = 3


def _add_common_options(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    default: Any = argparse.SUPPRESS if suppress_defaults else None
    false_default: Any = argparse.SUPPRESS if suppress_defaults else False
    parser.add_argument("-f", "--from", dest="source_language", default=default, help="源语言，默认 auto")
    parser.add_argument("-t", "--to", dest="target_language", default=default, help="目标语言，如 zh-CN、ja、en")
    parser.add_argument("-p", "--provider", choices=("deepseek", "openai"), default=default)
    parser.add_argument("--model", default=default, help="临时覆盖模型")
    parser.add_argument("--base-url", default=default, help="临时覆盖 API base URL")
    parser.add_argument("--config", dest="config_path", default=default, help="配置文件路径")
    parser.add_argument("--pager", choices=("auto", "always", "never"), default=default)
    parser.add_argument("--color", choices=("auto", "always", "never"), default=default)
    parser.add_argument("--no-cache", action="store_true", default=false_default)
    parser.add_argument("--refresh", action="store_true", default=false_default, help="忽略现有缓存并更新")
    parser.add_argument("--no-network", action="store_true", default=false_default, help="仅显示原文，不调用 API")
    parser.add_argument("--strict", action="store_true", default=false_default, help="翻译失败时返回状态码 3")
    parser.add_argument("--quiet", action="store_true", default=false_default, help="隐藏翻译状态")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bhelp",
        description="把 Linux 命令帮助显示为“原文在上、译文在下”的双语文档。",
    )
    _add_common_options(parser)
    parser.add_argument("--version", action="version", version=f"bhelp {__version__}")
    subparsers = parser.add_subparsers(dest="command_name")

    run_parser = subparsers.add_parser("run", help="执行并翻译一个帮助命令")
    _add_common_options(run_parser, suppress_defaults=True)
    run_parser.add_argument("command", nargs=argparse.REMAINDER, help="建议放在 -- 之后")

    man_parser = subparsers.add_parser("man", help="读取并翻译 man 页面")
    _add_common_options(man_parser, suppress_defaults=True)
    man_parser.add_argument("pages", nargs="+", help="man 页面名")

    pipe_parser = subparsers.add_parser("pipe", help="翻译标准输入")
    _add_common_options(pipe_parser, suppress_defaults=True)

    pager_parser = subparsers.add_parser("pager", help="作为 MANPAGER 提供交互式懒加载翻译")
    _add_common_options(pager_parser, suppress_defaults=True)
    pager_parser.add_argument("files", nargs="*", help="由 man 传入的可选格式化页面文件")

    config_parser = subparsers.add_parser("config", help="管理用户配置")
    config_parser.add_argument("--config", dest="config_path", default=argparse.SUPPRESS)
    config_actions = config_parser.add_subparsers(dest="config_action", required=True)
    init_parser = config_actions.add_parser("init", help="创建默认配置")
    init_parser.add_argument("--force", action="store_true")
    config_actions.add_parser("path", help="显示配置路径")
    config_actions.add_parser("show", help="脱敏显示配置")
    key_parser = config_actions.add_parser("set-key", help="安全保存 API key")
    key_parser.add_argument("provider", nargs="?", choices=("deepseek", "openai"), default="deepseek")
    key_parser.add_argument("--stdin", action="store_true", help="从标准输入读取；适合自动安装")

    doctor_parser = subparsers.add_parser("doctor", help="验证认证、模型和短翻译")
    _add_common_options(doctor_parser, suppress_defaults=True)

    integration_parser = subparsers.add_parser("integrate", help="管理原生命令集成")
    integration_parser.add_argument("--config", dest="config_path", default=argparse.SUPPRESS)
    integration_targets = integration_parser.add_subparsers(dest="integration_target", required=True)
    git_parser = integration_targets.add_parser("git", help="让 git help 直接调用 bhelp")
    git_parser.add_argument("action", choices=("install", "status", "remove"))
    system_man_parser = integration_targets.add_parser(
        "man", help="让系统 man 通过 MANPAGER 直接调用 bhelp"
    )
    system_man_parser.add_argument("action", choices=("install", "status", "remove"))

    cache_parser = subparsers.add_parser("cache", help="查看或清理翻译缓存")
    cache_parser.add_argument("action", choices=("stats", "clear"))
    return parser


def _format_bytes(value: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value} B"
        value /= 1024
    return f"{value:.1f} GiB"


def _config_path_from_args(args: argparse.Namespace) -> str | None:
    return getattr(args, "config_path", None)


def _handle_config(args: argparse.Namespace) -> int:
    path = _config_path_from_args(args)
    if args.config_action == "path":
        print(get_config_path(path))
        return 0
    if args.config_action == "init":
        created = initialize_config(path, args.force)
        print(f"已创建配置：{created}")
        return 0
    if args.config_action == "show":
        print(json.dumps(redacted_config(load_config(path)), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.config_action == "set-key":
        if args.stdin:
            key = sys.stdin.readline().strip()
        else:
            key = getpass.getpass(f"{args.provider} API key（输入不会显示）: ")
        saved = set_api_key(args.provider, key, path)
        print(f"已保存 {args.provider} API key：{saved}（密钥未显示）")
        return 0
    return 2


def _handle_cache(args: argparse.Namespace) -> int:
    cache = TranslationCache()
    if args.action == "stats":
        count, size = cache.stats()
        print(f"缓存：{count} 个条目，{_format_bytes(size)}，目录 {cache.root}")
    else:
        count, size = cache.clear()
        print(f"已清理：{count} 个条目，{_format_bytes(size)}")
    return 0


def _handle_integration(args: argparse.Namespace) -> int:
    path = _config_path_from_args(args)
    if args.integration_target == "man":
        if args.action == "install":
            executable = _find_bhelp_executable()
            if not executable:
                raise IntegrationError(
                    "cannot locate an installed bhelp executable; run scripts/install.sh first"
                )
            status = install_man_integration(executable, path)
            print("man 集成已安装；重新打开 Bash 后可直接运行：man git")
            if status.migrated_legacy_git:
                print("已恢复旧版 bhelp Git viewer；git help 现在也会经过统一 MANPAGER。")
        elif args.action == "remove":
            status = remove_man_integration(path)
            print("man 集成已移除；原有 MANPAGER 设置会在新 Bash 中重新生效。")
        else:
            status = man_status(path)
        print(
            json.dumps(
                {
                    "installed": status.installed,
                    "modified": status.modified,
                    "shell_rc": status.rc_path,
                    "MANPAGER": status.command,
                    "backup": status.backup_path,
                    "migrated_legacy_git": status.migrated_legacy_git,
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.integration_target != "git":
        return 2
    if args.action == "install":
        executable = _find_bhelp_executable()
        viewer_command = f"{shlex.quote(executable)} man" if executable else "bhelp man"
        if path:
            resolved_config = get_config_path(path).resolve()
            viewer_command += f" --config {shlex.quote(str(resolved_config))}"
        status = install_git_integration(path, viewer_command=viewer_command)
        print("Git 集成已安装；现在可直接运行：git help clone")
    elif args.action == "remove":
        status = remove_git_integration(path)
        print("Git 集成已移除，并已恢复安装前的 viewer 设置。")
    else:
        status = git_status()
    print(
        json.dumps(
            {
                "installed": status.installed,
                "help.format": status.help_formats,
                "man.viewer": status.viewers,
                "man.bhelp.cmd": status.command,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _find_bhelp_executable() -> str | None:
    executable = shutil.which("bhelp")
    if executable:
        return str(Path(executable).expanduser().resolve())
    candidate = Path(sys.argv[0]).expanduser().resolve()
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _effective_value(args: argparse.Namespace, config: dict[str, Any], name: str, env_name: str | None = None) -> Any:
    value = getattr(args, name, None)
    if value is not None:
        return value
    if env_name and env_name in os.environ:
        return os.environ[env_name]
    return config[name]


def _capture(args: argparse.Namespace, config: dict[str, Any]) -> CaptureResult:
    timeout = float(config["command_timeout_seconds"])
    maximum = int(config["max_input_chars"])
    if args.command_name == "run":
        command = list(args.command)
        if command and command[0] == "--":
            command = command[1:]
        return capture_command(command, timeout, maximum)
    if args.command_name == "man":
        return capture_man(args.pages, timeout, maximum)
    if args.command_name == "pipe":
        return capture_stdin(maximum)
    raise CaptureFailure("choose one of: run, man, pipe", 2)


def _original_only_result(text: str, batch_chars: int, error: str | None = None) -> TranslationResult:
    pairs = segment_text(text, batch_chars)
    return TranslationResult(pairs, None, 0, 0, [error] if error else [])


def _run_translation(args: argparse.Namespace, config: dict[str, Any]) -> int:
    capture = _capture(args, config)
    if not capture.text:
        return capture.returncode
    source = str(_effective_value(args, config, "source_language", "BHELP_SOURCE_LANGUAGE"))
    target = str(_effective_value(args, config, "target_language", "BHELP_TARGET_LANGUAGE"))
    pager_mode = str(_effective_value(args, config, "pager"))
    color_mode = str(_effective_value(args, config, "color"))
    batch_chars = int(config["batch_chars"])
    no_network = bool(getattr(args, "no_network", False)) or capture.too_large
    provider = None
    settings = None
    if no_network:
        error = "input exceeded max_input_chars; it was not sent to an API" if capture.too_large else None
        result = _original_only_result(capture.text, batch_chars, error)
    else:
        try:
            settings = resolve_provider_settings(
                config,
                provider_override=getattr(args, "provider", None),
                model_override=getattr(args, "model", None),
                base_url_override=getattr(args, "base_url", None),
            )
            provider = ChatCompletionsProvider(settings)
            result = translate_text(
                capture.text,
                source_language=source,
                target_language=target,
                provider=provider,
                batch_chars=batch_chars,
                cache_enabled=bool(config["cache_enabled"]) and not bool(getattr(args, "no_cache", False)),
                refresh_cache=bool(getattr(args, "refresh", False)),
                no_network=False,
            )
        except (ProviderError, ValueError) as exc:
            result = _original_only_result(capture.text, batch_chars, str(exc))

    output = render_bilingual(result.pairs, target, color=use_color(color_mode))
    display_output(output, pager_mode)
    quiet = bool(getattr(args, "quiet", False))
    if result.errors:
        for error in result.errors:
            print(f"bhelp: 翻译警告：{error}", file=sys.stderr)
    elif not quiet and settings and result.api_batches + result.cache_hits:
        detected = result.detected_language or source
        print(
            f"bhelp: {detected} → {language_name(target)}；"
            f"{settings.name}/{settings.model}；缓存命中 {result.cache_hits}；API 批次 {result.api_batches}",
            file=sys.stderr,
        )
    if result.errors and bool(getattr(args, "strict", False)):
        return EXIT_TRANSLATION_FAILED
    return capture.returncode


def _handle_doctor(args: argparse.Namespace, config: dict[str, Any]) -> int:
    if bool(getattr(args, "no_network", False)):
        raise ProviderError("doctor requires network access; remove --no-network to run it")
    settings = resolve_provider_settings(
        config,
        provider_override=getattr(args, "provider", None),
        model_override=getattr(args, "model", None),
        base_url_override=getattr(args, "base_url", None),
    )
    provider = ChatCompletionsProvider(settings)
    models = provider.list_models()
    model_visible = settings.model in models
    target = str(_effective_value(args, config, "target_language", "BHELP_TARGET_LANGUAGE"))
    detected, translation = provider.translate_blocks(
        [("b0000", "Clone a repository into a new directory.")], "English", language_name(target)
    )
    ok = bool(translation.get("b0000"))
    print(
        json.dumps(
            {
                "authenticated": True,
                "provider": settings.name,
                "configured_model": settings.model,
                "model_visible": model_visible,
                "completion_succeeded": ok,
                "detected_source_language": detected,
            },
            ensure_ascii=False,
        )
    )
    return 0 if ok else EXIT_TRANSLATION_FAILED


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.command_name:
        parser.print_help()
        return 2
    try:
        if args.command_name == "config":
            return _handle_config(args)
        if args.command_name == "cache":
            return _handle_cache(args)
        if args.command_name == "integrate":
            return _handle_integration(args)
        if args.command_name == "pager":
            from .pager import run_pager

            try:
                pager_config = load_config(_config_path_from_args(args))
            except ConfigError as exc:
                return run_pager(args, None, startup_warning=f"配置错误：{exc}")
            return run_pager(args, pager_config)
        config = load_config(_config_path_from_args(args))
        if args.command_name == "doctor":
            return _handle_doctor(args, config)
        return _run_translation(args, config)
    except (ConfigError, CacheError, ProviderError, IntegrationError, CaptureFailure) as exc:
        print(f"bhelp: {exc}", file=sys.stderr)
        return getattr(exc, "returncode", 2)
    except KeyboardInterrupt:
        print("\nbhelp: interrupted", file=sys.stderr)
        return 130


def entrypoint() -> None:
    """Console/zipapp entry point that preserves main()'s return code."""
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
