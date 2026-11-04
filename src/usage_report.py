# -*- coding: utf-8 -*-
"""按用户统计 MaxKB 对话的 token 消耗。

MaxKB 的 message_tokens / answer_tokens 是本地估算值（中文按 UTF-8 字节计算），
比 DeepSeek 真实计费值大约放大 3 倍，因此本脚本统一乘以 0.3 做校准。

用法：
    python src/usage_report.py                          # 最近 7 天汇总
    python src/usage_report.py --days 30                # 最近 30 天汇总
    python src/usage_report.py --all                    # 全部历史汇总
    python src/usage_report.py --detail --days 7        # 最近 7 天逐条明细
    python src/usage_report.py --detail --all --out usage.csv  # 全部明细导出 CSV
    python src/usage_report.py --all-apps --detail      # 不按应用过滤（慎用）

默认按 config/config.toml 或 config/.env 里的凭据反查应用 ID，只统计本机器人应用。
"""
import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from config import ConfigError, load_config, read_env_value

CORRECTION = 0.3  # 估算值 → 真实值 的校准系数

CONTAINER = read_env_value("MAXKB_DB_CONTAINER") or "maxkb"
DB_USER = read_env_value("MAXKB_DB_USER") or "root"
DB_NAME = read_env_value("MAXKB_DB_NAME") or "maxkb"
DISPLAY_TZ = "Asia/Shanghai"  # 导出时间的展示时区
DETAIL_HEADER = ["时间", "用户", "提问内容", "返回内容", "输入token", "输出token", "总消耗token"]


def query_csv(sql: str, secrets: tuple[str, ...] = ()) -> list[list[str]]:
    """执行只读查询并按 CSV 解析；SQL 走 stdin，避免凭据出现在进程参数里"""
    cmd = ["docker", "exec", "-i", CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME, "--csv", "-t"]
    try:
        proc = subprocess.run(
            cmd, input=sql, capture_output=True, text=True, encoding="utf-8", timeout=60
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("查询超时（60 秒）") from None
    if proc.returncode != 0:
        err = proc.stderr.strip()
        for secret in secrets:
            if secret:
                err = err.replace(secret, "***")
        raise RuntimeError(f"查询失败：{err}")
    return [row for row in csv.reader(io.StringIO(proc.stdout)) if row]


def _configured_maxkb():
    """读取 config.toml 的单 bot MaxKB 配置；旧 .env 模式返回 None。"""
    config_file = Path(__file__).parent / "config.toml"
    try:
        cfg = load_config(None)
    except ConfigError:
        if config_file.exists():
            raise
        return None
    return None if cfg.source_path is None else cfg.bot.maxkb


def resolve_application_id() -> str | None:
    """按当前配置解析应用 ID：api_key 优先用 application_id，否则反查数据库。"""
    maxkb = _configured_maxkb()
    if maxkb is not None and maxkb.application_id:
        app_id = maxkb.application_id.strip()
        return app_id if re.fullmatch(r"[0-9a-fA-F-]{36}", app_id) else None

    if maxkb is not None and maxkb.auth_mode == "api_key":
        key = maxkb.api_key
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key or ""):
            return None
        rows = query_csv(
            "select application_id from application_api_key "
            f"where secret_key = '{key}' limit 1;",
            secrets=(key,),
        )
    else:
        token = maxkb.access_token if maxkb is not None else read_env_value("MAXKB_ACCESS_TOKEN")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", token or ""):
            return None
        rows = query_csv(
            "select application_id from application_access_token "
            f"where access_token = '{token}' limit 1;",
            secrets=(token,),
        )
    app_id = rows[0][0].strip() if rows else ""
    return app_id if re.fullmatch(r"[0-9a-fA-F-]{36}", app_id) else None


def asker_name(raw: str) -> str:
    """asker 字段是 {"username": "xxx"} 的 JSON，取 username"""
    try:
        return (json.loads(raw) or {}).get("username") or raw
    except (json.JSONDecodeError, AttributeError):
        return raw


def build_where(args, app_id: str | None = None) -> str:
    conditions = []
    if not args.all:
        conditions.append(f"r.create_time >= now() - interval '{int(args.days)} days'")
    if app_id:
        conditions.append(f"c.application_id = '{app_id}'")
    return ("where " + " and ".join(conditions)) if conditions else ""


