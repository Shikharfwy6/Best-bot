import os, asyncio, traceback, time
from flask import Flask, request, abort
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from telegram import (Update, InlineKeyboardButton as B,
                      InlineKeyboardMarkup as M)
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, filters, ContextTypes)

TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]
SECRET = os.environ.get("WEBHOOK_SECRET", "mysecret123")
GROUP_ID = -1003701041040
PER_PAGE = 20
WINDOW = 60  # इतने सेकंड के अंदर आए posts एक ही message में गिने जाएँगे

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
db = client["tgbot"]
topics = db["topics"]    # {_id: topic_id, name, last_id}
posts = db["posts"]      # {t: topic_id, m: message_id}
state = db["state"]      # {_id: user_id, t, old, ask}
users = db["users"]      # {_id: user_id}
batch = db["batch"]      # {_id: topic_id, base, last, msgs}

try:
    posts.create_index([("t", 1), ("m", 1)], unique=True)
except Exception:
    pass

private = filters.ChatType.PRIVATE


# ---------- helpers ----------
def save_topic(tid, name):
    r = topics.update_one({"_id": int(tid)}, {"$set": {"name": name}},
                          upsert=True)
    return r.upserted_id is not None

def save_post(tid, mid):
    r = posts.update_one({"t": int(tid), "m": int(mid)},
                         {"$set": {"t": int(tid), "m": int(mid)}},
                         upsert=True)
    return r.upserted_id is not None

def topic_name(tid):
    t = topics.find_one({"_id": int(tid)})
    return (t or {}).get("name") or f"Topic {tid}"

def topics_markup(page=0):
    topics.update_one({"_id": 1}, {"$setOnInsert": {"name": "General"}},
                      upsert=True)
    items = list(topics.find().sort("_id", 1))
    chunk = items[page * PER_PAGE:(page + 1) * PER_PAGE]
    rows = [[B(topic_name(t["_id"])[:40], callback_data=f"t:{t['_id']}")]
            for t in chunk]
    nav = []
    if page > 0:
        nav.append(B("⬅️ पीछे", callback_data=f"pg:{page-1}"))
    if (page + 1) * PER_PAGE < len(items):
        nav.append(B("आगे ➡️", callback_data=f"pg:{page+1}"))
    if nav:
        rows.append(nav)
    return M(rows)

def nav_markup(tid, idx, total):
    def g(label, i):
        return B(label, callback_data=f"g:{tid}:{i}")
    rows = []

    r = []
    if idx > 1:
        r.append(g("⬅️ Prev", idx - 1))
    if idx < total:
        r.append(g("Next ➡️", idx + 1))
    else:
        r.append(g("🔁 शुरू से", 1))
    rows.append(r)

    r = []
    if idx > 1:
        r.append(g("⏪ -10", max(1, idx - 10)))
    if idx < total:
        r.append(g("⏩ +10", min(total, idx + 10)))
    if r:
        rows.append(r)

    if total > 100:
        r = []
        if idx > 1:
            r.append(g("⏪ -100", max(1, idx - 100)))
        if idx < total:
            r.append(g("⏩ +100", min(total, idx + 100)))
        if r:
            rows.append(r)

    rows.append([B(f"🔢 {idx}/{total} • नंबर पर जाएं",
                   callback_data=f"j:{tid}")])
    rows.append([B("🔙 Topics", callback_data="home")])
    return M(rows)

async def safe_delete(bot, chat, mid):
    if not mid:
        return
    try:
        await bot.delete_message(chat, mid)
    except Exception:
        pass

async def send_all(bot, text, markup=None):
    """सभी users को भेजो। {user_id: message_id} लौटाता है"""
    ids = [u["_id"] for u in users.find({}, {"_id": 1})]
    out = {}
    if not ids:
        return out
    sem = asyncio.Semaphore(25)

    async def one(uid):
        async with sem:
            for _ in range(2):
                try:
                    msg = await bot.send_message(uid, text,
                                                 reply_markup=markup)
                    out[str(uid)] = msg.message_id
                    return
                except RetryAfter as e:
                    await asyncio.sleep(min(e.retry_after, 5))
                except Forbidden:
                    users.delete_one({"_id": uid})
                    return
                except Exception:
                    return

    await asyncio.gather(*[one(u) for u in ids])
    return out

