# -*- coding: utf-8 -*-
"""配置加载：优先 config.toml，无配置时回退到旧 .env 单机器人模式。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

BASE_DIR = Path(__file__).resolve().parent.parent  # 项目根目录（src/ 的上一级）


class ConfigError(Exception):
    """配置缺失或格式错误"""


@dataclass(frozen=True)
class Persona:
    display_name: str
    welcome: str
    menu: str
    reset: str
    max_context: int = 10


@dataclass(frozen=True)
class MaxKBConfig:
    base_url: str
    auth_mode: str = "share_token"
    access_token: str = ""
    api_key: str = ""
    application_id: str = ""


@dataclass(frozen=True)
class BotConfig:
    name: str
    wecom_bot_id: str
    wecom_secret: str
    maxkb: MaxKBConfig
    persona: Persona
    state_file: Path


@dataclass(frozen=True)
class ServerConfig:
    state_dir: Path
    log_level: str = "INFO"
    log_file: Path | None = None


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig
    bot: BotConfig
    source_path: Path | None = None


GENERIC_WELCOME = (
    "你好，我是 {name}，直接发问题就行，也支持语音提问。"
    "发送「重置」可清除上下文重新开始。"
)

GENERIC_MENU = (
    "**{name}使用说明**\n"
    "> 直接发送问题即可，支持语音/文字、多轮对话。\n"
    ">\n"
    "> **可用指令：**\n"
    "> `答题` — 进入答题模式，可分多条语音/文字作答，说完发「回答完毕」一次性提交\n"
    "> `重置` — 清除对话上下文，重新开始\n"
    "> `菜单` — 显示本说明"
)

GENERIC_RESET = (
    "**{name}** · 上下文已重置\n"
    "> 支持语音和文字提问，可以继续问我。\n"
    ">\n"
    "> **上下文：** 0 / {max_context} 轮，聊满后可发「重置」清空\n"
    "> **指令：** 发「菜单」查看全部用法"
)

# 旧 .env 模式保持现有 Doris 文案，避免升级后行为突变
LEGACY_PERSONA = Persona(
    display_name="Doris 小助手",
    welcome=(
        "你好，我是 Doris 小助手，直接发问题就行，也支持语音提问。"
        "发送「重置」可清除上下文重新开始。"
    ),
    menu=(
        "**Doris 小助手使用说明**\n"
        "> 直接发送 Doris / SQL / 数仓相关问题即可，支持语音/文字、多轮对话。\n"
        ">\n"
        "> **可用指令：**\n"
        "> `答题` — 进入答题模式，可分多条语音/文字作答，说完发「回答完毕」一次性提交\n"
        "> `重置` — 清除对话上下文，重新开始\n"
        "> `菜单` — 显示本说明"
    ),
    reset=(
        "**Doris 知识小助手** · 上下文已重置\n"
        "> 我是基于《Doris 原理与企业实战调优》知识库的答疑助手，支持语音和文字提问。\n"
        ">\n"
        "> **可以问我：**\n"
        "> · 建表设计、分区分桶、数据模型选型\n"
        "> · SQL 语法速查与慢查询调优\n"
        "> · 架构原理、Compaction、物化视图等疑难排查\n"
        "> · 面试题模拟（发「答题」可分多段语音作答，说完发「回答完毕」）\n"
        ">\n"
        "> **上下文：** 0 / {max_context} 轮，聊满后可发「重置」清空\n"
        "> **指令：** 发「菜单」查看全部用法"
    ),
    max_context=10,
)

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_NAME_PATTERN = re.compile(r"[a-z0-9_-]{1,32}")
_VALID_AUTH_MODES = {"share_token", "api_key"}


def load_env(base_dir: Path | None = None) -> None:
    """把 config/.env 读入环境变量；真实环境变量优先。"""
    base = Path(base_dir) if base_dir else BASE_DIR
    env_file = base / "config" / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def read_env_value(key: str, base_dir: Path | None = None) -> str:
    load_env(base_dir)
    return os.environ.get(key, "")


def render(text: str, persona: Persona) -> str:
    """替换文案占位符；不用 str.format，避免文案里的其它花括号报错。"""
    return (
        text.replace("{name}", persona.display_name)
        .replace("{max_context}", str(persona.max_context))
    )


def _expand(value, base_dir: Path, path: str = ""):
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            key = match.group(1)
            resolved = read_env_value(key, base_dir)
            if not resolved:
                raise ConfigError(f"配置项 {path or '(root)'} 引用的环境变量 {key} 未设置")
            return resolved

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {
            k: _expand(v, base_dir, f"{path}.{k}" if path else str(k))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_expand(v, base_dir, f"{path}[{i}]") for i, v in enumerate(value)]
    return value


def _require(raw: dict, key: str, where: str) -> str:
    value = str(raw.get(key, "")).strip()
    if not value:
        raise ConfigError(f"缺少配置项 {where}.{key}")
    return value


def _persona_from(raw: dict, where: str) -> Persona:
    defaults = LEGACY_PERSONA if where == "legacy" else None
    try:
        max_context = int(raw.get("max_context", defaults.max_context if defaults else 10))
    except (TypeError, ValueError) as e:
        raise ConfigError(f"配置项 {where}.persona.max_context 必须是整数") from e
    if max_context < 0:
        raise ConfigError(f"配置项 {where}.persona.max_context 不能为负数")
    return Persona(
        display_name=str(raw.get("display_name") or (defaults.display_name if defaults else "知识助手")),
        welcome=str(raw.get("welcome") or (defaults.welcome if defaults else GENERIC_WELCOME)),
        menu=str(raw.get("menu") or (defaults.menu if defaults else GENERIC_MENU)),
        reset=str(raw.get("reset") or (defaults.reset if defaults else GENERIC_RESET)),
        max_context=max_context,
    )


def _maxkb_from(raw: dict, where: str) -> MaxKBConfig:
    base_url = _require(raw, "base_url", where)
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"配置项 {where}.base_url 不是合法的 http(s) 地址: {base_url}")
    auth_mode = str(raw.get("auth_mode", "share_token")).strip()
    if auth_mode not in _VALID_AUTH_MODES:
        raise ConfigError(f"配置项 {where}.auth_mode 只能是 share_token 或 api_key")
    access_token = str(raw.get("access_token", "")).strip()
    api_key = str(raw.get("api_key", "")).strip()
    if auth_mode == "share_token" and not access_token:
        raise ConfigError(f"配置项 {where}.access_token 未配置（auth_mode=share_token）")
    if auth_mode == "api_key" and not api_key:
        raise ConfigError(f"配置项 {where}.api_key 未配置（auth_mode=api_key）")
    return MaxKBConfig(
        base_url=base_url.rstrip("/"),
        auth_mode=auth_mode,
        access_token=access_token,
        api_key=api_key,
        application_id=str(raw.get("application_id", "")).strip(),
    )


def _legacy_config(base_dir: Path) -> AppConfig:
    missing = [k for k in ("WECOM_BOT_ID", "WECOM_SECRET") if not read_env_value(k, base_dir)]
    if missing:
        raise ConfigError(f"缺少配置项: {', '.join(missing)}，请检查 .env 或 config.toml")

    maxkb = _maxkb_from(
        {
            "base_url": read_env_value("MAXKB_API_BASE", base_dir) or "http://localhost:8081",
            "auth_mode": read_env_value("MAXKB_AUTH_MODE", base_dir) or "share_token",
            "access_token": read_env_value("MAXKB_ACCESS_TOKEN", base_dir),
            "api_key": read_env_value("MAXKB_API_KEY", base_dir),
            "application_id": read_env_value("MAXKB_APPLICATION_ID", base_dir),
        },
        "legacy.maxkb",
    )
    persona_raw = {
        "display_name": read_env_value("BOT_DISPLAY_NAME", base_dir),
        "welcome": read_env_value("BOT_WELCOME", base_dir),
        "menu": read_env_value("BOT_MENU", base_dir),
        "reset": read_env_value("BOT_RESET", base_dir),
        "max_context": read_env_value("BOT_MAX_CONTEXT", base_dir) or LEGACY_PERSONA.max_context,
    }
    state_dir = base_dir / "logs" / "sessions"
    return AppConfig(
        server=ServerConfig(
            state_dir=state_dir,
            log_level="INFO",
            log_file=base_dir / "logs" / "bot.log",
        ),
        bot=BotConfig(
            name="default",
            wecom_bot_id=read_env_value("WECOM_BOT_ID", base_dir),
            wecom_secret=read_env_value("WECOM_SECRET", base_dir),
            maxkb=maxkb,
            persona=_persona_from(persona_raw, "legacy"),
            state_file=state_dir / "conversations.json",
        ),
        source_path=None,
    )


def _toml_config(config_path: Path, base_dir: Path) -> AppConfig:
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"读取配置文件失败: {e}") from e
    data = _expand(data, base_dir)

    server_raw = data.get("server") or {}
    state_dir = Path(str(server_raw.get("state_dir", "logs/sessions")))
    if not state_dir.is_absolute():
        state_dir = base_dir / state_dir
    state_dir = state_dir.resolve()
    log_level = str(server_raw.get("log_level", "INFO")).upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError("server.log_level 只能是 DEBUG/INFO/WARNING/ERROR/CRITICAL")
    log_file = Path(str(server_raw.get("log_file", "logs/bot.log")))
    if not log_file.is_absolute():
        log_file = base_dir / log_file
    log_file = log_file.resolve()

    bots = data.get("bot")
    if not isinstance(bots, list) or not bots:
        raise ConfigError("配置文件缺少 [[bot]] 段")
    for item in bots:
        if isinstance(item, dict) and not isinstance(item.get("enabled", True), bool):
            raise ConfigError("bot.enabled 必须是 true 或 false")
    enabled = [b for b in bots if isinstance(b, dict) and b.get("enabled", True) is not False]
    if not enabled:
        raise ConfigError("没有 enabled = true 的 bot")
    if len(enabled) > 1:
        raise ConfigError("当前版本只支持一个 enabled bot；多机器人将在后续版本支持")

    raw = enabled[0]
    name = str(raw.get("name", "")).strip()
    if not _NAME_PATTERN.fullmatch(name):
        raise ConfigError("bot.name 只能包含小写字母、数字、下划线和短横线，长度 1-32")
    wecom_bot_id = _require(raw, "wecom_bot_id", f"bot[{name}]")
    wecom_secret = _require(raw, "wecom_secret", f"bot[{name}]")

    maxkb_raw = raw.get("maxkb")
    if not isinstance(maxkb_raw, dict):
        raise ConfigError(f"bot[{name}].maxkb 未配置")
    maxkb = _maxkb_from(maxkb_raw, f"bot[{name}].maxkb")

    persona_raw = raw.get("persona") if isinstance(raw.get("persona"), dict) else {}
    persona = _persona_from(persona_raw, f"bot[{name}]")

    state_file = Path(str(raw.get("state_file") or (state_dir / f"{name}.conversations.json")))
    if not state_file.is_absolute():
        state_file = base_dir / state_file
    state_file = state_file.resolve()
    if not state_file.is_relative_to(state_dir):
        raise ConfigError(f"bot[{name}].state_file 必须位于 server.state_dir 目录内")

    return AppConfig(
        server=ServerConfig(state_dir=state_dir, log_level=log_level, log_file=log_file),
        bot=BotConfig(
            name=name,
            wecom_bot_id=wecom_bot_id,
            wecom_secret=wecom_secret,
            maxkb=maxkb,
            persona=persona,
            state_file=state_file,
        ),
        source_path=config_path,
    )


def load_config(path: str | None = None, base_dir: Path | None = None) -> AppConfig:
    """加载 config.toml；未提供且默认文件不存在时回退到旧 .env 模式。"""
    base = Path(base_dir) if base_dir else BASE_DIR
    load_env(base)

    if path:
        config_path = Path(path)
        if not config_path.is_absolute():
            config_path = base / config_path
        if not config_path.exists():
            raise ConfigError(f"配置文件不存在: {config_path}")
    else:
        candidate = base / "config" / "config.toml"
        config_path = candidate if candidate.exists() else None

    if config_path is None:
        return _legacy_config(base)
    return _toml_config(config_path, base)
