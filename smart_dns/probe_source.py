from __future__ import annotations

import asyncio
import json
import math
import inspect
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from .models import Node, ProbeSource
from .state import TrafficSnapshot
from .tls import verified_context
from .tasks import supervised_loop


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError):
        return None


def _traffic_limit_gib(value: Any) -> float | None:
    number = _number(value)
    if number is None or number < 0:
        return None
    # Upstream labels this GB but src/utils/traffic.js defines GB = 1024**3.
    return number


def _pick_used_gib(
    node: Node, record: dict[str, Any], limit: float | None
) -> tuple[float | None, float | None, float | None]:
    rx = _number(record.get("net_rx_monthly"))
    tx = _number(record.get("net_tx_monthly"))
    rx_gib = rx / 1024**3 if rx is not None else None
    tx_gib = tx / 1024**3 if tx is not None else None
    mode = node.traffic_counter
    if mode == "auto":
        source_mode = str(record.get("traffic_calc_type") or "total").strip().lower()
        if source_mode in {"ul", "tx", "upload", "up", "out"}:
            mode = "upload"
        elif source_mode in {"dl", "rx", "download", "down", "in"}:
            mode = "download"
        elif source_mode == "max":
            mode = "max"
        elif source_mode != "total":
            return None, rx_gib, tx_gib
        else:
            mode = "total"
    if mode == "upload":
        return tx_gib, rx_gib, tx_gib
    if mode == "download":
        return rx_gib, rx_gib, tx_gib
    if rx_gib is None or tx_gib is None:
        return None, rx_gib, tx_gib
    if mode == "max":
        return max(rx_gib, tx_gib), rx_gib, tx_gib
    return (rx_gib or 0.0) + (tx_gib or 0.0), rx_gib, tx_gib


def metric_snapshot(
    node: Node, record: dict[str, Any], synced_at: str, now_epoch: float, max_age: int
) -> TrafficSnapshot:
    limit = node.traffic_limit_gib_override
    if limit is None:
        limit = _traffic_limit_gib(record.get("traffic_limit"))
    used, rx_gib, tx_gib = _pick_used_gib(node, record, limit)
    last_updated_ms = int(_number(record.get("last_updated")) or 0)
    age = max(0.0, now_epoch - last_updated_ms / 1000) if last_updated_ms else None
    reset_day = (
        node.reset_day_override
        if node.reset_day_override is not None
        else int(_number(record.get("reset_day")) or 0)
    )
    error = ""
    if used is None:
        error = "探针周期流量缺失或计费方式无效"
    elif not 0 <= reset_day <= 31:
        error = "探针重置日无效"
    elif last_updated_ms > (now_epoch + 60) * 1000:
        error = "探针更新时间超前，请检查服务器时钟"
    elif last_updated_ms <= 0:
        error = "探针未提供更新时间"
    elif age is not None and age > max_age:
        error = f"指标已过期 {int(age)} 秒"
    return TrafficSnapshot(
        probe_server_id=str(record.get("id") or ""),
        probe_server_name=str(record.get("name") or ""),
        used_gib=used,
        limit_gib=limit,
        rx_gib=rx_gib,
        tx_gib=tx_gib,
        rx_speed_bps=_number(record.get("net_in_speed")),
        tx_speed_bps=_number(record.get("net_out_speed")),
        reset_day=reset_day,
        source_last_updated_ms=last_updated_ms,
        synced_at=synced_at,
        age_seconds=age,
        fresh=not error,
        error=error,
    )


async def fetch_probe_servers(
    source: ProbeSource,
) -> tuple[dict[str, dict[str, Any]], str]:
    if not source.base_url:
        raise RuntimeError("探针 API 地址尚未配置")
    headers = {"Accept": "application/json"}
    if source.bearer_token:
        headers["Authorization"] = f"Bearer {source.bearer_token}"
    timeout = httpx.Timeout(source.request_timeout_seconds)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        headers=headers,
        trust_env=False,
        verify=verified_context(),
    ) as client:
        async with client.stream("GET", source.base_url + "/api/servers") as response:
            response.raise_for_status()
            if (
                response.headers.get("content-type", "").split(";", 1)[0].lower()
                != "application/json"
            ):
                raise RuntimeError("探针 API 未返回 JSON")
            length = int(response.headers.get("content-length", "0") or 0)
            if length > 2_000_000:
                raise RuntimeError("探针 API 响应超过 2 MB")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 2_000_000:
                    raise RuntimeError("探针 API 响应超过 2 MB")
            payload = json.loads(body)
    records = payload.get("servers") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise RuntimeError("探针 API 响应缺少 servers 数组")
    by_key: dict[str, dict[str, Any]] = {}
    duplicate_names: set[str] = set()
    for item in records:
        if not isinstance(item, dict):
            continue
        if item.get("id"):
            key = "id:" + str(item["id"])
            if key in by_key:
                raise RuntimeError("探针返回重复服务器 ID，已拒绝本次同步")
            by_key[key] = item
        if item.get("name"):
            key = "name:" + str(item["name"])
            if key in by_key:
                duplicate_names.add(key)
            by_key[key] = item
    for key in duplicate_names:
        by_key.pop(key, None)
    return by_key, datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProbePoller:
    def __init__(self, source: ProbeSource, callback, event_callback=None):
        self.source = source
        self.callback = callback
        self.event_callback = event_callback
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="probe-source-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        async def poll():
            if self.source.base_url:
                records, synced_at = await fetch_probe_servers(self.source)
                result = self.callback(records, synced_at)
                if inspect.isawaitable(result):
                    await result

        def error(exc):
            if self.event_callback:
                return self.event_callback(
                    "探针同步失败：" + type(exc).__name__ + "，请检查地址、令牌和网络"
                )

        await supervised_loop(
            "探针同步",
            poll,
            self._stop,
            lambda: self.source.poll_interval_seconds,
            error,
            retry_base=min(5, self.source.poll_interval_seconds),
        )
