"""Minimal stand-ins for third-party modules so the suite runs in environments without pip access.

Only installed when the real package is missing. They implement just enough surface for import-time and for the
fakes used in the tests — they are NOT a Telegram/Supabase emulator, and handler tests that use them verify our
logic, not python-telegram-bot's behaviour.
"""
from __future__ import annotations

import importlib.util
import sys
import types


def _mod(name: str, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


class _Anything:
    def __init__(self, *a, **k):
        self.args, self.kwargs = a, k

    def __getattr__(self, _):
        return _Anything()


def install() -> None:
    if importlib.util.find_spec("telegram") is None:
        class TelegramError(Exception): ...
        class NetworkError(TelegramError): ...
        class TimedOut(NetworkError): ...
        class BadRequest(TelegramError): ...
        class Forbidden(TelegramError): ...
        class RetryAfter(TelegramError):
            def __init__(self, retry_after=1): super().__init__("retry"); self.retry_after = retry_after

        class Btn:
            def __init__(self, text, callback_data=None, url=None, **_):
                self.text, self.callback_data, self.url = text, callback_data, url
            def __repr__(self): return f"Btn({self.text!r},{self.callback_data!r})"

        class BotCommand:
            def __init__(self, command, description): self.command, self.description = command, description

        class Markup:
            def __init__(self, inline_keyboard):
                self.inline_keyboard = [list(r) for r in inline_keyboard]

        class ChatType:
            PRIVATE, GROUP, SUPERGROUP, CHANNEL = "private", "group", "supergroup", "channel"

        class ParseMode:
            HTML = "HTML"

        class ChatAction:
            TYPING = "typing"

        class ContextTypes:
            DEFAULT_TYPE = object

        _mod("telegram", InlineKeyboardButton=Btn, InlineKeyboardMarkup=Markup, Update=_Anything, Bot=_Anything,
             BotCommand=BotCommand, Message=_Anything, InputMediaPhoto=_Anything)
        _mod("telegram.constants", ChatType=ChatType, ParseMode=ParseMode, ChatAction=ChatAction)
        _mod("telegram.error", TelegramError=TelegramError, NetworkError=NetworkError, TimedOut=TimedOut,
             BadRequest=BadRequest, Forbidden=Forbidden, RetryAfter=RetryAfter)
        _mod("telegram.ext", ContextTypes=ContextTypes, AIORateLimiter=_Anything, Application=_Anything,
             ApplicationBuilder=_Anything, CallbackQueryHandler=_Anything, ChatMemberHandler=_Anything,
             CommandHandler=_Anything, MessageHandler=_Anything, filters=_Anything())
    if importlib.util.find_spec("postgrest") is None:
        class APIError(Exception):
            def __init__(self, code="", message=""): super().__init__(message); self.code = code
        _mod("postgrest")
        _mod("postgrest.exceptions", APIError=APIError)
    if importlib.util.find_spec("supabase") is None:
        class ClientOptions:
            def __init__(self, **k): self.__dict__.update(k)
        pkg = _mod("supabase", Client=_Anything, create_client=lambda *a, **k: _Anything())
        pkg.__path__ = []
        _mod("supabase.client", ClientOptions=ClientOptions)
    if importlib.util.find_spec("httpx") is None:
        class TransportError(Exception): ...
        class TimeoutException(Exception): ...
        _mod("httpx", TransportError=TransportError, TimeoutException=TimeoutException)
    if importlib.util.find_spec("pytest") is None:
        import contextlib, unittest
        class _Mark:
            def __getattr__(self, n):
                return lambda *a, **k: (a[0] if a and callable(a[0]) and not k else (lambda f: f))
        class _Info: value = None
        @contextlib.contextmanager
        def raises(exc, match=None):
            info = _Info()
            try:
                yield info
            except exc as e:
                info.value = e
                return
            raise AssertionError(f"DID NOT RAISE {exc}")
        def skip(msg=""): raise unittest.SkipTest(msg)
        _mod("pytest", mark=_Mark(), raises=raises, skip=skip,
             fixture=lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda f: f)))
