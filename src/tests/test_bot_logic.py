# -*- coding: utf-8 -*-
"""bot.py 纯逻辑回归测试（不连接企微、不访问 MaxKB）

运行：.venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 先注入测试配置，避免导入 bot 时因缺少 .env 退出
os.environ.setdefault("WECOM_BOT_ID", "test-bot")
os.environ.setdefault("WECOM_SECRET", "test-secret")
os.environ.setdefault("MAXKB_ACCESS_TOKEN", "test-token")

import bot  # noqa: E402


class FakeClient:
    """只记录 reply_stream 调用的假企微客户端"""

    def __init__(self):
        self.replies = []

    async def reply_stream(self, frame, stream_id, content, finish=False):
        self.replies.append((stream_id, content, finish))


class BotLogicTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_conv_file = bot.CONV_FILE
        bot.CONV_FILE = Path(self.tmp.name) / "conversations.json"
        self.user = "test-user"
        bot._conversations.clear()
        bot._session_generations.pop(self.user, None)
        bot._answer_buffers.pop(self.user, None)
        bot._user_locks.pop(self.user, None)

    def tearDown(self):
        bot.CONV_FILE = self.orig_conv_file
        bot._conversations.clear()
        bot._session_generations.pop(self.user, None)
        bot._answer_buffers.pop(self.user, None)
        bot._user_locks.pop(self.user, None)
        self.tmp.cleanup()

    async def test_auto_submit_does_not_cancel_itself(self):
        """回归：静默自动提交时不能取消当前任务，否则回答丢失"""
        calls = []

        async def fake_qa(frame, user_id, content):
            calls.append((user_id, content))

        async def auto_submit():
            bot._answer_buffers[self.user] = {
                "parts": ["第一段", "第二段"],
                "task": asyncio.current_task(),
                "frame": object(),
            }
            await bot.submit_buffer(self.user, FakeClient(), fake_qa)

        await asyncio.create_task(auto_submit())

        self.assertEqual(calls, [(self.user, "第一段\n第二段")])
        self.assertNotIn(self.user, bot._answer_buffers)

    async def test_manual_submit_cancels_pending_autosubmit(self):
        """手动「回答完毕」时应取消尚未触发的自动提交任务"""
        cancelled = asyncio.Event()

        async def pending_auto():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(pending_auto())
        await asyncio.sleep(0)  # 让任务真正开始运行
        bot._answer_buffers[self.user] = {"parts": ["内容"], "task": task, "frame": object()}
        calls = []

        async def fake_qa(frame, user_id, content):
            calls.append(content)

        await bot.submit_buffer(self.user, FakeClient(), fake_qa)
        await asyncio.sleep(0)

        self.assertTrue(cancelled.is_set())
        self.assertEqual(calls, ["内容"])

    async def test_clear_answer_buffers_cancels_tasks(self):
        cancelled = asyncio.Event()

        async def pending():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(pending())
        await asyncio.sleep(0)
        bot._answer_buffers[self.user] = {"parts": [], "task": task, "frame": object()}

        bot._clear_answer_buffers()
        await asyncio.sleep(0)

        self.assertTrue(cancelled.is_set())
        self.assertEqual(bot._answer_buffers, {})

    async def test_empty_buffer_submits_nothing(self):
        calls = []

        async def fake_qa(frame, user_id, content):
            calls.append(content)

        client = FakeClient()
        bot._answer_buffers[self.user] = {"parts": ["", "  "], "task": None, "frame": object()}

        await bot.submit_buffer(self.user, client, fake_qa)

        self.assertEqual(calls, [])
        self.assertEqual(len(client.replies), 1)
        self.assertIn("作废", client.replies[0][1])

    async def test_user_lock_serializes_same_user(self):
        order = []

        async def worker(tag):
            async with bot.user_lock(self.user):
                order.append(f"{tag}-start")
                await asyncio.sleep(0.01)
                order.append(f"{tag}-end")

        await asyncio.gather(worker("a"), worker("b"))

        self.assertEqual(order, ["a-start", "a-end", "b-start", "b-end"])

    def test_state_migration_is_idempotent(self):
        legacy = Path(self.tmp.name) / "legacy.json"
        legacy.write_text('{"u": {"token": "t", "chat_id": "c"}}', encoding="utf-8")
        bot.CONV_FILE = Path(self.tmp.name) / "data" / "doris.conversations.json"

        bot._migrate_state(legacy)
        bot._migrate_state(legacy)  # 重复调用不应覆盖或报错

        self.assertEqual(
            json.loads(bot.CONV_FILE.read_text(encoding="utf-8")),
            {"u": {"token": "t", "chat_id": "c"}},
        )

    def test_save_failure_is_swallowed(self):
        target = Path(self.tmp.name) / "as_dir"
        target.mkdir()
        bot.CONV_FILE = target
        bot._conversations[self.user] = {"token": "t", "chat_id": "c"}

        bot.save_conversations()  # 目标为目录，应只记日志而不抛异常

    async def test_reset_discards_stale_session_writeback(self):
        """回归：重置后进行中的旧请求不能把旧会话写回"""
        bot._conversations[self.user] = {"token": "old", "chat_id": "old"}
        generation = bot._session_generations.get(self.user, 0)

        bot.reset_session(self.user)
        bot._store_session_if_current(self.user, generation, {"token": "new", "chat_id": "new"})

        self.assertNotIn(self.user, bot._conversations)

    async def test_session_saved_when_not_reset(self):
        generation = bot._session_generations.get(self.user, 0)

        bot._store_session_if_current(self.user, generation, {"token": "tok", "chat_id": "chat"})

        self.assertEqual(bot._conversations[self.user], {"token": "tok", "chat_id": "chat"})

    def test_truncate_keeps_short_text(self):
        self.assertEqual(bot._truncate_utf8("短文本"), "短文本")

    def test_truncate_respects_utf8_byte_limit(self):
        text = "长" * 20_000  # 60000 字节，超过企微 20480 上限

        out = bot._truncate_utf8(text)

        self.assertLessEqual(len(out.encode("utf-8")), bot.WECOM_MAX_BYTES)
        self.assertTrue(out.endswith(bot.TRUNCATE_NOTE))
        self.assertNotIn("�", out)
        # 上限小于提示语长度时也不能超限
        self.assertLessEqual(len(bot._truncate_utf8("长" * 100, max_bytes=10).encode("utf-8")), 10)

    def test_load_conversations_rejects_invalid_content(self):
        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        self.assertEqual(bot._load_conversations(bad), {})

        bad.write_text("[]", encoding="utf-8")
        self.assertEqual(bot._load_conversations(bad), {})

        bad.write_bytes('{"用户": "x"}'.encode("gbk"))  # 非 UTF-8（GBK）文件
        self.assertEqual(bot._load_conversations(bad), {})


if __name__ == "__main__":
    unittest.main()
