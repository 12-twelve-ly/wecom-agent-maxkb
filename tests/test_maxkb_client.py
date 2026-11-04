# -*- coding: utf-8 -*-
"""MaxKBClient 测试：使用 MockTransport，不访问真实 MaxKB"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import MaxKBConfig  # noqa: E402
from maxkb_client import MaxKBClient  # noqa: E402


def sse(*chunks: dict) -> str:
    lines = [f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks]
    return "".join(lines) + "data: [DONE]\n\n"


def share_config() -> MaxKBConfig:
    return MaxKBConfig(
        base_url="http://maxkb.local",
        auth_mode="share_token",
        access_token="share-token",
    )


def api_key_config() -> MaxKBConfig:
    return MaxKBConfig(
        base_url="http://maxkb.local",
        auth_mode="api_key",
        api_key="application-abc",
    )


class FakeMaxKB:
    """按路径返回预置响应的假 MaxKB。"""

    def __init__(self):
        self.calls = []
        self.chat_statuses = []
        self.open_chat_id = "chat-1"
        self.chunks = [{"content": "你好"}, {"content": "世界"}]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((path, request.headers.get("authorization")))
        if path.endswith("/chat/api/auth/anonymous"):
            return httpx.Response(200, json={"code": 200, "data": "jwt"})
        if path.endswith("/chat/api/open"):
            return httpx.Response(200, json={"code": 200, "data": self.open_chat_id})
        if "/chat/api/chat_message/" in path:
            status = self.chat_statuses.pop(0) if self.chat_statuses else 200
            if status != 200:
                return httpx.Response(status, text="error")
            return httpx.Response(200, text=sse(*self.chunks))
        return httpx.Response(404)


class MaxKBClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_share_token_flow(self):
        fake = FakeMaxKB()
        client = MaxKBClient(share_config(), transport=httpx.MockTransport(fake))

        answer, session = await client.ask(None, "u1", "问题", username="u1")

        self.assertEqual(answer, "你好世界")
        self.assertEqual(session, {"token": "jwt", "chat_id": "chat-1"})
        self.assertEqual(
            [c[0] for c in fake.calls],
            ["/chat/api/auth/anonymous", "/chat/api/open", "/chat/api/chat_message/chat-1"],
        )
        self.assertEqual(fake.calls[1][1], "Bearer jwt")

    async def test_api_key_flow_skips_anonymous_auth(self):
        fake = FakeMaxKB()
        client = MaxKBClient(api_key_config(), transport=httpx.MockTransport(fake))

        answer, session = await client.ask(None, "u1", "问题")

        self.assertEqual(answer, "你好世界")
        self.assertEqual(session, {"chat_id": "chat-1"})
        self.assertNotIn("/chat/api/auth/anonymous", [c[0] for c in fake.calls])
        self.assertEqual(fake.calls[0][1], "Bearer application-abc")

    async def test_api_key_invalid_raises_clear_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/chat/api/open"):
                return httpx.Response(401, json={"message": "invalid"})
            return httpx.Response(404)

        client = MaxKBClient(api_key_config(), transport=httpx.MockTransport(handler))

        with self.assertRaises(RuntimeError) as ctx:
            await client.ask(None, "u1", "问题")

        self.assertIn("API Key", str(ctx.exception))

    async def test_share_session_expired_rebuilds(self):
        fake = FakeMaxKB()
        fake.chat_statuses = [401]
        fake.open_chat_id = "chat-2"
        client = MaxKBClient(share_config(), transport=httpx.MockTransport(fake))

        answer, session = await client.ask(
            {"token": "old", "chat_id": "old"}, "u1", "问题"
        )

        self.assertEqual(answer, "你好世界")
        self.assertEqual(session["chat_id"], "chat-2")
        self.assertIn("/chat/api/auth/anonymous", [c[0] for c in fake.calls])

    async def test_transient_5xx_is_retried(self):
        fake = FakeMaxKB()
        fake.chat_statuses = [500]

        async def no_sleep(_seconds):
            return None

        client = MaxKBClient(share_config(), transport=httpx.MockTransport(fake))
        with mock.patch("maxkb_client.asyncio.sleep", new=no_sleep):
            answer, _ = await client.ask({"token": "jwt", "chat_id": "chat-1"}, "u1", "问题")

        self.assertEqual(answer, "你好世界")
        chat_calls = [c for c in fake.calls if "/chat_message/" in c[0]]
        self.assertEqual(len(chat_calls), 2)

    async def test_empty_answer_placeholder(self):
        fake = FakeMaxKB()
        fake.chunks = []
        client = MaxKBClient(share_config(), transport=httpx.MockTransport(fake))

        answer, _ = await client.ask({"token": "jwt", "chat_id": "chat-1"}, "u1", "问题")

        self.assertIn("空内容", answer)

    async def test_check_creates_chat(self):
        fake = FakeMaxKB()
        client = MaxKBClient(share_config(), transport=httpx.MockTransport(fake))

        await client.check()

        self.assertIn("/chat/api/open", [c[0] for c in fake.calls])


if __name__ == "__main__":
    unittest.main()
