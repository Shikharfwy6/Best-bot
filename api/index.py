import os, asyncio, html
from flask import Flask, request, abort
from pymongo import MongoClient
from telegram import Update
from telegram.ext import (Application, CommandHandler,
                          MessageHandler, filters, ContextTypes)

TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]
SECRET = os.environ.get("WEBHOOK_SECRET", "mysecret123")
GROUP_ID = -1003701041040
CHAT_SHORT = str(GROUP_ID).replace("-100", "")

topics = MongoClient(MONGO_URI)["tgbot"]["topics"]

def save_topic(tid, name):
    topics.update_one({"_id": int(tid)}, {"$set": {"name": name}}, upsert=True)

async def track(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    if not m or m.chat_id != GROUP_ID:
        return
    if m.forum_topic_created:
        save_topic(m.message_thread_id, m.forum_topic_created.name)
    elif m.forum_topic_edited and m.forum_topic_edited.name:
        save_topic(m.message_thread_id, m.forum_topic_edited.name)
    elif m.reply_to_message and m.reply_to_message.forum_topic_created:
        save_topic(m.message_thread_id,
                   m.reply_to_message.forum_topic_created.name)

async def addtopic(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    if m.chat_id != GROUP_ID or not m.message_thread_id:
        return
    member = await ctx.bot.get_chat_member(GROUP_ID, update.effective_user.id)
    if member.status not in ("administrator", "creator"):
        return
    name = " ".join(ctx.args) or f"Topic {m.message_thread_id}"
    save_topic(m.message_thread_id, name)
    await m.reply_text(f"✅ सेव हो गया: {name}")

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not topics.find_one({"_id": 1}):
        save_topic(1, "General")
    items = list(topics.find().sort("_id", 1))
    lines = ["📚 <b>Group के Topics:</b>\n"]
    for t in items:
        link = f"https://t.me/c/{CHAT_SHORT}/{t['_id']}"
        lines.append(f'• <a href="{link}">{html.escape(t["name"])}</a>')
    text = "\n".join(lines)
    # Telegram की 4096 limit के हिसाब से टुकड़े
    for i in range(0, len(text), 4000):
        await update.message.reply_text(
            text[i:i+4000], parse_mode="HTML",
            disable_web_page_preview=True)

async def process(data):
    application = Application.builder().token(TOKEN).updater(None).build()
    application.add_handler(
        CommandHandler("start", start, filters.ChatType.PRIVATE))
    application.add_handler(
        CommandHandler("addtopic", addtopic, filters.Chat(GROUP_ID)))
    application.add_handler(MessageHandler(filters.Chat(GROUP_ID), track))
    await application.initialize()
    await application.process_update(Update.de_json(data, application.bot))
    await application.shutdown()

app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "Bot चल रहा है ✅"

@app.route("/webhook", methods=["POST"])
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        abort(403)
    asyncio.run(process(request.get_json(force=True)))
    return "ok"
