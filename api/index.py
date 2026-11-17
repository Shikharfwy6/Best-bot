import os, asyncio
from flask import Flask, request, abort
from pymongo import MongoClient
from telegram import (Update, InlineKeyboardButton as B,
                      InlineKeyboardMarkup as M)
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, filters, ContextTypes)

TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]
SECRET = os.environ.get("WEBHOOK_SECRET", "mysecret123")
GROUP_ID = -1003701041040
PER_PAGE = 20

db = MongoClient(MONGO_URI)["tgbot"]
topics = db["topics"]   # {_id: topic_id, name}
posts = db["posts"]     # {t: topic_id, m: message_id}
pos = db["pos"]         # {_id: "user:topic", m: last_message_id}

def save_topic(tid, name):
    topics.update_one({"_id": int(tid)}, {"$set": {"name": name}}, upsert=True)

def save_post(tid, mid):
    posts.update_one({"t": int(tid), "m": int(mid)},
                     {"$set": {"t": int(tid), "m": int(mid)}}, upsert=True)

def topic_name(tid):
    t = topics.find_one({"_id": int(tid)})
    return t["name"] if t else f"Topic {tid}"

def topics_markup(page=0):
    topics.update_one({"_id": 1}, {"$setOnInsert": {"name": "General"}},
                      upsert=True)
    items = list(topics.find().sort("_id", 1))
    chunk = items[page * PER_PAGE:(page + 1) * PER_PAGE]
    rows = [[B(t["name"][:40], callback_data=f"t:{t['_id']}")] for t in chunk]
    nav = []
    if page > 0:
        nav.append(B("⬅️ पीछे", callback_data=f"pg:{page-1}"))
    if (page + 1) * PER_PAGE < len(items):
        nav.append(B("आगे ➡️", callback_data=f"pg:{page+1}"))
    if nav:
        rows.append(nav)
    return M(rows)

def next_markup(tid):
    return M([[B("➡️ Next post", callback_data=f"n:{tid}")],
              [B("🔙 Topics", callback_data="home")]])

# ---------- group में आने वाले messages सेव करना ----------
async def track(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    if not m or m.chat_id != GROUP_ID:
        return
    if m.forum_topic_created:
        save_topic(m.message_thread_id, m.forum_topic_created.name)
        return
    if m.forum_topic_edited:
        if m.forum_topic_edited.name:
            save_topic(m.message_thread_id, m.forum_topic_edited.name)
        return
    has_content = any([m.text, m.caption, m.photo, m.video, m.document,
                       m.audio, m.voice, m.animation, m.sticker,
                       m.video_note, m.poll, m.location, m.contact])
    if not has_content or (m.text and m.text.startswith("/")):
        return
    tid = m.message_thread_id if m.is_topic_message else 1
    save_post(tid, m.message_id)

async def addtopic(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    member = await ctx.bot.get_chat_member(GROUP_ID, update.effective_user.id)
    if member.status not in ("administrator", "creator"):
        return
    tid = m.message_thread_id if m.is_topic_message else 1
    name = " ".join(ctx.args) or f"Topic {tid}"
    save_topic(tid, name)
    await m.reply_text(f"✅ Topic सेव: {name}")

async def savepost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """किसी पुराने post पर reply करके /save लिखें (सिर्फ़ admin)"""
    m = update.effective_message
    member = await ctx.bot.get_chat_member(GROUP_ID, update.effective_user.id)
    if member.status not in ("administrator", "creator"):
        return
    r = m.reply_to_message
    if not r:
        return
    tid = r.message_thread_id if r.is_topic_message else 1
    save_post(tid, r.message_id)
    await m.reply_text("✅ Post सेव हो गया")

# ---------- user वाला हिस्सा ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📚 कोई Topic चुनें:",
                                    reply_markup=topics_markup(0))

async def cb_home(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await ctx.bot.send_message(q.message.chat_id, "📚 कोई Topic चुनें:",
                               reply_markup=topics_markup(0))

async def cb_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    page = int(q.data.split(":")[1])
    await q.message.edit_reply_markup(reply_markup=topics_markup(page))

async def cb_topic(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tid = int(q.data.split(":")[1])
    pos.delete_one({"_id": f"{q.from_user.id}:{tid}"})  # शुरू से
    count = posts.count_documents({"t": tid})
    if count == 0:
        await ctx.bot.send_message(
            q.message.chat_id, "इस topic में अभी कोई post सेव नहीं है।",
            reply_markup=M([[B("🔙 Topics", callback_data="home")]]))
        return
    await ctx.bot.send_message(
        q.message.chat_id, f"📂 {topic_name(tid)}\nकुल posts: {count}",
        reply_markup=next_markup(tid))

async def cb_next(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tid = int(q.data.split(":")[1])
    key = f"{q.from_user.id}:{tid}"
    doc = pos.find_one({"_id": key})
    last = doc["m"] if doc else 0
    chat = q.message.chat_id

    try:  # पुराने message से button हटाएं, ताकि सिर्फ़ नया button रहे
        await q.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    while True:
        nxt = posts.find_one({"t": tid, "m": {"$gt": last}}, sort=[("m", 1)])
        if not nxt:
            await ctx.bot.send_message(
                chat, "✅ इस topic के सारे posts देख लिए।",
                reply_markup=M([[B("🔁 शुरू से", callback_data=f"t:{tid}")],
                                [B("🔙 Topics", callback_data="home")]]))
            return
        try:
            await ctx.bot.copy_message(
                chat_id=chat, from_chat_id=GROUP_ID,
                message_id=nxt["m"], reply_markup=next_markup(tid))
            pos.update_one({"_id": key}, {"$set": {"m": nxt["m"]}}, upsert=True)
            return
        except Exception:
            # post delete हो चुका होगा: हटाकर अगले पर जाएं
            posts.delete_one({"_id": nxt["_id"]})
            last = nxt["m"]

async def process(data):
    application = Application.builder().token(TOKEN).updater(None).build()
    private = filters.ChatType.PRIVATE
    application.add_handler(CommandHandler("start", start, private))
    application.add_handler(CommandHandler("addtopic", addtopic,
                                           filters.Chat(GROUP_ID)))
    application.add_handler(CommandHandler("save", savepost,
                                           filters.Chat(GROUP_ID)))
    application.add_handler(CallbackQueryHandler(cb_home, pattern=r"^home$"))
    application.add_handler(CallbackQueryHandler(cb_page, pattern=r"^pg:\d+$"))
    application.add_handler(CallbackQueryHandler(cb_topic, pattern=r"^t:\d+$"))
    application.add_handler(CallbackQueryHandler(cb_next, pattern=r"^n:\d+$"))
    application.add_handler(MessageHandler(filters.Chat(GROUP_ID), track))
    await application.initialize()
    await application.process_update(Update.de_json(data, application.bot))
    await application.shutdown()

app = Flask(__name__)

@app.route("/", defaults={"path": ""}, methods=["GET"])
@app.route("/<path:path>", methods=["GET"])
def home(path):
    return "Bot चल रहा है ✅"

@app.route("/", defaults={"path": ""}, methods=["POST"])
@app.route("/<path:path>", methods=["POST"])
def webhook(path):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        abort(403)
    asyncio.run(process(request.get_json(force=True)))
    return "ok"
