from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from handlers.common import get_ctx


async def setimage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner only: reply to a photo with /setimage <character_id> to attach artwork."""
    ctx = get_ctx(context)
    message, user = update.effective_message, update.effective_user
    if user.id not in ctx.settings.owner_ids:
        return
    reply = message.reply_to_message
    if not context.args or not reply or not reply.photo:
        return await message.reply_text("Usage: reply to a photo with /setimage <character_id>")
    char_id = context.args[0].lower()
    char = ctx.catalog.by_id.get(char_id)
    if not char:
        return await message.reply_text("Unknown character id.")
    file_id = reply.photo[-1].file_id
    await ctx.characters.set_file_id(char_id, file_id)
    char.image_file_id = file_id
    await message.reply_text(f"✅ Artwork saved for {char.name}.")


async def reload_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner only: reload character catalog from database."""
    ctx = get_ctx(context)
    message, user = update.effective_message, update.effective_user
    if user.id not in ctx.settings.owner_ids:
        return
    from database.seed import sync_characters
    if ctx.settings.auto_seed:
        await sync_characters(ctx.db)
    ctx.catalog = await ctx.characters.load_catalog()
    await message.reply_text(f"✅ Character catalog reloaded ({len(ctx.catalog)} characters active).")

