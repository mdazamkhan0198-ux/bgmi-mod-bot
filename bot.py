import os
import re
import sqlite3
import datetime
import logging
import requests
from telegram import Update, ChatPermissions
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, ChatMemberHandler
)

logging.basicConfig(level=logging.INFO)

# --- CONFIGURATION ---
# Replace these strings with your actual credentials
BOT_TOKEN = "8823641804:AAE-FH80pUdwK4"
SIGHTENGINE_USER = "1721004361"
SIGHTENGINE_SECRET = "SuR4QHjJjf3CD4GwMKF9ZRsNotsAUvLh"
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
    """Sends action logs to the configured log channel for the group."""
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
        await update.message.reply_text("Usage: /setlog <Channel_ID>\nExample: `/setlog -1001234567890`", parse_mode="Markdown")
        return
    
    log_id = int(context.args[0])
    db = get_db()
    db.execute("INSERT OR REPLACE INTO log_channels VALUES (?, ?)", (update.effective_chat.id, log_id))
    db.commit()
    await update.message.reply_text(f"✅ Log channel linked to ID: `{log_id}`", parse_mode="Markdown")
    await send_log(context, update.effective_chat.id, f"<b>📜 Log System Activated</b>\nConnected to group: <i>{update.effective_chat.title}</i>")

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
    else:
        await update.message.reply_text("Note not found.")

async def set_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /filter <keyword> <reply>")
        return
    kw, reply = context.args[0].lower(), " ".join(context.args[1:])
    db = get_db()
    db.execute("INSERT OR REPLACE INTO filters VALUES (?, ?, ?)", (update.effective_chat.id, kw, reply))
    db.commit()
    await update.message.reply_text(f"Filter added for '{kw}'.")

async def warn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a message to warn the user.")
        return
    
    target = update.message.reply_to_message.from_user
    admin = update.effective_user
    chat_id = update.effective_chat.id
    
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (chat_id, target.id))
    row = cur.fetchone()
    count = (row[0] if row else 0) + 1
    db.execute("INSERT OR REPLACE INTO warns VALUES (?, ?, ?)", (chat_id, target.id, count))
    db.commit()
    
    log_text = f"<b>⚠️ USER WARNED</b>\n<b>User:</b> {target.mention_html()} ({target.id})\n<b>Admin:</b> {admin.mention_html()}\n<b>Warn Count:</b> {count}/3"
    
    if count >= 3:
        await context.bot.ban_chat_member(chat_id, target.id)
        await update.message.reply_text(f"⚠️ {target.mention_html()} reached 3 warnings and was banned.", parse_mode="HTML")
        log_text += "\n<b>Action:</b> Banned (3 Warnings Reached)"
    else:
        await update.message.reply_text(f"⚠️ Warned {target.mention_html()} ({count}/3)", parse_mode="HTML")
    
    await send_log(context, chat_id, log_text)

