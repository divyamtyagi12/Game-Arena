"""Performance & event-loop latency monitoring utilities for GameArena Bot."""
from __future__ import annotations

import asyncio
import functools
import logging
import time
from typing import Any, Callable

log = logging.getLogger("metrics")


async def event_loop_lag_monitor(interval_seconds: float = 1.0, lag_threshold_seconds: float = 0.4) -> None:
    """Continuously measures asyncio event-loop scheduling latency.
    
    If synchronous code blocks the event loop, the sleep wakes up late, and this monitor
    flags the exact lag in seconds.
    """
    log.info("[METRICS] Event loop lag monitor active (check every %.1fs, threshold %.2fs)",
             interval_seconds, lag_threshold_seconds)
    while True:
        try:
            t0 = time.perf_counter()
            await asyncio.sleep(interval_seconds)
            elapsed = time.perf_counter() - t0
            lag = elapsed - interval_seconds
            if lag >= lag_threshold_seconds:
                log.warning("[EVENT_LOOP_LAG] ⚠️ Event loop blocked! Lag: %.3fs (total sleep took %.3fs)", lag, elapsed)
            else:
                log.debug("[EVENT_LOOP_OK] lag=%.4fs", lag)
        except asyncio.CancelledError:
            log.info("[METRICS] Event loop lag monitor stopped")
            raise
        except Exception:  # noqa: BLE001
            log.exception("[METRICS] Error in event loop lag monitor (recovering)")
            await asyncio.sleep(1.0)


def timed_handler(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wraps an async Telegram update handler to log its execution latency."""
    name = getattr(fn, "__qualname__", getattr(fn, "__name__", str(fn)))

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        try:
            return await fn(*args, **kwargs)
        finally:
            elapsed = time.perf_counter() - t0
            if elapsed > 1.0:
                log.warning("[HANDLER_SLOW] ⚠️ %s executed in %.3fs (> 1.0s threshold)", name, elapsed)
            else:
                log.debug("[HANDLER_OK] %s executed in %.3fs", name, elapsed)

    return wrapper


def install_handler_metrics(app: Any) -> None:
    """Instruments all handlers registered on the PTB application with execution timers."""
    count = 0
    for group_handlers in app.handlers.values():
        for handler in group_handlers:
            if hasattr(handler, "callback") and callable(handler.callback):
                # Avoid double-wrapping
                if not getattr(handler.callback, "_is_timed", False):
                    wrapped = timed_handler(handler.callback)
                    wrapped._is_timed = True  # type: ignore[attr-defined]
                    handler.callback = wrapped
                    count += 1
    log.info("[METRICS] Installed performance timers on %d Telegram handlers", count)
