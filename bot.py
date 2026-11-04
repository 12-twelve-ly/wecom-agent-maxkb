# -*- coding: utf-8 -*-
"""企业微信智能机器人 <-> MaxKB 中转服务（单机器人版）

架构：企微智能机器人 <--(WebSocket长连接)--> 本程序 <--(HTTP API)--> MaxKB 应用
配置：优先读取 config.toml；没有配置时回退到旧的 .env 单机器人模式。
"""
import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from wecom_aibot_sdk import WSClient, generate_req_id

from config import AppConfig, ConfigError, LEGACY_PERSONA, Persona, load_config, render
from maxkb_client import MaxKBClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("wecom-maxkb-bot")

# 运行期状态：在 main() 中由 init_runtime() 根据配置初始化
CONFIG: AppConfig | None = None
PERSONA: Persona = LEGACY_PERSONA
MAXKB_CLIENT: MaxKBClient | None = None

CONV_FILE = Path(__file__).parent / "conversations.json"


def _load_conversations(path: Path) -> dict:
    """加载历史会话；文件损坏或格式不符时返回空字典，不影响启动"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:  # ValueError 同时覆盖 JSONDecodeError / UnicodeDecodeError
        log.warning("读取 %s 失败，忽略旧会话: %s", path.name, e)
        return {}
    if not isinstance(data, dict):
        log.warning("%s 内容不是字典，忽略旧会话", path.name)
        return {}
    return data


_conversations: dict = _load_conversations(CONV_FILE)


def save_conversations() -> None:
    """原子写入会话映射；失败只记日志，不能影响已经生成的回答"""
    tmp = CONV_FILE.with_name(f"{CONV_FILE.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        data = json.dumps(_conversations, ensure_ascii=False, indent=2)
        # POSIX 下以 0600 创建临时文件，O_EXCL/O_NOFOLLOW 防止预置符号链接
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(tmp, flags, 0o600)
        if os.name != "nt":
            try:
                os.fchmod(fd, 0o600)  # 兼容旧版本残留的 644 临时文件
            except OSError:
                pass
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, CONV_FILE)
    except OSError as e:
        log.warning("保存会话失败: %s", e)


# 每个用户的会话“代次”：重置时递增，进行中的旧请求据此丢弃会话写回
_session_generations: dict[str, int] = {}
_user_locks: dict[str, asyncio.Lock] = {}


def user_lock(user_id: str) -> asyncio.Lock:
    """同一用户的提问串行执行，避免并发使用同一个 MaxKB 会话"""
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    return lock


def reset_session(user_id: str) -> None:
    """重置用户上下文；代次递增可阻止进行中的旧请求把会话写回"""
    _session_generations[user_id] = _session_generations.get(user_id, 0) + 1
    _conversations.pop(user_id, None)
    save_conversations()


def _store_session_if_current(user_id: str, generation: int, session: dict) -> None:
    if _session_generations.get(user_id, 0) != generation:
        log.info("会话已重置，丢弃旧会话写回 user=%s", user_id)
        return
    if _conversations.get(user_id) != session:
        _conversations[user_id] = session
        save_conversations()


async def ask_maxkb(
    user_id: str,
    question: str,
    on_delta=None,
    username: str | None = None,
) -> str:
    """调用 MaxKB；每个企微用户绑定一个 MaxKB 会话，代次保证重置后不写回旧会话。"""
    if MAXKB_CLIENT is None:
        raise RuntimeError("MaxKB 客户端未初始化")
    generation = _session_generations.get(user_id, 0)
    session = _conversations.get(user_id)
    answer, new_session = await MAXKB_CLIENT.ask(
        session, user_id, question, on_delta=on_delta, username=username
    )
    _store_session_if_current(user_id, generation, new_session)
    return answer


# ---------------- 企微机器人 ----------------
RESET_WORDS = {"重置", "清除上下文", "/reset", "重新开始"}
MENU_WORDS = {"菜单", "帮助", "help", "/help", "menu", "/menu"}

# 答题模式：多段语音/文字先缓冲拼接，最后一次性提交
ANSWER_START_WORDS = {"答题", "开始答题", "开始回答"}
ANSWER_DONE_WORDS = {"回答完毕", "回答完了", "说完了", "提交回答"}
ANSWER_CANCEL_WORDS = {"取消答题", "取消回答", "放弃回答"}
BUFFER_SILENCE_SECONDS = 240  # 静默超时自动提交
MAX_QUESTION_CHARS = 4000      # 单条消息长度上限
MAX_BUFFER_PARTS = 40          # 答题模式最多拼接段数
MAX_BUFFER_CHARS = 20000       # 答题模式拼接总长度上限

_answer_buffers: dict[str, dict] = {}  # user_id -> {"parts": [], "task": Task|None, "frame": frame}

WECOM_MAX_BYTES = 20_480  # 企微流式回复 content 上限（按累计全文的 UTF-8 字节数）
TRUNCATE_NOTE = "\n\n…（内容过长，已截断，可缩小问题范围后重试）"


def _truncate_utf8(text: str, max_bytes: int = WECOM_MAX_BYTES) -> str:
    """按 UTF-8 字节安全截断，避免超出企微单条消息上限"""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    limit = max_bytes - len(TRUNCATE_NOTE.encode("utf-8"))
    if limit <= 0:
        return data[:max_bytes].decode("utf-8", "ignore")
    return data[:limit].decode("utf-8", "ignore") + TRUNCATE_NOTE


async def submit_buffer(
    user_id: str,
    client: WSClient,
    qa_fn: Callable[[Any, str, str], Awaitable[None]],
) -> None:
    """把缓冲的多段内容拼接成一条完整问题提交"""
    buf = _answer_buffers.pop(user_id, None)
    if not buf:
        return
    task = buf.get("task")
    # 自动提交时当前任务就是 buf["task"]，不能取消自己，否则后续 await 会抛 CancelledError
    if task is not None and task is not asyncio.current_task():
        task.cancel()
    merged = "\n".join(buf["parts"]).strip()
    if not merged:
        stream_id = generate_req_id("stream")
        await client.reply_stream(buf["frame"], stream_id, "没有记录到有效内容，本次答题作废。", finish=True)
        return
    log.info("提交答题 user=%s parts=%d chars=%d", user_id, len(buf["parts"]), len(merged))
    await qa_fn(buf["frame"], user_id, merged)


def _clear_answer_buffers() -> None:
    """断线时丢弃答题缓冲，避免用已失效的 frame/client 自动提交"""
    if not _answer_buffers:
        return
    for buf in _answer_buffers.values():
        task = buf.get("task")
        if task is not None and not task.done():
            task.cancel()
    _answer_buffers.clear()
    log.info("连接断开，已清空未提交的答题缓冲")


async def run_once(config: AppConfig) -> None:
    client = WSClient({"bot_id": config.bot.wecom_bot_id, "secret": config.bot.wecom_secret})
    persona = config.bot.persona

    async def handle_question(frame, content: str, source: str = "text") -> None:
        body = frame.body if isinstance(frame.body, dict) else {}
        sender = body.get("from")
        user_id = sender.get("userid") if isinstance(sender, dict) else None
        if not user_id:
            log.warning("消息缺少 userid，无法确定会话，已忽略")
            return
        # 不记录消息正文，避免业务数据长期留在日志里
        log.info("收到消息(%s) user=%s chars=%d", source, user_id, len(content))

        stream_id = generate_req_id("stream")

        if not content:
            await client.reply_stream(frame, stream_id, "没识别到内容，请再说一次或直接打字发送。", finish=True)
            return
        if len(content) > MAX_QUESTION_CHARS:
            await client.reply_stream(
                frame, stream_id,
                f"内容太长了（{len(content)} 字，最多 {MAX_QUESTION_CHARS} 字），请精简后再发。",
                finish=True,
            )
            return

        buf = _answer_buffers.get(user_id)

        # 重置上下文指令（同时退出答题模式）
        if content in RESET_WORDS:
            if buf and buf.get("task"):
                buf["task"].cancel()
            _answer_buffers.pop(user_id, None)
            reset_session(user_id)
            await client.reply_stream(frame, stream_id, render(persona.reset, persona), finish=True)
            return

        # 进入答题模式
        if content in ANSWER_START_WORDS:
            if buf and buf.get("task"):
                buf["task"].cancel()
            _answer_buffers[user_id] = {"parts": [], "task": None, "frame": frame}
            await client.reply_stream(
                frame, stream_id,
                "已进入答题模式 🎙️\n"
                "接下来你发的语音和文字都会拼接为一次回答，不会逐条提交。\n"
                "说完发「回答完毕」提交；发「取消答题」放弃；静默 4 分钟会自动提交。",
                finish=True,
            )
            return

        # 答题模式内：缓冲 / 提交 / 取消
        if buf is not None:
            buf["frame"] = frame  # 始终用最新 frame 回复
            if content in ANSWER_CANCEL_WORDS:
                if buf.get("task"):
                    buf["task"].cancel()
                _answer_buffers.pop(user_id, None)
                await client.reply_stream(frame, stream_id, "已取消本次答题，记录的内容已丢弃。", finish=True)
                return
            if content in ANSWER_DONE_WORDS:
                await submit_buffer(user_id, client, qa)
                return
            total = sum(len(p) for p in buf["parts"])
            if len(buf["parts"]) >= MAX_BUFFER_PARTS or total + len(content) > MAX_BUFFER_CHARS:
                await client.reply_stream(
                    frame, stream_id,
                    f"答题内容已达上限（{MAX_BUFFER_PARTS} 段 / {MAX_BUFFER_CHARS} 字），请发「回答完毕」提交。",
                    finish=True,
                )
                return
            buf["parts"].append(content)
            schedule_autosubmit(user_id)
            total += len(content)
            await client.reply_stream(
                frame, stream_id,
                f"已记录第 {len(buf['parts'])} 段（累计 {total} 字）。继续说，或发「回答完毕」提交。",
                finish=True,
            )
            return

        # 菜单/帮助指令
        if content.lower() in MENU_WORDS:
            await client.reply_stream(frame, stream_id, render(persona.menu, persona), finish=True)
            return

        await qa(frame, user_id, content)

    async def qa(frame, user_id: str, content: str) -> None:
        """把一条完整问题发给 MaxKB 并流式回推"""
        stream_id = generate_req_id("stream")
        await client.reply_stream(frame, stream_id, "思考中...", finish=False)

        # 边生成边刷新到企微（节流：最多每 1.5 秒推一次）
        state = {"last": 0.0}
        async def on_delta(text: str):
            now = time.time()
            if now - state["last"] >= 1.5:
                state["last"] = now
                try:
                    await client.reply_stream(frame, stream_id, _truncate_utf8(text), finish=False)
                except Exception as e:
                    log.debug("流式刷新失败: %s", e)

        try:
            # 同一用户串行提问，避免并发写同一个 MaxKB 会话
            async with user_lock(user_id):
                answer = await ask_maxkb(user_id, content, on_delta=on_delta, username=user_id)
        except Exception:
            log.exception("调用 MaxKB 失败 user=%s", user_id)
            answer = "抱歉，服务暂时不可用，请稍后再试。"
        await client.reply_stream(frame, stream_id, _truncate_utf8(answer), finish=True)

    def schedule_autosubmit(user_id: str) -> None:
        """静默超时自动提交答题缓冲"""
        buf = _answer_buffers.get(user_id)
        if not buf:
            return
        if buf.get("task"):
            buf["task"].cancel()
        async def _auto():
            await asyncio.sleep(BUFFER_SILENCE_SECONDS)
            try:
                if user_id in _answer_buffers:
                    await submit_buffer(user_id, client, qa)
            except Exception:
                log.exception("自动提交答题失败 user=%s", user_id)
        buf["task"] = asyncio.create_task(_auto())

    def text_field(frame, key: str) -> str:
        """从回调帧安全取出字符串字段，类型不符时返回空串"""
        body = frame.body or {}
        value = body.get(key)
        if not isinstance(value, dict):
            return ""
        content = value.get("content", "")
        return content.strip() if isinstance(content, str) else ""

    async def on_text(frame):
        await handle_question(frame, text_field(frame, "text"), source="text")

    async def on_voice(frame):
        # 企微服务端已做语音识别，voice.content 直接是文本
        content = text_field(frame, "voice")
        # 语音识别常带结尾标点，去掉以便匹配「重置」等指令
        content = content.rstrip("。！？!?，,、~ ")
        await handle_question(frame, content, source="voice")

    async def on_enter(frame):
        await client.reply_welcome(frame, {
            "msgtype": "text",
            "text": {"content": render(persona.welcome, persona)},
        })

    client.on("message.text", on_text)
    client.on("message.voice", on_voice)
    client.on("event.enter_chat", on_enter)

    try:
        await client.connect_async()
        log.info("已连接企微 bot=%s，等待消息中...", config.bot.name)
        while client.is_connected:
            await asyncio.sleep(1)
    finally:
        # 清理本连接遗留的缓冲与重连任务，避免旧 client 与新 client 并存
        _clear_answer_buffers()
        try:
            await client.disconnect()
        except Exception:
            log.exception("关闭企微连接失败")


def _migrate_state(legacy_file: Path) -> None:
    """目标状态文件不存在时，从旧 conversations.json 复制一次。"""
    if CONV_FILE.resolve() == legacy_file.resolve() or CONV_FILE.exists() or not legacy_file.exists():
        return
    try:
        CONV_FILE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(legacy_file, CONV_FILE)
        if os.name != "nt":
            os.chmod(legacy_file, 0o600)
        log.info("已迁移旧会话 %s -> %s", legacy_file.name, CONV_FILE)
    except OSError as e:
        log.warning("迁移旧会话失败: %s", e)


def _harden_early(base: Path, config_path: str | None) -> None:
    """在加载配置前收紧 .env / 配置文件权限（POSIX；Windows 需 icacls）。"""
    if os.name == "nt":
        return
    targets = [base / ".env"]
    targets.append(Path(config_path) if config_path else base / "config.toml")
    for path in targets:
        if not path.is_absolute():
            path = base / path
        try:
            if path.exists():
                os.chmod(path, 0o600)
        except OSError:
            pass


def _harden_permissions(config: AppConfig) -> None:
    """POSIX 下收紧配置/状态文件权限；Windows 需用 icacls 手工处理"""
    if os.name == "nt":
        return
    legacy_file = Path(__file__).parent / "conversations.json"
    paths = [Path(__file__).parent / ".env", legacy_file, CONV_FILE]
    if config.source_path is not None:
        paths.append(config.source_path)
    for path in paths:
        try:
            if path.exists():
                os.chmod(path, 0o600)
        except OSError as e:
            log.warning("收紧 %s 权限失败: %s", path.name, e)
    try:
        config.server.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(config.server.state_dir, 0o700)
    except OSError as e:
        log.warning("收紧状态目录权限失败: %s", e)


def init_runtime(config: AppConfig) -> None:
    """根据配置初始化全局状态（单机器人）。"""
    global CONFIG, PERSONA, MAXKB_CLIENT, CONV_FILE, _conversations
    CONFIG = config
    PERSONA = config.bot.persona
    MAXKB_CLIENT = MaxKBClient(config.bot.maxkb)
    CONV_FILE = config.bot.state_file
    _migrate_state(Path(__file__).parent / "conversations.json")
    try:
        CONV_FILE.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("创建状态目录失败: %s", e)
    _conversations = _load_conversations(CONV_FILE)
    logging.getLogger().setLevel(getattr(logging, config.server.log_level, logging.INFO))
    _harden_permissions(config)


async def serve(config: AppConfig) -> None:
    """外层重连循环。"""
    backoff = 5
    while True:
        try:
            await run_once(config)
            backoff = 5
        except Exception:
            log.exception("企微连接异常")
        log.warning("WebSocket 已断开，%s 秒后重连...", backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


async def run_check(config: AppConfig) -> None:
    """只读自检：验证 MaxKB 认证与创建会话，不发送问题。"""
    client = MaxKBClient(config.bot.maxkb)
    await client.check()
    print(
        f"配置正常: bot={config.bot.name} "
        f"maxkb={config.bot.maxkb.base_url} auth_mode={config.bot.maxkb.auth_mode}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="企微智能机器人 <-> MaxKB 中转服务")
    parser.add_argument("--config", help="配置文件路径（默认使用同目录 config.toml；不存在则用 .env）")
    parser.add_argument("--check", action="store_true", help="只校验配置并测试 MaxKB 连通性，不启动机器人")
    args = parser.parse_args()

    _harden_early(Path(__file__).parent, args.config)

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"配置错误：{e}", file=sys.stderr)
        raise SystemExit(2) from e

    if args.check:
        try:
            asyncio.run(run_check(config))
        except Exception as e:
            print(f"自检失败：{e}", file=sys.stderr)
            raise SystemExit(1) from e
        return

    try:
        init_runtime(config)
    except ConfigError as e:
        print(f"配置错误：{e}", file=sys.stderr)
        raise SystemExit(2) from e

    try:
        asyncio.run(serve(config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
