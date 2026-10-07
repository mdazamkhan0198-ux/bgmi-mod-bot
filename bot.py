import os
import sqlite3
import datetime
import logging
import requests
from telegram import Update, ChatPermissions
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes
)

logging.basicConfig(level=logging.INFO)

# --- CONFIGURATION ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
SIGHTENGINE_USER = os.getenv("SIGHTENGINE_USER", "")
SIGHTENGINE_SECRET = os.getenv("SIGHTENGINE_SECRET", "")

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect("group_bot.db")
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS notes (chat_id INT, name TEXT, content TEXT, PRIMARY KEY (chat_id, name))")
    c.execute("CREATE TABLE IF NOT EXISTS filters (chat_id INT, keyword TEXT, reply TEXT, PRIMARY KEY (chat_id, keyword))")
    c.execute("CREATE TABLE IF NOT EXISTS warns (chat_id INT, user_id INT, count INT, PRIMARY KEY (chat_id, user_id))")
    c.execute("CREATE TABLE IF NOT EXISTS feds (fed_id TEXT PRIMARY KEY, name TEXT, owner_id INT)")
    c.execute("CREATE TABLE IF NOT EXISTS fed_groups (chat_id INT PRIMARY KEY, fed_id TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS fed_bans (fed_id TEXT, user_id INT, reason TEXT, PRIMARY KEY (fed_id, user_id))")
    c.execute("CREATE TABLE IF NOT EXISTS bans (chat_id INT, user_id INT, reason TEXT, PRIMARY KEY (chat_id, user_id))")
    c.execute("CREATE TABLE IF NOT EXISTS log_channels (chat_id INT PRIMARY KEY, log_channel_id INT)")
    c.execute("CREATE TABLE IF NOT EXISTS stats (chat_id INT PRIMARY KEY, msg_count INT DEFAULT 0)")
    c.execute("CREATE TABLE IF NOT EXISTS reports (chat_id INT, reporter_id INT, target_id INT, timestamp TEXT)")
    conn.commit()
    conn.close()

init_db()

def get_db():
    return sqlite3.connect("group_bot.db")