async def edit_all(bot, msgs, text):
    """पहले भेजे गए messages का text बदलो"""
    if not msgs:
        return
    sem = asyncio.Semaphore(25)

    async def one(uid, mid):
        async with sem:
            try:
                await bot.edit_message_text(text, chat_id=int(uid),
                                            message_id=mid)
            except RetryAfter as e:
                await asyncio.sleep(min(e.retry_after, 5))
            except Exception:
                pass  # text वही हो तो Telegram error देता है, ignore

    await asyncio.gather(*[one(u, m) for u, m in msgs.items()])

async def send_post(bot, chat, tid, idx, old=None, extra=()):
    while True:
        total = posts.count_documents({"t": tid})
        if total == 0:
            await bot.send_message(
                chat, "इस topic में अभी कोई post सेव नहीं है।",
                reply_markup=M([[B("🔙 Topics", callback_data="home")]]))
            break
        idx = max(1, min(int(idx), total))
        docs = list(posts.find({"t": tid}).sort("m", 1)
                    .skip(idx - 1).limit(1))
        if not docs:
            break
        doc = docs[0]
        try:
            await bot.copy_message(
                chat_id=chat, from_chat_id=GROUP_ID, message_id=doc["m"],
                reply_markup=nav_markup(tid, idx, total))
            break
        except BadRequest:
            posts.delete_one({"_id": doc["_id"]})
    await asyncio.gather(
        *[safe_delete(bot, chat, x) for x in [old, *extra]])


# ---------- group में आने वाले messages ----------
def new_count(tid, base):
    """सेव की हुई पुरानी ID (base) के बाद इस topic के कितने posts आए"""
    return posts.count_documents({"t": tid, "m": {"$gt": base}})

def post_text(n, tid):
    return f"📬 {n} Post added in: {topic_name(tid)}"

