"""Thin async wrapper around the (synchronous) supabase-py client.

Calls run in a worker thread so they never block the event loop; transient
network errors are retried with backoff. Credentials are never logged.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import time
from typing import Any, Callable

import httpx
from postgrest.exceptions import APIError
from supabase import Client, create_client
from supabase.client import ClientOptions

log = logging.getLogger(__name__)


class DatabaseError(RuntimeError):
    """Supabase/network failure after retries."""


def is_unique_violation(exc: BaseException) -> bool:
    return isinstance(exc, APIError) and str(getattr(exc, "code", "")) == "23505"


class Database:
    def __init__(self, url: str, key: str, max_workers: int = 25, query_timeout: float = 15.0) -> None:
        opts = ClientOptions(postgrest_client_timeout=query_timeout)
        self._client: Client = create_client(url, key, options=opts)
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="db-pool"
        )
        self._query_timeout = query_timeout

    async def exec(self, build: Callable[[Client], Any], *, retries: int = 3, timeout: float | None = None) -> Any:
        """`build(client)` returns a postgrest request; we execute it in a dedicated thread pool and return `.data`."""
        effective_timeout = timeout or self._query_timeout

        def _run() -> Any:
            return build(self._client).execute().data

        # Caller identification for structured slow query logging
        caller_name = "unknown"
        try:
            stack = inspect.stack()
            if len(stack) > 1:
                frame = stack[1]
                caller_name = f"{frame.function}:{frame.lineno}"
        except Exception:
            pass

        delay = 0.5
        loop = asyncio.get_running_loop()
        for attempt in range(1, retries + 1):
            t0 = time.perf_counter()
            try:
                task = loop.run_in_executor(self._executor, _run)
                data = await asyncio.wait_for(task, timeout=effective_timeout)
                elapsed = time.perf_counter() - t0
                if elapsed > 1.0:
                    log.warning("[DB_SLOW] %s took %.3fs (attempt %d/%d)", caller_name, elapsed, attempt, retries)
                else:
                    log.debug("[DB_OK] %s took %.3fs", caller_name, elapsed)
                return data
            except (httpx.TransportError, httpx.TimeoutException, asyncio.TimeoutError) as exc:
                elapsed = time.perf_counter() - t0
                exc_name = type(exc).__name__
                if attempt == retries:
                    log.error("[DB_FAIL] %s failed after %d attempts (last %.3fs, err=%s)", caller_name, retries, elapsed, exc_name)
                    raise DatabaseError(f"Supabase unreachable or timed out ({caller_name}): {exc_name}") from exc
                log.warning("[DB_RETRY] %s network/timeout error (%s) after %.3fs, retry %d/%d in %.1fs",
                            caller_name, exc_name, elapsed, attempt, retries, delay)
                await asyncio.sleep(delay)
                delay *= 2

    async def rpc(self, fn: str, params: dict[str, Any], *, timeout: float | None = None) -> Any:
        return await self.exec(lambda c: c.rpc(fn, params), timeout=timeout)
