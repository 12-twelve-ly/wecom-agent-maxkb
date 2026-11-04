# -*- coding: utf-8 -*-
"""usage_report.py 纯逻辑测试（不访问 docker / 数据库）"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import usage_report  # noqa: E402


class Args:
    def __init__(self, all: bool = False, days: int = 7):
        self.all = all
        self.days = days


class UsageReportTest(unittest.TestCase):
    def test_detail_row_calibrates_tokens(self):
        row = ["2026-11-04 12:00:00", '{"username": "u1"}', "问", "答", "1000", "500"]

        out = usage_report._detail_row(row, 0.5)

        self.assertEqual(out, ["2026-11-04 12:00:00", "u1", "问", "答", "500", "250", "750"])

    def test_detail_row_keeps_raw_tokens(self):
        row = ["2026-11-04 12:00:00", "u1", "问", "答", "1000", "500"]

        out = usage_report._detail_row(row, 1.0)

        self.assertEqual(out[4:], ["1000", "500", "1500"])

    def test_detail_row_handles_null_tokens(self):
        row = ["2026-11-04 12:00:00", "u1", "问", "答", "", "500"]

        out = usage_report._detail_row(row, 1.0)

        self.assertEqual(out[4:], ["0", "500", "500"])

    def test_strip_control_removes_ansi_and_nul(self):
        self.assertEqual(usage_report._strip_control("a\x1b[31mb\x00"), "a[31mb")

    def test_sanitize_cell_blocks_formula_injection(self):
        self.assertEqual(usage_report._sanitize_cell("=SUM(A1)"), "'=SUM(A1)")
        self.assertEqual(usage_report._sanitize_cell("@cmd"), "'@cmd")

    def test_sanitize_cell_removes_carriage_return(self):
        self.assertEqual(usage_report._sanitize_cell("a\x1b[31m\rb"), "a[31mb")

    def test_detail_row_sanitizes_question_and_answer(self):
        row = ["2026-11-04 12:00:00", "u1", "=SUM(A1)", "@cmd", "1", "2"]

        out = usage_report._detail_row(row, 1.0)

        self.assertTrue(out[2].startswith("'="))
        self.assertTrue(out[3].startswith("'@"))

    def test_resolve_application_id_rejects_unsafe_token(self):
        with mock.patch.object(usage_report, "_configured_maxkb", return_value=None):
            with mock.patch.object(usage_report, "read_env_value", return_value="x' or 1=1 --"):
                with mock.patch.object(usage_report, "query_csv") as query:
                    self.assertIsNone(usage_report.resolve_application_id())
                    query.assert_not_called()

    def test_build_where_combines_time_and_app(self):
        where = usage_report.build_where(Args(), "00000000-0000-0000-0000-000000000000")

        self.assertIn("interval '7 days'", where)
        self.assertIn("c.application_id = '00000000-0000-0000-0000-000000000000'", where)

    def test_build_where_all_apps_has_no_condition(self):
        self.assertEqual(usage_report.build_where(Args(all=True), None), "")


if __name__ == "__main__":
    unittest.main()