async def track(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    if not m or m.chat_id != GROUP_ID:
        return

    # नया topic
    if m.forum_topic_created:
        tid = m.message_thread_id
        name = m.forum_topic_created.name
        if save_topic(tid, name):
            await send_all(
                ctx.bot, f"🆕 New topic add: {name}",
                M([[B("📂 Topic खोलें", callback_data=f"t:{tid}")]]))
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

    # नया post
    tid = m.message_thread_id if m.is_topic_message else 1
    if not save_post(tid, m.message_id):
        return

    # database में सेव इस topic की पुरानी आख़िरी ID निकालो, और नई ID सेव करो
    prev = topics.find_one_and_update(
        {"_id": tid},
        {"$max": {"last_id": m.message_id},
         "$setOnInsert": {"name": f"Topic {tid}"}},
        upsert=True, return_document=ReturnDocument.BEFORE)
    prev_last = (prev or {}).get("last_id", 0)

    now = time.time()

    # 1) चालू batch है? तो उसमें जुड़ो, गिनती database से निकालो, edit करो
    doc = batch.find_one_and_update(
        {"_id": tid, "last": {"$gte": now - WINDOW}},
        {"$set": {"last": now}, "$min": {"base": prev_last}},
        return_document=ReturnDocument.AFTER)
    if doc:
        n = new_count(tid, doc["base"])
        await edit_all(ctx.bot, doc.get("msgs", {}), post_text(n, tid))
        return

    # 2) नया batch शुरू करो (सिर्फ़ एक function ये कर पाएगा)
    try:
        r = batch.update_one(
            {"_id": tid, "last": {"$lt": now - WINDOW}},
            {"$set": {"base": prev_last, "last": now, "msgs": {}}},
            upsert=True)
        starter = r.matched_count > 0 or r.upserted_id is not None
    except DuplicateKeyError:
        starter = False  # किसी और function ने अभी शुरू किया
    if not starter:
        doc = batch.find_one({"_id": tid})
        if doc:
            n = new_count(tid, doc["base"])
            await edit_all(ctx.bot, doc.get("msgs", {}), post_text(n, tid))
        return

    n = new_count(tid, prev_last)
    msgs = await send_all(ctx.bot, post_text(n, tid))
    doc = batch.find_one_and_update(
        {"_id": tid}, {"$set": {"msgs": msgs}},
        return_document=ReturnDocument.AFTER)
    # भेजते समय और posts आ गए हों तो गिनती ठीक करो
    if doc:
        n2 = new_count(tid, doc["base"])
        if n2 != n:
            await edit_all(ctx.bot, msgs, post_text(n2, tid))

async def is_admin(ctx, user_id):
    member = await ctx.bot.get_chat_member(GROUP_ID, user_id)
    return member.status in ("administrator", "creator")

async def addtopic(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    if not await is_admin(ctx, update.effective_user.id):
        return
    tid = m.message_thread_id if m.is_topic_message else 1
    name = " ".join(ctx.args) or f"Topic {tid}"
    save_topic(tid, name)
    await m.reply_text(f"✅ Topic सेव: {name}")

async def savepost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    if not await is_admin(ctx, update.effective_user.id):
        return
    r = m.reply_to_message
    if not r:
        return
    tid = r.message_thread_id if r.is_topic_message else 1
    save_post(tid, r.message_id)
    await m.reply_text("✅ Post सेव हो गया")


# ---------- user वाला हिस्सा ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    users.update_one({"_id": u.id}, {"$set": {"name": u.first_name}},
                     upsert=True)
    await update.message.reply_text("📚 कोई Topic चुनें:",
                                    reply_markup=topics_markup(0))

async def cb_home(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat = q.message.chat_id
    await asyncio.gather(
        q.answer(),
        ctx.bot.send_message(chat, "📚 कोई Topic चुनें:",
                             reply_markup=topics_markup(0)))
    await safe_delete(ctx.bot, chat, q.message.message_id)

async def cb_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    page = int(q.data.split(":")[1])
    await q.message.edit_reply_markup(reply_markup=topics_markup(page))

async def cb_topic(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tid = int(q.data.split(":")[1])
    await asyncio.gather(
        q.answer(),
        send_post(ctx.bot, q.message.chat_id, tid, 1,
                  old=q.message.message_id))

async def cb_go(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, tid, idx = q.data.split(":")
    await asyncio.gather(
        q.answer(),
        send_post(ctx.bot, q.message.chat_id, int(tid), int(idx),
                  old=q.message.message_id))

async def cb_jump(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tid = int(q.data.split(":")[1])
    total = posts.count_documents({"t": tid})
    ask = await ctx.bot.send_message(
        q.message.chat_id,
        f"🔢 1 से {total} के बीच post का नंबर भेजें:")
    state.update_one(
        {"_id": q.from_user.id},
        {"$set": {"t": tid, "old": q.message.message_id,
                  "ask": ask.message_id}},
        upsert=True)

async def on_number(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.message
    st = state.find_one({"_id": m.from_user.id})
    if not st:
        return
    txt = m.text.strip()
    if not txt.isdigit() or int(txt) < 1:
        await m.reply_text("❌ सिर्फ़ नंबर भेजें (जैसे 25)")
        return
    state.delete_one({"_id": m.from_user.id})
    await send_post(ctx.bot, m.chat_id, st["t"], int(txt),
                    old=st["old"], extra=[st["ask"], m.message_id])


# ---------- webhook ----------
def build_app():
    application = (Application.builder().token(TOKEN)
                   .updater(None).build())
    application.add_handler(CommandHandler("start", start, private))
    application.add_handler(CommandHandler("addtopic", addtopic,
                                           filters.Chat(GROUP_ID)))
    application.add_handler(CommandHandler("save", savepost,
                                           filters.Chat(GROUP_ID)))
    application.add_handler(CallbackQueryHandler(cb_home, pattern=r"^home$"))
    application.add_handler(CallbackQueryHandler(cb_page, pattern=r"^pg:\d+$"))
    application.add_handler(CallbackQueryHandler(cb_topic, pattern=r"^t:\d+$"))
    application.add_handler(CallbackQueryHandler(cb_go,
                                                 pattern=r"^g:\d+:\d+$"))
    application.add_handler(CallbackQueryHandler(cb_jump, pattern=r"^j:\d+$"))
    application.add_handler(MessageHandler(
        private & filters.TEXT & ~filters.COMMAND, on_number))
    application.add_handler(MessageHandler(filters.Chat(GROUP_ID), track))
    return application

async def process(data):
    application = build_app()
    async with application:
        await application.process_update(
            Update.de_json(data, application.bot))

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
    try:
        asyncio.run(process(request.get_json(force=True)))
    except Exception:
        traceback.print_exc()
    return "ok"
