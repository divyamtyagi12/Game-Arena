"""GAME ARENA (ANIME DRAFT + NUMBER WARS + IPL DRAFT) — Telegram bot entry point.

    python bot.py            # long polling (default) or webhook, see RUN_MODE
"""
from __future__ import annotations

import dataclasses
import logging
import sys

from telegram import BotCommand, Update
from telegram.ext import (AIORateLimiter, Application, ApplicationBuilder, CallbackQueryHandler,
                          ChatMemberHandler, CommandHandler, MessageHandler, filters)

from config import ConfigError, load_settings
from database.client import Database
from database.seed import sync_characters
from handlers import admin, arena, draft, info, leaderboard, lobby, number_wars, start, system
from services import draft_service
from services.context import AppContext
from ipl_draft.handlers import callbacks as ipl_callbacks, commands as ipl_commands
from ipl_draft.runtime import IplRuntime
from ipl_draft.services import simulation_worker as ipl_worker
from services.recovery import recover_games
from utils.logging_setup import setup_logging
from utils.metrics import event_loop_lag_monitor, install_handler_metrics

log = logging.getLogger("anime_draft")
ALLOWED_UPDATES = ["message", "callback_query", "my_chat_member"]
COMMANDS = [
    BotCommand("start", "Pick a game & create a lobby (group) / register (DM)"),
    BotCommand("help", "Show commands"),
    BotCommand("rules", "How the game works"),
    BotCommand("team", "Show your drafted team (DM)"),
    BotCommand("draft", "Resend your current draft prompt (DM)"),
    BotCommand("table", "League standings"),
    BotCommand("status", "Current game status"),
    BotCommand("leaderboard", "Global & group rankings"),
    BotCommand("nwrules", "Number Wars: how it works"),
    BotCommand("nwstats", "Number Wars: your rating & record"),
    BotCommand("cancelgame", "Cancel the game (host/admin)"),
    BotCommand("ipl", "IPL Draft: open a lobby (group)"),
    BotCommand("iplrules", "IPL Draft: how it works"),
    BotCommand("iplteam", "IPL Draft: your squad (DM)"),
    BotCommand("ipltable", "IPL Draft: points table"),
    BotCommand("iplfixtures", "IPL Draft: fixtures"),
    BotCommand("iplhistory", "IPL Draft: match history"),
    BotCommand("iplstats", "IPL Draft: your stats & career record"),
    BotCommand("stats", "Your stats in all games"),
]


async def post_init(app: Application) -> None:
    ctx: AppContext = app.bot_data["ctx"]
    ctx.bot = app.bot
    ctx.bot_username = (await app.bot.get_me()).username or ""
    await ctx.db.exec(lambda c: c.table("games").select("id").limit(1))  # fails fast if schema missing
    await ctx.db.exec(lambda c: c.table("nw_matches").select("id").limit(1))  # …or if number_wars.sql wasn't run
    if ctx.settings.auto_seed:
        await sync_characters(ctx.db)
    ctx.catalog = await ctx.characters.load_catalog()
    if len(ctx.catalog) == 0:
        raise RuntimeError("No characters in the database. Run database/schema.sql, then AUTO_SEED=true "
                           "or `python -m scripts.seed_characters`.")
    await app.bot.set_my_commands(COMMANDS)
    await recover_games(ctx)
    await start_ipl(app)
    ctx.spawn(event_loop_lag_monitor(interval_seconds=1.0, lag_threshold_seconds=0.4), name="event-loop-monitor")
    if ctx.settings.draft_timeout_minutes > 0:
        ctx.spawn(draft_service.autopick_loop(ctx), name="autopick-loop")
    log.info("ANIME DRAFT ready as @%s (%d characters)", ctx.bot_username, len(ctx.catalog))


