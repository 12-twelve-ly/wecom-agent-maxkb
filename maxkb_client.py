# -*- coding: utf-8 -*-
"""MaxKB 应用对话客户端。

支持两种认证：
- share_token：POST /chat/api/auth/anonymous 换匿名 token，再 open + chat_message；
- api_key：secret_key（application-/agent- 前缀）直接作为 Bearer，复用同一套接口。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

import httpx

from config import MaxKBConfig

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0, read=180.0, write=30.0)
TOTAL_TIMEOUT_SECONDS = 300
MAX_TRANSIENT_RETRIES = 2
BACKOFF_BASE_SECONDS = 0.5

DeltaCallback = Callable[[str], Awaitable[None]]


class MaxKBSessionError(RuntimeError):
    """会话失效：token 过期、chat_id 不存在或 API Key 无效。"""

    def __init__(self, detail: str, status: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


class MaxKBTransientError(RuntimeError):
    """可重试的临时错误（5xx、网络异常）。"""


class MaxKBClient:
    def __init__(self, config: MaxKBConfig, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self._transport = transport

    # ---------------- 基础请求 ----------------
    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, transport=self._transport)

    @staticmethod
    def _body(response: httpx.Response) -> dict:
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async def _auth(self, client: httpx.AsyncClient) -> str:
        """share_token 模式：用应用分享 token 换匿名访问 token。"""
        response = await client.post(
            f"{self.config.base_url}/chat/api/auth/anonymous",
            json={"access_token": self.config.access_token},
        )
        body = self._body(response)
        if response.status_code >= 500:
            raise MaxKBTransientError(f"MaxKB 认证失败: HTTP {response.status_code}")
        if response.status_code != 200 or body.get("code") != 200:
            raise RuntimeError(
                f"MaxKB 认证失败: HTTP {response.status_code} {body.get('message', '')}"
            )
        token = body.get("data")
        if not isinstance(token, str) or not token:
            raise RuntimeError("MaxKB 认证返回的 token 为空")
        return token

    async def _open_chat(self, client: httpx.AsyncClient, token: str) -> str:
        response = await client.get(
            f"{self.config.base_url}/chat/api/open",
            headers=self._headers(token),
        )
        body = self._body(response)
        if response.status_code >= 500:
            raise MaxKBTransientError(f"MaxKB 创建会话失败: HTTP {response.status_code}")
        if response.status_code in (401, 403):
            raise MaxKBSessionError(body.get("message", "认证失败"), response.status_code)
        if response.status_code != 200 or body.get("code") != 200:
            raise RuntimeError(
                f"MaxKB 创建会话失败: HTTP {response.status_code} {body.get('message', '')}"
            )
        chat_id = body.get("data")
        if not isinstance(chat_id, str) or not chat_id:
            raise RuntimeError("MaxKB 创建会话返回的 chat_id 为空")
        return chat_id

    async def _stream_once(
        self,
        client: httpx.AsyncClient,
        token: str,
        chat_id: str,
        question: str,
        on_delta: DeltaCallback | None,
        username: str | None,
    ) -> str:
        parts: list[str] = []
        payload: dict[str, Any] = {"message": question, "stream": True, "re_chat": False}
        if username:
            payload["form_data"] = {"asker": username}

        async with client.stream(
            "POST",
            f"{self.config.base_url}/chat/api/chat_message/{chat_id}",
            headers=self._headers(token),
            json=payload,
        ) as response:
            if response.status_code != 200:
                raw = (await response.aread()).decode("utf-8", "ignore")
                if response.status_code in (401, 403) or "对话不存在" in raw:
                    raise MaxKBSessionError(raw[:300], response.status_code)
                if response.status_code >= 500:
                    raise MaxKBTransientError(f"MaxKB 对话失败: HTTP {response.status_code}")
                raise RuntimeError(f"MaxKB 对话失败: HTTP {response.status_code} {raw[:300]}")

            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str or data_str == "[DONE]":
                    continue
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                content = data.get("content")
                if content:
                    parts.append(content)  # MaxKB 的 content 是增量片段
                    if on_delta:
                        await on_delta("".join(parts))

        return "".join(parts).strip() or "（MaxKB 返回了空内容）"

    # ---------------- 对外接口 ----------------
    async def ask(
        self,
        session: dict | None,
        user_id: str,
        question: str,
        on_delta: DeltaCallback | None = None,
        username: str | None = None,
    ) -> tuple[str, dict]:
        """发送问题并返回 (回答, 新会话)。

        user_id 仅用于日志；会话本身由调用方按用户持久化。
        """
        try:
            return await asyncio.wait_for(
                self._ask_loop(session, user_id, question, on_delta, username),
                timeout=TOTAL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as e:
            raise RuntimeError(f"MaxKB 请求超时（{TOTAL_TIMEOUT_SECONDS} 秒）") from e

    async def _ask_loop(
        self,
        session: dict | None,
        user_id: str,
        question: str,
        on_delta: DeltaCallback | None,
        username: str | None,
    ) -> tuple[str, dict]:
        current = session if isinstance(session, dict) else {}
        chat_id = current.get("chat_id") if isinstance(current.get("chat_id"), str) else None
        token = current.get("token") if isinstance(current.get("token"), str) else None
        if self.config.auth_mode == "api_key":
            token = self.config.api_key

        session_rebuilt = False
        transient_failures = 0

        async with self._new_client() as client:
            while True:
                try:
                    if self.config.auth_mode == "share_token" and not token:
                        token = await self._auth(client)
                    if not chat_id:
                        chat_id = await self._open_chat(client, token)
                    answer = await self._stream_once(
                        client, token, chat_id, question, on_delta, username
                    )
                    return answer, self._new_session(token, chat_id)
                except MaxKBSessionError as e:
                    if session_rebuilt:
                        raise
                    session_rebuilt = True
                    if self.config.auth_mode == "api_key":
                        if e.status in (401, 403):
                            raise RuntimeError("MaxKB API Key 无效或已过期") from e
                        chat_id = None  # 对话不存在，仅重建 chat
                    else:
                        token = chat_id = None
                except (MaxKBTransientError, httpx.TransportError) as e:
                    transient_failures += 1
                    if transient_failures > MAX_TRANSIENT_RETRIES:
                        raise RuntimeError(f"MaxKB 请求失败: {e}") from e
                    await asyncio.sleep(BACKOFF_BASE_SECONDS * (2 ** (transient_failures - 1)))

    def _new_session(self, token: str, chat_id: str) -> dict:
        if self.config.auth_mode == "api_key":
            return {"chat_id": chat_id}
        return {"token": token, "chat_id": chat_id}

    async def check(self) -> None:
        """认证并创建一个会话，用于启动前/命令行自检（不发送问题）。"""
        async with self._new_client() as client:
            token = self.config.api_key if self.config.auth_mode == "api_key" else await self._auth(client)
            chat_id = await self._open_chat(client, token)
            if not chat_id:
                raise RuntimeError("MaxKB 未返回 chat_id")