async def check_warns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = update.message.reply_to_message.from_user if update.message.reply_to_message else update.effective_user
    db = get_db()
    row = db.execute("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (update.effective_chat.id, target.id)).fetchone()
    count = row[0] if row else 0
    await update.message.reply_text(f"{target.first_name} has {count}/3 warnings.")

async def reset_warns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    target = update.message.reply_to_message.from_user if update.message.reply_to_message else None
    if not target: return
    chat_id = update.effective_chat.id
    db = get_db()
    db.execute("DELETE FROM warns WHERE chat_id=? AND user_id=?", (chat_id, target.id))
    db.commit()
    await update.message.reply_text(f"Warnings reset for {target.first_name}.")
    
    log_text = f"<b>🔄 WARNS RESET</b>\n<b>User:</b> {target.mention_html()} ({target.id})\n<b>Admin:</b> {update.effective_user.mention_html()}"
    await send_log(context, chat_id, log_text)

async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    user_id = context.args[0] if context.args else None
    target_name = user_id
    if not user_id and update.message.reply_to_message:
        user_id = update.message.reply_to_message.from_user.id
        target_name = update.message.reply_to_message.from_user.mention_html()
        
    if user_id:
        chat_id = update.effective_chat.id
        await context.bot.restrict_chat_member(chat_id, int(user_id), permissions=ChatPermissions(can_send_messages=False))
        await update.message.reply_text("User muted.")
        log_text = f"<b>🔇 USER MUTED</b>\n<b>User ID/Mention:</b> {target_name}\n<b>Admin:</b> {update.effective_user.mention_html()}"
        await send_log(context, chat_id, log_text)

async def tmute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context) or not context.args: return
    duration_str = context.args[0]
    unit = duration_str[-1]
    amount = int(duration_str[:-1])
    delta = datetime.timedelta(minutes=amount) if unit == 'm' else datetime.timedelta(hours=amount)
    until = datetime.datetime.now() + delta
    
    target_id = update.message.reply_to_message.from_user.id if update.message.reply_to_message else int(context.args[1])
    chat_id = update.effective_chat.id
    await context.bot.restrict_chat_member(chat_id, target_id, permissions=ChatPermissions(can_send_messages=False), until_date=until)
    await update.message.reply_text(f"Muted for {duration_str}.")
    
    log_text = f"<b>⏱️ TEMP MUTE</b>\n<b>User ID:</b> {target_id}\n<b>Duration:</b> {duration_str}\n<b>Admin:</b> {update.effective_user.mention_html()}"
    await send_log(context, chat_id, log_text)

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE, silent=False):
    if not await is_admin(update, context): return
    target = update.message.reply_to_message.from_user if update.message.reply_to_message else None
    reason = " ".join(context.args) if context.args else "No reason provided"
    chat_id = update.effective_chat.id
    
    if target:
        await context.bot.ban_chat_member(chat_id, target.id)
        if silent:
            await update.message.reply_to_message.delete()
        else:
            await update.message.reply_text(f"Banned {target.first_name}. Reason: {reason}")
        
        db = get_db()
        db.execute("INSERT OR REPLACE INTO bans VALUES (?, ?, ?)", (chat_id, target.id, reason))
        db.commit()
        
        log_text = f"<b>🔨 USER BANNED</b>\n<b>User:</b> {target.mention_html()} ({target.id})\n<b>Admin:</b> {update.effective_user.mention_html()}\n<b>Reason:</b> {reason}\n<b>Silent:</b> {silent}"
        await send_log(context, chat_id, log_text)

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context) or not context.args: return
    user_id = int(context.args[0])
    chat_id = update.effective_chat.id
    await context.bot.unban_chat_member(chat_id, user_id)
    await update.message.reply_text("User unbanned.")
    
    log_text = f"<b>🔓 USER UNBANNED</b>\n<b>User ID:</b> {user_id}\n<b>Admin:</b> {update.effective_user.mention_html()}"
    await send_log(context, chat_id, log_text)

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
    if not await is_admin(update, context) or not context.args: return
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
        await update.message.reply_text(f"User {user_id} un-FBanned.")
        log_text = f"<b>🌐 FEDERATION UNBAN</b>\n<b>User ID:</b> {user_id}\n<b>Admin:</b> {update.effective_user.mention_html()}"
    db.commit()
    await send_log(context, chat_id, log_text)

# --- TRACK JOIN / LEFT EVENTS ---

