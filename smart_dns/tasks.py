"""后台任务共用的故障隔离、日志和有界退避。"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time

logger = logging.getLogger(__name__)


async def supervised_loop(
    name, operation, stop, interval, on_error=None, retry_base=1.0, retry_max=60.0
):
    failures = 0
    while not stop.is_set():
        started = time.monotonic()
        try:
            await operation()
            failures = 0
            delay = max(0.001, interval() - (time.monotonic() - started))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures += 1
            # 日志只记录异常类型，避免 HTTP URL / 凭证混入日志。
            logger.error("后台任务 %s 异常：%s；将自动重试", name, type(exc).__name__)
            if on_error:
                try:
                    result = on_error(exc)
                    if inspect.isawaitable(result):
                        await result
                except Exception as event_error:
                    logger.error(
                        "后台任务 %s 事件记录失败：%s", name, type(event_error).__name__
                    )
            ceiling = retry_max() if callable(retry_max) else retry_max
            delay = min(ceiling, retry_base * 2 ** min(failures - 1, 8))
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