async def start_ipl(app: Application) -> None:
    """IPL Draft is optional infrastructure: if its tables are missing the other two games keep working."""
    rt: IplRuntime = app.bot_data["ipl"]
    if not rt.s.enabled:
        log.info("IPL DRAFT disabled (IPL_ENABLED=false)")
        return
    try:
        await rt.ctx.db.exec(lambda c: c.table("ipl_tournaments").select("id").limit(1))
        pool = await rt.repo.pool_summary()
    except Exception as exc:  # noqa: BLE001
        log.error("IPL DRAFT disabled: database/ipl_draft.sql has not been applied (%s)", type(exc).__name__)
        rt.s = dataclasses.replace(rt.s, enabled=False)
        return
    if int(pool.get("eligible") or 0) == 0:
        log.warning("IPL DRAFT: the player table is empty — run `python -m scripts.import_ipl_players` before starting a draft")
    else:
        log.info("IPL DRAFT pool: %s eligible players (%s verified from real stats)", pool.get("eligible"), pool.get("verified"))
    await ipl_worker.recover(rt)
    rt.ctx.spawn(ipl_worker.run_forever(rt), name="ipl-worker")


def register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", start.start_command))
    app.add_handler(CommandHandler("help", start.help_command))
    app.add_handler(CommandHandler("rules", start.rules_command))
    app.add_handler(CommandHandler("team", info.team_command))
    app.add_handler(CommandHandler("table", info.table_command))
    app.add_handler(CommandHandler("status", info.status_command))
    app.add_handler(CommandHandler("draft", draft.draft_command))
    app.add_handler(CommandHandler("cancelgame", lobby.cancelgame_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard.leaderboard_command))
    app.add_handler(CommandHandler("setimage", admin.setimage_command))
    for name, fn in (("ipl", ipl_commands.ipl_command), ("iplrules", ipl_commands.iplrules_command),
                     ("iplteam", ipl_commands.iplteam_command), ("ipltable", ipl_commands.ipltable_command),
                     ("iplfixtures", ipl_commands.iplfixtures_command), ("iplhistory", ipl_commands.iplhistory_command),
                     ("iplstats", ipl_commands.iplstats_command)):
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CommandHandler("stats", arena.stats_command))
    app.add_handler(CallbackQueryHandler(ipl_callbacks.ipl_callback, pattern=r"^ipl:"))
    app.add_handler(CommandHandler("nwrules", number_wars.nwrules_command))
    app.add_handler(CommandHandler("nwstats", number_wars.nwstats_command))
    app.add_handler(CallbackQueryHandler(arena.arena_callback, pattern=r"^ga:"))
    app.add_handler(CallbackQueryHandler(number_wars.nw_callback, pattern=r"^nw:"))
    app.add_handler(CallbackQueryHandler(number_wars.nw_leaderboard_callback, pattern=r"^nwl:"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
                                   number_wars.number_text))   # typed numbers during a round
    app.add_handler(CallbackQueryHandler(lobby.lobby_callback, pattern=r"^lb:"))
    app.add_handler(CallbackQueryHandler(draft.draft_callback, pattern=r"^dp:"))
    app.add_handler(CallbackQueryHandler(leaderboard.leaderboard_callback, pattern=r"^lbd:"))
    app.add_handler(ChatMemberHandler(system.on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(system.stale_callback))  # anything else = stale button
    app.add_error_handler(system.error_handler)


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(2)
    setup_logging(settings.log_level, settings.secrets)

    ctx = AppContext.create(settings, Database(settings.supabase_url, settings.supabase_key))
    app = (ApplicationBuilder().token(settings.telegram_token)
           .rate_limiter(AIORateLimiter(max_retries=0))
           .concurrent_updates(True)   # simultaneous button presses are handled in parallel
           .post_init(post_init).build())
    app.bot_data["ctx"] = ctx
    app.bot_data["ipl"] = IplRuntime.create(ctx)
    register_handlers(app)
    install_handler_metrics(app)

    if settings.run_mode == "webhook":
        base = settings.webhook_url.rstrip("/")
        app.run_webhook(listen=settings.webhook_listen, port=settings.webhook_port,
                        url_path=settings.webhook_path, webhook_url=f"{base}/{settings.webhook_path}",
                        secret_token=settings.webhook_secret or None, allowed_updates=ALLOWED_UPDATES)
    else:
        app.run_polling(allowed_updates=ALLOWED_UPDATES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