async def track_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Logs when users join or leave the chat."""
    result = update.chat_member
    chat_id = update.effective_chat.id
    user = result.new_chat_member.user
    
    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status
    
    if old_status in ["left", "kicked"] and new_status in ["member", "administrator"]:
        log_text = f"<b>📥 MEMBER JOINED</b>\n<b>User:</b> {user.mention_html()} ({user.id})"
        await send_log(context, chat_id, log_text)
    elif old_status in ["member", "administrator"] and new_status in ["left", "kicked"]:
        log_text = f"<b>📤 MEMBER LEFT / REMOVED</b>\n<b>User:</b> {user.mention_html()} ({user.id})"
        await send_log(context, chat_id, log_text)

# --- AUTOMATIC ENFORCEMENT & FILTERS ---

async def auto_moderation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat: return
    chat_id = update.effective_chat.id
    msg = update.message

    # 1. Automatic Deleted Accounts Cleanup
    if msg.new_chat_members:
        for member in msg.new_chat_members:
            if member.is_deleted or member.first_name == "Deleted Account":
                await context.bot.ban_chat_member(chat_id, member.id)
                await msg.delete()
                log_text = f"<b>🤖 AUTO-BAN</b>\nDeleted Account removed: ID {member.id}"
                await send_log(context, chat_id, log_text)
                return

    # 2. Automatic Link Remover
    if msg.text or msg.caption:
        text = msg.text or msg.caption
        if re.search(r'(https?://[^\s]+|t\.me/[^\s]+|telegram\.me/[^\s]+)', text):
            if not await is_admin(update, context):
                await msg.delete()
                log_text = f"<b>🔗 LINK DELETED</b>\n<b>Sender:</b> {msg.from_user.mention_html()} ({msg.from_user.id})"
                await send_log(context, chat_id, log_text)
                return

    # 3. Keyword Auto-Filter
    if msg.text:
        db = get_db()
        res = db.execute("SELECT reply FROM filters WHERE chat_id=? AND keyword=?", (chat_id, msg.text.lower())).fetchone()
        if res:
            await msg.reply_text(res[0])

    # 4. Automatic 18+ NSFW Nude Content Detection
    if msg.photo:
        if SIGHTENGINE_USER != "YOUR_SIGHTENGINE_USER":
            photo_file = await msg.photo[-1].get_file()
            photo_url = photo_file.file_path
            
            params = {
                'url': photo_url,
                'models': 'nudity-2.0',
                'api_user': SIGHTENGINE_USER,
                'api_secret': SIGHTENGINE_SECRET
            }
            req = requests.get('https://api.sightengine.com/1.0/check.json', params=params)
            res = req.json()
            if res.get('status') == 'success':
                nudity = res.get('nudity', {})
                if nudity.get('sexual_activity', 0) > 0.5 or nudity.get('sexual_display', 0) > 0.5 or nudity.get('erotica', 0) > 0.6:
                    await msg.delete()
                    log_text = f"<b>🔞 NSFW CONTENT DELETED</b>\n<b>Sender:</b> {msg.from_user.mention_html()} ({msg.from_user.id})"
                    await send_log(context, chat_id, log_text)

# --- MAIN RUNNER ---
if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("setlog", set_log_channel))
    app.add_handler(CommandHandler("save", save_note))
    app.add_handler(CommandHandler("get", get_note))
    app.add_handler(CommandHandler("filter", set_filter))
    app.add_handler(CommandHandler("warn", warn_user))
    app.add_handler(CommandHandler("warns", check_warns))
    app.add_handler(CommandHandler("resetwarn", reset_warns))
    app.add_handler(CommandHandler("mute", mute_user))
    app.add_handler(CommandHandler("dmute", mute_user))
    app.add_handler(CommandHandler("tmute", tmute_user))
    app.add_handler(CommandHandler("ban", lambda u, c: ban_user(u, c, silent=False)))
    app.add_handler(CommandHandler("sban", lambda u, c: ban_user(u, c, silent=True)))
    app.add_handler(CommandHandler("unban", unban_user))
    app.add_handler(CommandHandler("newfed", new_fed))
    app.add_handler(CommandHandler("joinfed", join_fed))
    app.add_handler(CommandHandler("fban", lambda u, c: fban_user(u, c, ban=True)))
    app.add_handler(CommandHandler("funban", lambda u, c: fban_user(u, c, ban=False)))

    # Member Join/Leave Tracker
    app.add_handler(ChatMemberHandler(track_chat_members, ChatMemberHandler.CHAT_MEMBER))

    # Real-Time Monitoring & Auto Moderation Filter
    app.add_handler(MessageHandler(filters.ALL, auto_moderation_handler))

    print("Bot starting...")
    app.run_polling()
z
