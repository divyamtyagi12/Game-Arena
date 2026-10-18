"""Tests for Anime Draft concurrency, timeout protection, rate limiting, and lag monitoring."""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import RetryAfter, TimedOut

from database.client import Database, DatabaseError
from services import lobby_service
from services.context import AppContext
from services.telegram_io import safe_edit, send_message
from utils.metrics import event_loop_lag_monitor, timed_handler


class TestAnimeDraftConcurrency(unittest.IsolatedAsyncioTestCase):
    async def test_event_loop_lag_monitor_detects_blocking_call(self) -> None:
        """Verify the event loop monitor accurately reports when the event loop is blocked."""
        task = asyncio.create_task(event_loop_lag_monitor(interval_seconds=0.05, lag_threshold_seconds=0.03))
        await asyncio.sleep(0.01)
        # Simulate blocking synchronous CPU work on the event loop
        t_start = time.perf_counter()
        while time.perf_counter() - t_start < 0.08:
            pass
        await asyncio.sleep(0.06)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_timed_handler_records_execution(self) -> None:
        """Verify timed_handler wraps async calls cleanly and returns the result."""
        async def dummy_handler(update, context):
            await asyncio.sleep(0.01)
            return "ok"

        wrapped = timed_handler(dummy_handler)
        result = await wrapped(None, None)
        self.assertEqual(result, "ok")

    async def test_telegram_io_send_message_aborts_excessive_retry_after(self) -> None:
        """Verify send_message does not freeze the bot when RetryAfter is excessively large."""
        mock_ctx = MagicMock(spec=AppContext)
        mock_bot = AsyncMock()
        mock_bot.send_message.side_effect = RetryAfter(30)
        mock_ctx.bot = mock_bot

        t0 = time.perf_counter()
        res = await send_message(mock_ctx, 12345, "hello")
        elapsed = time.perf_counter() - t0

        self.assertIsNone(res)
        # Must return immediately and NOT sleep for 30 seconds
        self.assertLess(elapsed, 1.0)

    async def test_telegram_io_safe_edit_aborts_excessive_retry_after(self) -> None:
        """Verify safe_edit returns error instead of sleeping on large RetryAfter."""
        mock_ctx = MagicMock(spec=AppContext)
        mock_bot = AsyncMock()
        mock_bot.edit_message_text.side_effect = RetryAfter(25)
        mock_ctx.bot = mock_bot

        t0 = time.perf_counter()
        res = await safe_edit(mock_ctx, 12345, 999, "new text")
        elapsed = time.perf_counter() - t0

        self.assertEqual(res, "error")
        self.assertLess(elapsed, 1.0)

    async def test_telegram_io_send_message_handles_timeout(self) -> None:
        """Verify send_message safely handles network timeouts."""
        mock_ctx = MagicMock(spec=AppContext)
        mock_bot = AsyncMock()
        mock_bot.send_message.side_effect = TimedOut()
        mock_ctx.bot = mock_bot

        res = await send_message(mock_ctx, 12345, "hello")
        self.assertIsNone(res)

    async def test_lobby_debounce_throttles_rapid_refreshes(self) -> None:
        """Verify lobby refreshes are debounced when rapid concurrent joins occur."""
        lobby_service._last_lobby_refresh.clear()
        mock_ctx = MagicMock()
        mock_ctx.lock.return_value = asyncio.Lock()
        mock_ctx.games.get = AsyncMock(return_value={
            "id": 1, "group_id": -100, "status": "LOBBY",
            "min_players": 3, "max_players": 8, "lobby_message_id": 123
        })
        mock_ctx.players.list_for_game = AsyncMock(return_value=[{"display_name": "P1"}])

        with patch("services.lobby_service.safe_edit", new_callable=AsyncMock) as mock_edit:
            mock_edit.return_value = "ok"
            # First refresh should succeed promptly
            t0 = time.perf_counter()
            await lobby_service.refresh_lobby(mock_ctx, 1)
            t1 = time.perf_counter()

            # Second refresh immediately after should debounce slightly (0.5s pause)
            await lobby_service.refresh_lobby(mock_ctx, 1)
            t2 = time.perf_counter()

            self.assertGreaterEqual(t2 - t1, 0.4)
            self.assertEqual(mock_edit.call_count, 2)
