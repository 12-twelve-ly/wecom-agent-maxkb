# -*- coding: utf-8 -*-
"""config.py 配置解析测试（使用临时目录与合成值）"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402

SERVER_TEMPLATE = """
[server]
state_dir = "logs/sessions"
log_file = "logs/bot.log"
log_level = "debug"
"""

BOT_TEMPLATE = """
[[bot]]
name = "doris"
enabled = true
wecom_bot_id = "${TEST_BOT_ID}"
wecom_secret = "${TEST_BOT_SECRET}"

[bot.maxkb]
base_url = "http://localhost:8081/"
auth_mode = "share_token"
access_token = "${TEST_MAXKB_TOKEN}"

[bot.persona]
display_name = "测试助手"
menu = "菜单 {name}"
reset = "重置 {max_context}"
max_context = 5
"""

CONFIG_TEMPLATE = SERVER_TEMPLATE + BOT_TEMPLATE


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.env = {
            "TEST_BOT_ID": "bot-id",
            "TEST_BOT_SECRET": "bot-secret",
            "TEST_MAXKB_TOKEN": "share-token",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, content: str) -> Path:
        path = self.base / "config" / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_toml_single_bot(self):
        self._write(CONFIG_TEMPLATE)
        with mock.patch.dict(os.environ, self.env, clear=True):
            cfg = config.load_config(base_dir=self.base)

        self.assertEqual(cfg.bot.name, "doris")
        self.assertEqual(cfg.bot.wecom_bot_id, "bot-id")
        self.assertEqual(cfg.bot.wecom_secret, "bot-secret")
        self.assertEqual(cfg.bot.maxkb.base_url, "http://localhost:8081")
        self.assertEqual(cfg.bot.maxkb.auth_mode, "share_token")
        self.assertEqual(cfg.bot.persona.display_name, "测试助手")
        self.assertEqual(cfg.bot.persona.max_context, 5)
        self.assertEqual(
            cfg.bot.state_file.resolve(),
            (self.base / "logs" / "sessions" / "doris.conversations.json").resolve(),
        )
        self.assertEqual(
            cfg.server.log_file.resolve(),
            (self.base / "logs" / "bot.log").resolve(),
        )
        self.assertEqual(config.render("菜单 {name}/{max_context}", cfg.bot.persona), "菜单 测试助手/5")

    def test_missing_env_is_reported(self):
        self._write(CONFIG_TEMPLATE)
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(config.ConfigError) as ctx:
                config.load_config(base_dir=self.base)

        self.assertIn("TEST_BOT_ID", str(ctx.exception))

    def test_multiple_enabled_bots_rejected(self):
        content = SERVER_TEMPLATE + BOT_TEMPLATE + BOT_TEMPLATE.replace('name = "doris"', 'name = "other"')
        self._write(content)
        with mock.patch.dict(os.environ, self.env, clear=True):
            with self.assertRaises(config.ConfigError) as ctx:
                config.load_config(base_dir=self.base)

        self.assertIn("只支持一个", str(ctx.exception))

    def test_api_key_mode_fields(self):
        content = CONFIG_TEMPLATE.replace(
            'auth_mode = "share_token"\naccess_token = "${TEST_MAXKB_TOKEN}"',
            'auth_mode = "api_key"\napi_key = "application-abc"\napplication_id = "00000000-0000-0000-0000-000000000000"',
        )
        self._write(content)
        with mock.patch.dict(os.environ, self.env, clear=True):
            cfg = config.load_config(base_dir=self.base)

        self.assertEqual(cfg.bot.maxkb.auth_mode, "api_key")
        self.assertEqual(cfg.bot.maxkb.api_key, "application-abc")
        self.assertEqual(cfg.bot.maxkb.application_id, "00000000-0000-0000-0000-000000000000")

    def test_api_key_mode_requires_key(self):
        content = CONFIG_TEMPLATE.replace(
            'auth_mode = "share_token"\naccess_token = "${TEST_MAXKB_TOKEN}"',
            'auth_mode = "api_key"',
        )
        self._write(content)
        with mock.patch.dict(os.environ, self.env, clear=True):
            with self.assertRaises(config.ConfigError) as ctx:
                config.load_config(base_dir=self.base)

        self.assertIn("api_key", str(ctx.exception))

    def test_invalid_bot_name(self):
        self._write(CONFIG_TEMPLATE.replace('name = "doris"', 'name = "Doris Bot"'))
        with mock.patch.dict(os.environ, self.env, clear=True):
            with self.assertRaises(config.ConfigError):
                config.load_config(base_dir=self.base)

    def test_state_file_must_stay_inside_state_dir(self):
        content = CONFIG_TEMPLATE.replace(
            'wecom_secret = "${TEST_BOT_SECRET}"',
            'wecom_secret = "${TEST_BOT_SECRET}"\nstate_file = "../outside.json"',
        )
        self._write(content)
        with mock.patch.dict(os.environ, self.env, clear=True):
            with self.assertRaises(config.ConfigError) as ctx:
                config.load_config(base_dir=self.base)

        self.assertIn("state_dir", str(ctx.exception))

    def test_enabled_must_be_boolean(self):
        self._write(CONFIG_TEMPLATE.replace("enabled = true", 'enabled = "false"'))
        with mock.patch.dict(os.environ, self.env, clear=True):
            with self.assertRaises(config.ConfigError):
                config.load_config(base_dir=self.base)

    def test_log_level_must_be_known(self):
        self._write(CONFIG_TEMPLATE.replace('log_level = "debug"', 'log_level = "LOUD"'))
        with mock.patch.dict(os.environ, self.env, clear=True):
            with self.assertRaises(config.ConfigError):
                config.load_config(base_dir=self.base)

    def test_legacy_env_fallback(self):
        legacy_env = {
            "WECOM_BOT_ID": "legacy-bot",
            "WECOM_SECRET": "legacy-secret",
            "MAXKB_ACCESS_TOKEN": "legacy-token",
            "MAXKB_API_BASE": "http://localhost:8081",
        }
        with mock.patch.dict(os.environ, legacy_env, clear=True):
            cfg = config.load_config(base_dir=self.base)

        self.assertEqual(cfg.bot.name, "default")
        self.assertEqual(cfg.bot.wecom_bot_id, "legacy-bot")
        self.assertEqual(cfg.bot.maxkb.access_token, "legacy-token")
        self.assertEqual(cfg.bot.persona.display_name, "Doris 小助手")
        self.assertEqual(cfg.bot.state_file, self.base / "logs" / "sessions" / "conversations.json")

    def test_legacy_missing_config_is_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(config.ConfigError):
                config.load_config(base_dir=self.base)

    def test_persona_defaults_in_config_mode(self):
        content = CONFIG_TEMPLATE.split("[bot.persona]")[0]
        self._write(content)
        with mock.patch.dict(os.environ, self.env, clear=True):
            cfg = config.load_config(base_dir=self.base)

        self.assertEqual(cfg.bot.persona.display_name, "知识助手")
        self.assertEqual(cfg.bot.persona.max_context, 10)


if __name__ == "__main__":
    unittest.main()