def report(args, app_id: str | None = None) -> None:
    where = build_where(args, app_id)
    sql = (
        "select c.asker, count(*) as rounds, "
        "sum(r.message_tokens) as tin, sum(r.answer_tokens) as tout, "
        "min(r.create_time)::date, max(r.create_time)::date "
        "from application_chat_record r join application_chat c on r.chat_id = c.id "
        f"{where} "
        "group by c.asker "
        "order by (sum(r.message_tokens) + sum(r.answer_tokens)) desc;"
    )
    rows = query_csv(sql)

    if not rows:
        print("没有对话记录。")
        return

    title = "全部历史" if args.all else f"最近 {args.days} 天"
    print(f"\nDoris 小助手 token 消耗统计（{title}）")
    print(f"（原始值为 MaxKB 本地估算，已按 {CORRECTION} 系数校准为真实值）\n")
    print(f"{'用户':<34}{'轮次':>6}{'输入':>12}{'输出':>12}{'合计':>12}")
    print("-" * 76)

    total_rounds = total_in = total_out = 0
    for row in rows:
        if len(row) < 4:
            continue
        name = _strip_control(asker_name(row[0]))
        rounds = int(row[1] or 0)
        tin = int(row[2] or 0) * CORRECTION
        tout = int(row[3] or 0) * CORRECTION
        total_rounds += rounds
        total_in += tin
        total_out += tout
        print(f"{name[:32]:<34}{rounds:>6}{tin:>12,.0f}{tout:>12,.0f}{tin + tout:>12,.0f}")

    print("-" * 76)
    print(f"{'合计':<34}{total_rounds:>6}{total_in:>12,.0f}{total_out:>12,.0f}"
          f"{total_in + total_out:>12,.0f}")
    print(f"\n生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}")


FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _strip_control(text: str) -> str:
    """去掉终端/CSV 中的控制字符（保留 \\t 和 \\n），避免 ANSI 转义或行覆盖"""
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)


def _sanitize_cell(text: str) -> str:
    """净化单元格，并对可能触发 Excel 公式的起始字符加前缀"""
    cleaned = _strip_control(text)
    return "'" + cleaned if cleaned.startswith(FORMULA_PREFIXES) else cleaned


def _detail_row(row: list[str], factor: float) -> list[str]:
    """把一条 psql CSV 记录转换成导出字段"""
    timestamp, asker, question, answer, tin, tout = row[:6]
    in_tokens = round(int(tin or 0) * factor)
    out_tokens = round(int(tout or 0) * factor)
    return [
        _sanitize_cell(timestamp),
        _sanitize_cell(asker_name(asker)),
        _sanitize_cell(question),
        _sanitize_cell(answer),
        str(in_tokens),
        str(out_tokens),
        str(in_tokens + out_tokens),
    ]


def detail(args, app_id: str | None = None) -> None:
    """导出每条问答的明细日志：时间、用户、问答内容、token"""
    where = build_where(args, app_id)
    sql = (
        f"select to_char(r.create_time at time zone '{DISPLAY_TZ}', 'YYYY-MM-DD HH24:MI:SS'), "
        "c.asker, r.problem_text, r.answer_text, r.message_tokens, r.answer_tokens "
        "from application_chat_record r join application_chat c on r.chat_id = c.id "
        f"{where} "
        "order by r.create_time;"
    )
    rows = [row for row in query_csv(sql) if len(row) >= 6]
    factor = 1.0 if args.raw else CORRECTION

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # 导出内容含用户问答，POSIX 下以 0600 创建
        fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        if os.name != "nt":
            try:
                os.fchmod(fd, 0o600)  # 已存在的 644 文件也要收紧
            except OSError:
                pass
        handle = os.fdopen(fd, "w", encoding="utf-8-sig", newline="")
    else:
        handle = sys.stdout

    try:
        writer = csv.writer(handle)
        writer.writerow(DETAIL_HEADER)
        for row in rows:
            writer.writerow(_detail_row(row, factor))
    finally:
        if args.out:
            handle.close()

    print(f"已导出 {len(rows)} 条记录到 {args.out or '标准输出'}", file=sys.stderr)
    if not args.raw:
        print(f"token 为 MaxKB 原始估算值 × {CORRECTION} 校准；加 --raw 导出原始值", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="统计 MaxKB 各用户的 token 消耗")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--days", type=int, default=7, help="统计最近 N 天（默认 7）")
    group.add_argument("--all", action="store_true", help="统计全部历史")
    parser.add_argument("--detail", action="store_true", help="导出每条问答明细（时间/用户/问答/token）")
    parser.add_argument("--out", metavar="FILE", help="明细导出为 CSV 文件（默认输出到终端）")
    parser.add_argument("--raw", action="store_true", help="明细中的 token 使用 MaxKB 原始估算值")
    parser.add_argument("--all-apps", action="store_true", help="不按应用过滤（可能包含其他应用的数据）")
    args = parser.parse_args()

    if args.raw and not args.detail:
        parser.error("--raw 只能与 --detail 一起使用")
    if args.out and not args.detail:
        parser.error("--out 只能与 --detail 一起使用")

    try:
        app_id = None if args.all_apps else resolve_application_id()
        if not args.all_apps and not app_id:
            raise RuntimeError(
                "无法从 .env 的 MAXKB_ACCESS_TOKEN 解析应用 ID；"
                "如确需统计整个 MaxKB 实例，请加 --all-apps"
            )
        if args.detail:
            detail(args, app_id)
        else:
            report(args, app_id)
    except Exception as e:
        print(f"出错了：{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