# --- LOGGER HELPER ---
async def send_log(context: ContextTypes.DEFAULT_TYPE, chat_id: int, log_text: str):
    db = get_db()
    res = db.execute("SELECT log_channel_id FROM log_channels WHERE chat_id=?", (chat_id,)).fetchone()
    if res and res[0]:
        try:
            await context.bot.send_message(chat_id=res[0], text=log_text, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Failed to send log to channel {res[0]}: {e}")

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if chat.type == "private":
        return True
    member = await context.bot.get_chat_member(chat.id, user.id)
    return member.status in ["administrator", "creator"]

# --- LOG CHANNEL COMMAND ---
async def set_log_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    if not context.args:
        await update.message.reply_text("Usage: /setlog <Channel_ID>\nExample: /setlog -1001234567890", parse_mode="Markdown")
        return

    log_id = int(context.args[0])
    db = get_db()
    db.execute("INSERT OR REPLACE INTO log_channels VALUES (?, ?)", (update.effective_chat.id, log_id))
    db.commit()
    await update.message.reply_text(f"✅ Log channel linked to ID: `{log_id}`", parse_mode="Markdown")
    await send_log(context, update.effective_chat.id, f"<b>📢 Log System Activated</b>\nConnected to group: <i>{update.effective_chat.title}</i>")

# --- MODERATION COMMANDS ---
async def save_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /save <notename> <content>")
        return
    name, content = context.args[0].lower(), " ".join(context.args[1:])
    db = get_db()
    db.execute("INSERT OR REPLACE INTO notes VALUES (?, ?, ?)", (update.effective_chat.id, name, content))
    db.commit()
    await update.message.reply_text(f"Saved note '{name}'.")

async def get_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    name = context.args[0].lower()
    db = get_db()
    res = db.execute("SELECT content FROM notes WHERE chat_id=? AND name=?", (update.effective_chat.id, name)).fetchone()
    if res:
        await update.message.reply_text(res[0])

async def set_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /filter <keyword> <reply>")
        return
    keyword, reply = context.args[0].lower(), " ".join(context.args[1:])
    db = get_db()
    db.execute("INSERT OR REPLACE INTO filters VALUES (?, ?, ?)", (update.effective_chat.id, keyword, reply))
    db.commit()
    await update.message.reply_text(f"Filter set for '{keyword}'.")

async def warn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    msg = update.message
    chat_id = update.effective_chat.id
    target_id, target_name = None, None

    if msg.reply_to_message:
        target = msg.reply_to_message.from_user
        target_id, target_name = target.id, target.mention_html()
        if msg.text.startswith("/dwarn"):
            await msg.reply_to_message.delete()
    elif context.args and context.args[0].isdigit():
        target_id = int(context.args[0])
        target_name = f"<a href='tg://user?id={target_id}'>{target_id}</a>"

    if target_id:
        db = get_db()
        res = db.execute("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (chat_id, target_id)).fetchone()
        count = (res[0] + 1) if res else 1

        if count >= 3:
            db.execute("DELETE FROM warns WHERE chat_id=? AND user_id=?", (chat_id, target_id))
            await context.bot.ban_chat_member(chat_id, target_id)
            await msg.reply_text(f"{target_name} reached 3 warnings and was banned.", parse_mode="HTML")
            log_text = f"<b>🚫 BAN (3 WARNS)</b>\n<b>User:</b> {target_name}\n<b>Admin:</b> {update.effective_user.mention_html()}"
        else:
            db.execute("INSERT OR REPLACE INTO warns VALUES (?, ?, ?)", (chat_id, target_id, count))
            await msg.reply_text(f"Warned {target_name} ({count}/3).", parse_mode="HTML")
            log_text = f"<b>⚠️ WARN ({count}/3)</b>\n<b>User:</b> {target_name}\n<b>Admin:</b> {update.effective_user.mention_html()}"

        db.commit()
        await send_log(context, chat_id, log_text)
        
async def unwarn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    msg = update.message
    chat_id = update.effective_chat.id
    target_id, target_name = None, None

    if msg.reply_to_message:
        target = msg.reply_to_message.from_user
        target_id, target_name = target.id, target.mention_html()
    elif context.args and context.args[0].isdigit():
        target_id = int(context.args[0])
        target_name = f"<a href='tg://user?id={target_id}'>{target_id}</a>"
    else:
        await msg.reply_text("Please reply to a user or pass their numerical User ID.")
        return

    db = get_db()
    res = db.execute("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (chat_id, target_id)).fetchone()
    
    if not res or res[0] <= 0:
        await msg.reply_text("This user has no active warnings.")
        return

    new_count = res[0] - 1
    if new_count == 0:
        db.execute("DELETE FROM warns WHERE chat_id=? AND user_id=?", (chat_id, target_id))
    else:
        db.execute("UPDATE warns SET count=? WHERE chat_id=? AND user_id=?", (new_count, chat_id, target_id))
    
    db.commit()
    await msg.reply_text(f"Removed 1 warning from {target_name}. Current warnings: ({new_count}/3).", parse_mode="HTML")

    log_text = f"<b>⚠️ UNWARN ({new_count}/3)</b>\n<b>User:</b> {target_name}\n<b>Admin:</b> {update.effective_user.mention_html()}"
    await send_log(context, chat_id, log_text)
    

async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    target_id = update.message.reply_to_message.from_user.id if update.message.reply_to_message else int(context.args[0])
    target_name = update.message.reply_to_message.from_user.mention_html() if update.message.reply_to_message else str(target_id)
    chat_id = update.effective_chat.id

    await context.bot.restrict_chat_member(chat_id, target_id, permissions=ChatPermissions(can_send_messages=False))
    await update.message.reply_text("User muted.")

    log_text = f"<b>🔇 MUTE</b>\n<b>User:</b> {target_name}\n<b>Admin:</b> {update.effective_user.mention_html()}"
    await send_log(context, chat_id, log_text)

async def tmute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context) or not context.args: return
    duration_str = context.args[0]
    unit = duration_str[-1]
    amount = int(duration_str[:-1])
    delta = datetime.timedelta(minutes=amount) if unit == 'm' else datetime.timedelta(hours=amount)
    until = datetime.datetime.now() + delta

    target_id = update.message.reply_to_message.from_user.id if update.message.reply_to_message else int(context.args[0])
    chat_id = update.effective_chat.id

    await context.bot.restrict_chat_member(chat_id, target_id, permissions=ChatPermissions(can_send_messages=False), until_date=until)
    await update.message.reply_text(f"Muted for {duration_str}.")

    log_text = f"<b>⏳ TEMP MUTE</b>\n<b>User ID:</b> {target_id}\n<b>Duration:</b> {duration_str}\n<b>Admin:</b> {update.effective_user.mention_html()}"
    await send_log(context, chat_id, log_text)

async def unmute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    msg = update.message
    chat_id = update.effective_chat.id
    target_id, target_name = None, None

    if msg.reply_to_message:
        target = msg.reply_to_message.from_user
        target_id, target_name = target.id, target.mention_html()
    elif context.args and context.args[0].isdigit():
        target_id = int(context.args[0])
        target_name = f"<a href='tg://user?id={target_id}'>{target_id}</a>"
    else:
        await msg.reply_text("Please reply to a user or pass their numerical User ID.")
        return

    # Restore all standard chat permissions
    await context.bot.restrict_chat_member(
        chat_id, 
        target_id, 
        permissions=ChatPermissions(
            can_send_messages=True,
            can_send_media_messages=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True
        )
    )
    await msg.reply_text(f"Unmuted {target_name}.", parse_mode="HTML")

    log_text = f"<b>🔊 UNMUTE</b>\n<b>User:</b> {target_name}\n<b>Admin:</b> {update.effective_user.mention_html()}"
    await send_log(context, chat_id, log_text)
    

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    msg = update.message
    chat_id = update.effective_chat.id
    target_id, target_name = None, None
    reason = "No reason provided"

    if msg.reply_to_message:
        target = msg.reply_to_message.from_user
        target_id, target_name = target.id, target.mention_html()
        if context.args: reason = " ".join(context.args)
        if msg.text.startswith("/dban"):
            await msg.reply_to_message.delete()
    elif context.args:
        arg = context.args[0]
        reason = " ".join(context.args[1:]) if len(context.args) > 1 else reason
        if arg.isdigit():
            target_id = int(arg)
            target_name = f"<a href='tg://user?id={target_id}'>{target_id}</a>"
        else:
            await msg.reply_text("Please reply to a user or pass their numerical User ID.")
            return

    if target_id:
        await context.bot.ban_chat_member(chat_id, target_id)
        db = get_db()
        db.execute("INSERT OR REPLACE INTO bans VALUES (?, ?, ?)", (chat_id, target_id, reason))
        db.commit()

        await msg.reply_text(f"Banned {target_name}. Reason: {reason}", parse_mode="HTML")
        log_text = f"<b>🔨 BAN</b>\n<b>User:</b> {target_name} ({target_id})\n<b>Admin:</b> {update.effective_user.mention_html()}\n<b>Reason:</b> {reason}"
        await send_log(context, chat_id, log_text)

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    user_id = int(context.args[0])
    chat_id = update.effective_chat.id
    await context.bot.unban_chat_member(chat_id, user_id)
    await update.message.reply_text("User unbanned.")

    log_text = f"<b>🔓 USER UNBANNED</b>\n<b>User ID:</b> {user_id}\n<b>Admin:</b> {update.effective_user.mention_html()}"
    await send_log(context, chat_id, log_text)

async def whyban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /whyban <user_id>")
        return

    user_id = int(context.args[0])
    chat_id = update.effective_chat.id
    db = get_db()

    res = db.execute("SELECT reason FROM bans WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
    
    if res:
        await update.message.reply_text(f"<b>Ban Reason for {user_id}:</b> {res[0]}", parse_mode="HTML")
    else:
        fed = db.execute("SELECT fed_id FROM fed_groups WHERE chat_id=?", (chat_id,)).fetchone()
        if fed:
            fres = db.execute("SELECT reason FROM fed_bans WHERE fed_id=? AND user_id=?", (fed[0], user_id)).fetchone()
            if fres:
                await update.message.reply_text(f"<b>Fed Ban Reason for {user_id}:</b> {fres[0]}", parse_mode="HTML")
                return

        await update.message.reply_text("No ban record found for this user.")

# --- FEDERATION COMMANDS ---
async def new_fed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    fed_name = " ".join(context.args)
    fed_id = str(datetime.datetime.now().timestamp())
    db = get_db()
    db.execute("INSERT INTO feds VALUES (?, ?, ?)", (fed_id, fed_name, update.effective_user.id))
    db.commit()
    await update.message.reply_text(f"Created Federation '{fed_name}' with ID: `{fed_id}`", parse_mode="Markdown")

async def join_fed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    if not context.args: return
    fed_id = context.args[0]
    db = get_db()
    db.execute("INSERT OR REPLACE INTO fed_groups VALUES (?, ?)", (update.effective_chat.id, fed_id))
    db.commit()
    await update.message.reply_text("Group linked to Federation.")

async def fban_user(update: Update, context: ContextTypes.DEFAULT_TYPE, ban=True):
    if not context.args: return
    user_id = int(context.args[0])
    chat_id = update.effective_chat.id
    db = get_db()
    fed = db.execute("SELECT fed_id FROM fed_groups WHERE chat_id=?", (chat_id,)).fetchone()
    if not fed:
        await update.message.reply_text("Group not linked to any Federation.")
        return
    fed_id = fed[0]
    if ban:
        db.execute("INSERT OR REPLACE INTO fed_bans VALUES (?, ?, ?, ?)", (fed_id, user_id, "Fed Banned", update.effective_user.id))
        db.commit()
        await send_log(context, chat_id, f"<b>🔒 FED BAN</b>\n<b>User ID:</b> {user_id}\n<b>Admin:</b> {update.effective_user.mention_html()}")
        await update.message.reply_text("User fed banned.")
    else:
        db.execute("DELETE FROM fed_bans WHERE fed_id=? AND user_id=?", (fed_id, user_id))
        db.commit()
        await update.message.reply_text("User unfed banned.")

# --- AUTOMATION & FILTERS ---
async def check_nsfw(file_id, context):
    if not SIGHTENGINE_USER or not SIGHTENGINE_SECRET: return False
    file = await context.bot.get_file(file_id)
    url = f"https://api.sightengine.com/1.0/check.json?url={file.file_path}&models=nudity-2.0&api_user={SIGHTENGINE_USER}&api_secret={SIGHTENGINE_SECRET}"
    r = requests.get(url).json()
    if r.get("status") == "success":
        nudity = r.get("nudity", {})
        if nudity.get("sexual_activity", 0) > 0.5 or nudity.get("sexual_display", 0) > 0.5:
            return True
    return False

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message: return
    msg = update.effective_message
    chat_id = update.effective_chat.id
    user = update.effective_user

    # Stats
    db = get_db()
    db.execute("INSERT INTO stats VALUES (?, 1) ON CONFLICT(chat_id) DO UPDATE SET msg_count = msg_count + 1", (chat_id,))
    db.commit()

    # Fed Check on Join
    fed = db.execute("SELECT fed_id FROM fed_groups WHERE chat_id=?", (chat_id,)).fetchone()
    if fed:
        is_fbanned = db.execute("SELECT user_id FROM fed_bans WHERE fed_id=? AND user_id=?", (fed[0], user.id)).fetchone()
        if is_fbanned:
            await context.bot.ban_chat_member(chat_id, user.id)
            await send_log(context, chat_id, f"<b>🛡️ FED BAN ENFORCED</b>\n<b>User:</b> {user.mention_html()} ({user.id})")
            return

    # Delete Deleted Accounts
    if user and not user.first_name:
        await context.bot.ban_chat_member(chat_id, user.id)
        return

    # Filters
    if msg.text:
        text = msg.text.lower()
        res = db.execute("SELECT reply FROM filters WHERE chat_id=? AND keyword=?", (chat_id, text)).fetchone()
        if res:
            await msg.reply_text(res[0])

    # NSFW Check for Media
    if SIGHTENGINE_USER and (msg.photo or msg.sticker):
        file_id = msg.photo[-1].file_id if msg.photo else msg.sticker.file_id
        if await check_nsfw(file_id, context):
            await msg.delete()
            await send_log(context, chat_id, f"<b>🔞 NSFW Auto-Deleted</b>\n<b>User:</b> {user.mention_html()}")

# --- UTILITY COMMANDS ---
async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Pong! 🏓 Bot is active.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot is up and running 24/7!")

def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logging.error("BOT_TOKEN is not set properly!")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("setlog", set_log_channel))
    app.add_handler(CommandHandler("save", save_note))
    app.add_handler(CommandHandler("get", get_note))
    app.add_handler(CommandHandler("filter", set_filter))
    app.add_handler(CommandHandler(["warn", "dwarn"], warn_user))
    app.add_handler(CommandHandler(["unwarn", "rmwarn"], unwarn_user))
    app.add_handler(CommandHandler("mute", mute_user))
    app.add_handler(CommandHandler("tmute", tmute_user))
    app.add_handler(CommandHandler("unmute", unmute_user))
    app.add_handler(CommandHandler(["ban", "dban"], ban_user))
    app.add_handler(CommandHandler("unban", unban_user))
    app.add_handler(CommandHandler("whyban", whyban_user))
    app.add_handler(CommandHandler("newfed", new_fed))
    app.add_handler(CommandHandler("joinfed", join_fed))
    app.add_handler(CommandHandler("fban", fban_user))

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    print("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
    
