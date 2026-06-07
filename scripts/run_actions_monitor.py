#!/usr/bin/env python3
"""Run the aiohttp monitor inside GitHub Actions for a Beijing-time window.

The normal server.py entrypoint tries to open a browser, which is useful locally
but unnecessary in GitHub Actions. This runner imports the app, starts it on
127.0.0.1, lets the project's built-in periodic tasks run, and shuts down at
15:00 Asia/Shanghai by default.  v2.8L also supports waiting for a custom
start time when the workflow is launched manually with the Run workflow button.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import signal
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import web

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


CST = ZoneInfo("Asia/Shanghai")


def _parse_hhmm(value: str, label: str = "时间") -> tuple[int, int]:
    value = (value or "").strip()
    try:
        hour_text, minute_text = value.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except Exception as exc:
        raise ValueError(f"{label}必须是 HH:MM，例如 09:30 或 15:00；当前值: {value!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"{label}越界: {value!r}")
    return hour, minute


def _configured_push_times() -> set[str]:
    raw = os.environ.get("WECHAT_PUSH_TIME", "")
    result: set[str] = set()
    for part in re.split(r"[,，;；\s]+", raw):
        if not part:
            continue
        try:
            hour, minute = _parse_hhmm(part, "微信推送时间")
        except ValueError:
            continue
        result.add(f"{hour:02d}:{minute:02d}")
    return result


def _push_shutdown_grace_seconds(end_time: str) -> int:
    """Keep Actions alive briefly when a push is scheduled at RUN_UNTIL."""
    try:
        hour, minute = _parse_hhmm(end_time, "结束时间")
    except ValueError:
        return 0
    if f"{hour:02d}:{minute:02d}" not in _configured_push_times():
        return 0
    return max(0, int(os.environ.get("ACTIONS_PUSH_GRACE_SECONDS", "90") or 90))


def _start_at() -> datetime:
    after_minutes = os.environ.get("ACTIONS_START_AFTER_MINUTES", "").strip()
    now = datetime.now(CST)
    if after_minutes:
        minutes = max(0, int(after_minutes))
        return now + timedelta(minutes=minutes)

    start_time = os.environ.get("ACTIONS_START_TIME") or os.environ.get("RUN_FROM") or "09:30"
    hour, minute = _parse_hhmm(start_time, "开始时间")
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _deadline() -> datetime:
    after_minutes = os.environ.get("ACTIONS_END_AFTER_MINUTES", "").strip()
    now = datetime.now(CST)
    if after_minutes:
        minutes = max(1, int(after_minutes))
        return now + timedelta(minutes=minutes)

    end_time = os.environ.get("ACTIONS_END_TIME") or os.environ.get("RUN_UNTIL") or "15:00"
    hour, minute = _parse_hhmm(end_time, "结束时间")
    deadline = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    grace_seconds = _push_shutdown_grace_seconds(end_time)
    if grace_seconds:
        deadline += timedelta(seconds=grace_seconds)
        print(f"[INFO] RUN_UNTIL 与微信推送时间重合，追加 {grace_seconds} 秒保护窗口。", flush=True)
    return deadline


async def _wait_until(target_time: datetime, stop_event: asyncio.Event, label: str) -> str:
    while True:
        now = datetime.now(CST)
        remaining = (target_time - now).total_seconds()
        if remaining <= 0:
            return "deadline"
        print(
            f"[INFO] 当前北京时间 {now:%Y-%m-%d %H:%M:%S}，"
            f"{label} {target_time:%Y-%m-%d %H:%M:%S}，"
            f"剩余约 {remaining / 60:.1f} 分钟。",
            flush=True,
        )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=min(300, remaining))
            return "signal"
        except asyncio.TimeoutError:
            continue


async def main() -> None:
    start_time = _start_at()
    deadline = _deadline()
    now = datetime.now(CST)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    if now >= deadline:
        print(f"[OK] 当前北京时间 {now:%H:%M:%S} 已到/超过结束时间 {deadline:%H:%M}，无需启动。")
        return

    if start_time >= deadline:
        print(
            f"[OK] 开始时间 {start_time:%H:%M} 不早于结束时间 {deadline:%H:%M}，无需启动。",
            flush=True,
        )
        return

    if now < start_time:
        print(
            f"[INFO] 已设置北京时间开始时间 {start_time:%H:%M}，结束时间 {deadline:%H:%M}；到点后启动监控服务。",
            flush=True,
        )
        reason = await _wait_until(start_time, stop_event, "计划开始")
        if reason == "signal":
            print("[OK] 启动前收到停止信号，退出。", flush=True)
            return
        now = datetime.now(CST)
        if now >= deadline:
            print(f"[OK] 等待开始后已到/超过结束时间 {deadline:%H:%M}，无需启动。", flush=True)
            return
    else:
        print(
            f"[INFO] 当前北京时间 {now:%H:%M:%S} 已到/超过开始时间 {start_time:%H:%M}，立即启动监控。",
            flush=True,
        )

    port = int(os.environ.get("FUND_PORT", "8080"))
    from server import create_app  # noqa: WPS433 - delay import so --help works without DB deps
    app = create_app()
    runner = web.AppRunner(app)

    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=port)
    await site.start()
    print(f"[OK] LOF 监控服务已在 GitHub Actions 启动: http://127.0.0.1:{port}")
    print("[INFO] GitHub 托管 runner 不开放公网入站端口；此地址只在 runner 内部可访问。")
    print(f"[INFO] 本次运行窗口: {start_time:%H:%M} - {deadline:%H:%M} 北京时间。", flush=True)
    push_times = sorted(_configured_push_times())
    if push_times:
        print(f"[INFO] 今日微信告警检查时间: {', '.join(push_times)}；服务日志会输出每只基金的 Source=[AkShare/原有接口]。", flush=True)
    else:
        print("[INFO] 未读取到 WECHAT_PUSH_TIME；若已在 SQLite 配置推送时间，请以服务日志中的 WeChat scheduler config 为准。", flush=True)

    try:
        reason = await _wait_until(deadline, stop_event, "计划结束")
        print(f"[OK] 收到结束条件: {reason}，开始清理并退出。")
    finally:
        await runner.cleanup()
        print("[OK] 服务已停止。")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run LOF monitor inside GitHub Actions for a Beijing HH:MM window.")
    parser.add_argument("--from", dest="run_from", default="", help="北京时间开始时间，例如 09:30")
    parser.add_argument("--start", dest="run_from_alias", default="", help="北京时间开始时间，例如 09:30；--from 的别名")
    parser.add_argument("--until", default="", help="北京时间结束时间，例如 15:00")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run_from = args.run_from or args.run_from_alias
    if run_from:
        os.environ["ACTIONS_START_TIME"] = run_from
    if args.until:
        os.environ["ACTIONS_END_TIME"] = args.until
    asyncio.run(main())
