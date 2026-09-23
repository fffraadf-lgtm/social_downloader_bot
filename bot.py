# -*- coding: utf-8 -*-
"""
بوت المحاضرات — مواد دراسية ← محاضرات ← ملفات.
إرسال الملفات مع اسم المادة والمحاضرة مباشرة + نظام تنظيف الشات وجلب أسماء المشرفين تلقائياً.
تمت إضافة نظام إعادة الاتصال التلقائي عند انقطاع الإنترنت ومعالجة أخطاء الشبكة.
"""
import asyncio
import html
import json
import logging
import os
import time

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict, InvalidToken, NetworkError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("lectures-bot")

with open(CONFIG_PATH, encoding="utf-8") as f:
    CFG = json.load(f)

TOKEN = CFG.get("token", "").strip()
WELCOME = CFG.get("welcome", "أهلاً بيك 👋\nاختر المادة العلمية:")

# حسابك الشخصي للدعم الفني
SUPPORT_USERNAME = "raadjv"

db.init()

STATE = {}
USER_NAV_MSG = {}


# ============================ أدوات مساعدة ============================
def admins():
    return set(CFG.get("admins", [])) | set(db.db_admins())


def is_admin(uid):
    return uid in admins()


ALL_PERMS = ["add", "upload", "edit", "delete", "broadcast"]
PERM_LABEL = {
    "add": "➕ إضافة مواد ومحاضرات",
    "upload": "📤 رفع الملفات",
    "edit": "✏️ تعديل الأسماء والترتيب",
    "delete": "🗑 الحذف",
    "broadcast": "📢 الرسالة الجماعية",
}


def is_owner(uid):
    if uid in set(CFG.get("admins", [])):
        return True
    r = db.admin_row(uid)
    return bool(r and r["role"] == "owner")


def perms_of(uid):
    if is_owner(uid):
        return set(ALL_PERMS)
    r = db.admin_row(uid)
    if not r:
        return set()
    return {x.strip() for x in (r["perms"] or "").split(",") if x.strip()}


def can(uid, perm):
    return perm in perms_of(uid)


def scope_of(uid):
    if is_owner(uid):
        return "all"
    r = db.admin_row(uid)
    return (r["scope"] if r else "all") or "all"


def scope_ids(uid):
    sc = scope_of(uid)
    if sc == "all":
        return None
    return {int(x) for x in sc.split(",") if x.strip().isdigit()}


def in_scope(uid, sid):
    ids = scope_ids(uid)
    return ids is None or int(sid) in ids


def scoped_subjects(uid):
    ids = scope_ids(uid)
    return [s for s in db.subjects() if ids is None or s["id"] in ids]


def perms_text(uid):
    if is_owner(uid):
        return "👑 مالك البوت — كل الصلاحيات"
    ps = perms_of(uid)
    if not ps:
        return "بدون صلاحيات"
    names = [PERM_LABEL[p] for p in ALL_PERMS if p in ps]
    ids = scope_ids(uid)
    line = " • ".join(names)
    if ids is not None:
        subs = [s["name"] for s in db.subjects() if s["id"] in ids]
        line += "\n📚 المواد المسموحة: " + (", ".join(subs) if subs else "لا شيء")
    return line


def esc(t):
    return html.escape(str(t or ""))


def kb(rows):
    return InlineKeyboardMarkup(rows)


def get_role_reply_keyboard(uid):
    """الأزرار الدائمة الثابتة بالأسفل وتجبر تيليجرام على إبقائها ظاهرة دائماً"""
    if is_owner(uid):
        rows = [
            [KeyboardButton("📚 مواد قسمي"), KeyboardButton("👑 إدارة التحكم")]
        ]
    elif is_admin(uid):
        rows = [
            [KeyboardButton("📚 مواد قسمي"), KeyboardButton("🛠 إدارة القسم")]
        ]
    else:
        rows = [
            [KeyboardButton("📚 مواد قسمي")],
            [KeyboardButton("💬 الدعم والمساعدة")]
        ]
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False
    )


async def safe_delete(bot, chat_id, message_id):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def show(update: Update, text, markup=None):
    q = update.callback_query
    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    if q:
        try:
            await q.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
            USER_NAV_MSG[uid] = q.message.message_id
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
            msg = await q.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
            USER_NAV_MSG[uid] = msg.message_id
            return

    old_msg = USER_NAV_MSG.get(uid)
    if old_msg:
        await safe_delete(update.get_bot(), chat_id, old_msg)

    msg = await update.effective_message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    USER_NAV_MSG[uid] = msg.message_id


# ============================ إشعار المحاضرة ============================
async def notify_new_lecture(context: ContextTypes.DEFAULT_TYPE, lid: int):
    l = db.lecture(lid)
    if not l:
        return
    s = db.subject(l["subject_id"])
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start=l{lid}"

    msg = (
        "🔔 <b>إشعار محاضرة جديدة!</b>\n\n"
        f"📘 المادة: <b>{esc(s['name'])}</b>\n"
        f"📄 المحاضرة: <b>{esc(l['name'])}</b>"
    )
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("📥 فتح المحاضرة", url=link)]])

    for uid in db.all_user_ids():
        try:
            await context.bot.send_message(uid, msg, parse_mode=ParseMode.HTML, reply_markup=markup)
            await asyncio.sleep(0.04)
        except Exception:
            pass


# ============================ واجهة الطالب ============================
def subjects_kb():
    rows = [
        [InlineKeyboardButton("📘 " + s["name"], callback_data="u:s:%d" % s["id"])]
        for s in db.subjects()
    ]
    return kb(rows)


async def user_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    subs = db.subjects()
    if not subs:
        txt = "لا توجد مواد مضافة بعد ⏳"
        if is_admin(uid):
            txt += "\n\nاضغط لوحة التحكم من الأسفل لإضافة أول مادة."
        await show(update, txt, subjects_kb())
        return
    await show(update, WELCOME, subjects_kb())


async def user_subject(update: Update, sid):
    s = db.subject(sid)
    if not s:
        await show(update, "المادة غير موجودة.", subjects_kb())
        return
    lecs = db.lectures(sid)
    rows = [
        [InlineKeyboardButton("📄 " + l["name"], callback_data="u:l:%d" % l["id"])]
        for l in lecs
    ]
    rows.append([InlineKeyboardButton("⬅️ رجوع للمواد", callback_data="u:home")])
    txt = "📘 <b>%s</b>\n\n" % esc(s["name"])
    txt += "اختر المحاضرة:" if lecs else "لا توجد محاضرات في هذه المادة بعد ⏳"
    await show(update, txt, kb(rows))


async def send_lecture(update: Update, context: ContextTypes.DEFAULT_TYPE, lid):
    l = db.lecture(lid)
    if not l:
        await show(update, "المحاضرة غير موجودة.", subjects_kb())
        return
    s = db.subject(l["subject_id"])
    rows = db.files(lid)
    chat_id = update.effective_chat.id
    uid = update.effective_user.id

    back = kb([
        [InlineKeyboardButton("⬅️ رجوع للمحاضرات", callback_data="u:s:%d" % l["subject_id"])],
        [InlineKeyboardButton("🏠 المواد", callback_data="u:home")],
    ])

    if not rows:
        await show(update, "📘 <b>%s</b>\n📄 <b>%s</b>\n\nما موجود ملفات بهذه المحاضرة بعد ⏳" % (esc(s["name"]), esc(l["name"])), back)
        return

    q = update.callback_query
    if q:
        await safe_delete(context.bot, chat_id, q.message.message_id)
    elif uid in USER_NAV_MSG:
        await safe_delete(context.bot, chat_id, USER_NAV_MSG[uid])

    status_msg = await context.bot.send_message(
        chat_id,
        "📘 <b>%s</b>\n📄 <b>%s</b>\n\nجاري الإرسال…" % (esc(s["name"]), esc(l["name"])),
        parse_mode=ParseMode.HTML
    )

    file_caption = "📘 %s\n📄 %s" % (s["name"], l["name"])

    sent = 0
    for r in rows:
        kind, fid = r["kind"], r["file_id"]
        cap = f"{file_caption}\n\n{r['caption']}" if r["caption"] else file_caption
        try:
            if kind == "photo":
                await context.bot.send_photo(chat_id, fid, caption=cap)
            elif kind == "video":
                await context.bot.send_video(chat_id, fid, caption=cap)
            elif kind == "audio":
                await context.bot.send_audio(chat_id, fid, caption=cap)
            elif kind == "voice":
                await context.bot.send_voice(chat_id, fid, caption=cap)
            elif kind == "animation":
                await context.bot.send_animation(chat_id, fid, caption=cap)
            elif kind == "video_note":
                await context.bot.send_video_note(chat_id, fid)
            else:
                await context.bot.send_document(chat_id, fid, caption=cap)
            sent += 1
        except Exception as e:
            log.warning("فشل إرسال الملف %s: %s", r["id"], e)

    await safe_delete(context.bot, chat_id, status_msg.message_id)

    if sent:
        tail = "📘 <b>%s</b>\n📄 <b>%s</b>" % (esc(s["name"]), esc(l["name"]))
    else:
        tail = "⚠️ تعذر إرسال الملفات."

    done_msg = await context.bot.send_message(chat_id, tail, parse_mode=ParseMode.HTML, reply_markup=back)
    USER_NAV_MSG[uid] = done_msg.message_id


# ============================ لوحة التحكم ============================
async def admin_home(update: Update):
    uid = update.effective_user.id
    st = db.stats()
    mine = len(scoped_subjects(uid))
    title = "👑 <b>لوحة تحكم المالك</b>" if is_owner(uid) else "🛠 <b>إدارة القسم (المشرف)</b>"
    txt = (
        f"{title}\n\n"
        "📘 المواد: %d\n"
        "📄 المحاضرات: %d\n"
        "📎 الملفات: %d\n"
        "👥 المستخدمون: %d\n\n"
        "🔑 صلاحياتك: %s"
    ) % (st["subjects"], st["lectures"], st["files"], st["users"], perms_text(uid))
    if scope_ids(uid) is not None:
        txt += "\n(تشوف %d مادة من أصل %d)" % (mine, st["subjects"])
    rows = []
    if can(uid, "add") and scope_ids(uid) is None:
        rows.append([InlineKeyboardButton("➕ إضافة مادة", callback_data="a:addsub")])
    rows.append([InlineKeyboardButton("📚 إدارة المواد", callback_data="a:subs")])
    if can(uid, "broadcast"):
        rows.append([InlineKeyboardButton("📢 رسالة جماعية", callback_data="a:bc")])
    if is_owner(uid):
        rows.append([InlineKeyboardButton("👮 المشرفون والصلاحيات", callback_data="a:admins")])
    rows.append([InlineKeyboardButton("👤 شوف البوت مثل الزبون", callback_data="u:home")])
    await show(update, txt, kb(rows))


async def admin_subjects(update: Update):
    uid = update.effective_user.id
    subs = scoped_subjects(uid)
    rows = [
        [InlineKeyboardButton("📘 " + s["name"], callback_data="a:s:%d" % s["id"])]
        for s in subs
    ]
    if can(uid, "edit") and scope_ids(uid) is None and len(subs) > 1:
        rows.append([InlineKeyboardButton("🔃 ترتيب المواد", callback_data="a:ord:s:0")])
    if can(uid, "add") and scope_ids(uid) is None:
        rows.append([InlineKeyboardButton("➕ إضافة مادة", callback_data="a:addsub")])
    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="a:home")])
    txt = "📚 <b>إدارة المواد</b>\n\nاضغط على اسم المادة لإدارتها:" if subs else "ما عندك مواد بعد 👇"
    await show(update, txt, kb(rows))


async def admin_subject(update: Update, sid):
    s = db.subject(sid)
    if not s:
        await admin_subjects(update)
        return
    uid = update.effective_user.id
    lecs = db.lectures(sid)
    rows = [
        [InlineKeyboardButton(
            "📄 %s (%d)" % (l["name"], db.count_files(l["id"])), callback_data="a:l:%d" % l["id"]
        )]
        for l in lecs
    ]
    if can(uid, "edit") and len(lecs) > 1:
        rows.append([InlineKeyboardButton("🔃 ترتيب المحاضرات", callback_data="a:ord:l:%d:0" % sid)])
    if can(uid, "add"):
        rows.append([InlineKeyboardButton("➕ إضافة محاضرة", callback_data="a:addlec:%d" % sid)])
    sub_row = []
    if can(uid, "edit"):
        sub_row.append(InlineKeyboardButton("✏️ تعديل الاسم", callback_data="a:ren:s:%d" % sid))
    if can(uid, "delete") and scope_ids(uid) is None:
        sub_row.append(InlineKeyboardButton("🗑 حذف المادة", callback_data="a:del:s:%d" % sid))
    if sub_row:
        rows.append(sub_row)
    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="a:subs")])
    txt = "📘 <b>%s</b>\nعدد المحاضرات: %d" % (esc(s["name"]), len(lecs))
    await show(update, txt, kb(rows))


async def admin_lecture(update: Update, context, lid):
    l = db.lecture(lid)
    if not l:
        await admin_subjects(update)
        return
    s = db.subject(l["subject_id"])
    n = db.count_files(lid)
    me = await context.bot.get_me()
    link = "https://t.me/%s?start=l%d" % (me.username, lid)
    txt = (
        "📘 %s\n📄 <b>%s</b>\n📎 الملفات: %d\n\n🔗 رابط مباشر للمحاضرة:\n<code>%s</code>"
    ) % (esc(s["name"]), esc(l["name"]), n, link)
    uid = update.effective_user.id
    rows = []
    if can(uid, "upload"):
        rows.append([InlineKeyboardButton("📤 رفع ملفات", callback_data="a:up:%d" % lid)])
    rows.append([InlineKeyboardButton("📎 ملفات المحاضرة", callback_data="a:files:%d" % lid)])
    lec_row = []
    if can(uid, "edit"):
        lec_row.append(InlineKeyboardButton("✏️ تعديل الاسم", callback_data="a:ren:l:%d" % lid))
    if can(uid, "delete"):
        lec_row.append(InlineKeyboardButton("🗑 حذف المحاضرة", callback_data="a:del:l:%d" % lid))
    if lec_row:
        rows.append(lec_row)
    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="a:s:%d" % l["subject_id"])])
    await show(update, txt, kb(rows))


async def admin_files(update: Update, lid):
    uid = update.effective_user.id
    rows_db = db.files(lid)
    rows = []
    for i, r in enumerate(rows_db, start=1):
        label = "%d. %s" % (i, (r["title"] or r["kind"])[:30])
        row = [InlineKeyboardButton(label, callback_data="a:noop")]
        if can(uid, "delete"):
            row.append(InlineKeyboardButton("🗑", callback_data="a:delf:%d" % r["id"]))
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="a:l:%d" % lid)])
    if not rows_db:
        txt = "ما موجود ملفات بهذه المحاضرة."
    elif can(uid, "delete"):
        txt = "📎 <b>ملفات المحاضرة</b>\nاضغط 🗑 لحذف ملف:"
    else:
        txt = "📎 <b>ملفات المحاضرة</b>"
    await show(update, txt, kb(rows))


# ============================ شاشة الترتيب ============================
async def order_screen(update: Update, kind, sid=0, sel=0):
    items = db.subjects() if kind == "s" else db.lectures(sid)
    rows = []
    for it in items:
        mark = "📍 " if it["id"] == sel else "▫️ "
        cb = "a:ord:s:%d" % it["id"] if kind == "s" else "a:ord:l:%d:%d" % (sid, it["id"])
        rows.append([InlineKeyboardButton(mark + it["name"], callback_data=cb)])
    if sel:
        if kind == "s":
            up, down = "a:omv:s:%d:up" % sel, "a:omv:s:%d:down" % sel
        else:
            up, down = "a:omv:l:%d:up" % sel, "a:omv:l:%d:down" % sel
        rows.append([
            InlineKeyboardButton("⬆️ فوق", callback_data=up),
            InlineKeyboardButton("⬇️ تحت", callback_data=down),
        ])
    rows.append([InlineKeyboardButton(
        "✅ خلصت", callback_data="a:subs" if kind == "s" else "a:s:%d" % sid
    )])
    title = "🔃 <b>ترتيب المواد</b>" if kind == "s" else "🔃 <b>ترتيب المحاضرات</b>"
    if sel:
        name = next((i["name"] for i in items if i["id"] == sel), "")
        body = "المحدد: <b>%s</b>\nحركه بالأسهم تحت، وكل ضغطة تنقله خطوة وحدة." % esc(name)
    else:
        body = "اضغط على الي تريد تحركه 👇"
    await show(update, title + "\n\n" + body, kb(rows))


# ============================ إدارة المشرفين ============================
async def admins_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = []
    for a in db.all_admins():
        badge = "👑" if a["role"] == "owner" else "👮"
        aid = a["id"]
        name = a["name"]

        if not name or name.strip().isdigit() or name.startswith("مشرف ("):
            try:
                c = await context.bot.get_chat(aid)
                real_name = c.full_name or c.title or c.username or str(aid)
                db.set_admin_field(aid, "name", real_name)
                name = real_name
            except Exception:
                name = name or str(aid)

        rows.append([InlineKeyboardButton("%s %s" % (badge, name), callback_data="a:adm:%d" % aid)])
    rows.append([InlineKeyboardButton("➕ إضافة مشرف", callback_data="a:addadm")])
    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="a:home")])
    txt = (
        "👮 <b>المشرفون</b>\n\n"
        "👑 = مالك (كل الصلاحيات + إدارة المشرفين)\n"
        "👮 = مشرف بصلاحيات محددة\n\n"
        "اضغط على أي واحد لتعديل صلاحياته أو اسمه."
    )
    await show(update, txt, kb(rows))


async def admin_one(update: Update, context: ContextTypes.DEFAULT_TYPE, aid: int):
    a = db.admin_row(aid)
    if not a:
        return await admins_list(update, context)
    me = update.effective_user.id
    owner = a["role"] == "owner"
    ps = perms_of(aid)
    name = a["name"]

    if not name or name.strip().isdigit() or name.startswith("مشرف ("):
        try:
            c = await context.bot.get_chat(aid)
            real_name = c.full_name or c.title or c.username or str(aid)
            db.set_admin_field(aid, "name", real_name)
            name = real_name
        except Exception:
            name = name or str(aid)

    txt = "👤 <b>%s</b>\n🆔 <code>%d</code>\n📛 الدور: %s\n\n" % (
        esc(name), aid, "👑 مالك" if owner else "👮 مشرف",
    )
    rows = []
    if owner:
        txt += "المالك عنده كل الصلاحيات تلقائياً."
    else:
        txt += "اضغط على أي صلاحية لتشغيلها أو إطفائها:"
        for p in ALL_PERMS:
            mark = "✅" if p in ps else "⬜️"
            rows.append([InlineKeyboardButton(
                "%s %s" % (mark, PERM_LABEL[p]), callback_data="a:perm:%d:%s" % (aid, p)
            )])
        ids = scope_ids(aid)
        scope_lbl = "📚 المواد: الكل" if ids is None else "📚 المواد: %d مادة محددة" % len(ids)
        rows.append([InlineKeyboardButton(scope_lbl, callback_data="a:scope:%d" % aid)])

    rows.append([InlineKeyboardButton("✏️ تغيير اسم المشرف", callback_data="a:renadm:%d" % aid)])

    if aid != me:
        rows.append([InlineKeyboardButton(
            "👮 اجعله مشرف عادي" if owner else "👑 ترقية لمالك", callback_data="a:role:%d" % aid
        )])
        rows.append([InlineKeyboardButton("🗑 إزالة المشرف", callback_data="a:admdel:%d" % aid)])
    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="a:admins")])
    await show(update, txt, kb(rows))


async def admin_scope(update: Update, aid):
    ids = scope_ids(aid)
    rows = [[InlineKeyboardButton(
        ("✅ " if ids is None else "⬜️ ") + "كل المواد", callback_data="a:scall:%d" % aid
    )]]
    for s in db.subjects():
        mark = "✅" if (ids is not None and s["id"] in ids) else "⬜️"
        rows.append([InlineKeyboardButton(
            "%s %s" % (mark, s["name"]), callback_data="a:sc:%d:%d" % (aid, s["id"])
        )])
    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="a:adm:%d" % aid)])
    await show(
        update,
        "📚 <b>المواد المسموحة لهذا المشرف</b>\n\n"
        "إذا اخترت مواد محددة، راح يشوف ويشتغل عليها بس.",
        kb(rows),
    )


# ============================ الأزرار ============================
NEED_PERM = {
    "addsub": "add",
    "addlec": "add",
    "ren": "edit",
    "ord": "edit",
    "omv": "edit",
    "del": "delete",
    "delok": "delete",
    "delf": "delete",
    "up": "upload",
    "endup": "upload",
    "bc": "broadcast",
}
OWNER_ONLY = {"admins", "adm", "addadm", "renadm", "perm", "role", "scope", "sc", "scall", "admdel", "admdelok"}


def subject_of_action(p):
    act = p[1]
    try:
        if act in ("s", "addlec"):
            return int(p[2])
        if act in ("l", "files", "up", "endup"):
            l = db.lecture(int(p[2]))
            return l["subject_id"] if l else None
        if act in ("ren", "del", "delok", "omv"):
            kind, rid = p[2], int(p[3])
            if kind == "s":
                return rid or None
            l = db.lecture(rid)
            return l["subject_id"] if l else None
        if act == "ord" and p[2] == "l":
            return int(p[3])
        if act == "delf":
            r = db.file_row(int(p[2]))
            if not r:
                return None
            l = db.lecture(r["lecture_id"])
            return l["subject_id"] if l else None
    except (IndexError, ValueError):
        return None
    return None


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    uid = q.from_user.id
    try:
        await q.answer()
    except BadRequest:
        pass

    if data == "u:home":
        return await user_home(update, context)
    if data.startswith("u:s:"):
        return await user_subject(update, int(data.split(":")[2]))
    if data.startswith("u:l:"):
        return await send_lecture(update, context, int(data.split(":")[2]))

    if not data.startswith("a:"):
        return
    if not is_admin(uid):
        return await q.answer("هذا الزر للمشرف فقط", show_alert=True)

    p = data.split(":")
    act = p[1]

    need = NEED_PERM.get(act)
    if need and not can(uid, need):
        return await q.answer("ما عندك صلاحية " + PERM_LABEL[need], show_alert=True)
    if act in OWNER_ONLY and not is_owner(uid):
        return await q.answer("هذا القسم لمالك البوت فقط", show_alert=True)
    sid_guard = subject_of_action(p)
    if sid_guard is not None and not in_scope(uid, sid_guard):
        return await q.answer("هذه المادة مو ضمن صلاحياتك", show_alert=True)

    if act == "noop":
        return
    if act == "home":
        STATE.pop(uid, None)
        return await admin_home(update)
    if act == "subs":
        return await admin_subjects(update)
    if act == "s":
        return await admin_subject(update, int(p[2]))
    if act == "l":
        return await admin_lecture(update, context, int(p[2]))
    if act == "files":
        return await admin_files(update, int(p[2]))

    if act == "addsub":
        STATE[uid] = {"want": "subject_name", "prompt_msg_id": q.message.message_id}
        return await show(
            update,
            "✍️ أرسل اسم المادة العلمية\nمثال: <code>كيمياء حياتية</code>\n\n"
            "تكدر ترسل عدة أسماء بسطور منفصلة وتنضاف كلها مرة وحدة.",
            kb([[InlineKeyboardButton("إلغاء", callback_data="a:subs")]]),
        )

    if act == "addlec":
        sid = int(p[2])
        STATE[uid] = {"want": "lecture_name", "sid": sid, "prompt_msg_id": q.message.message_id}
        return await show(
            update,
            "✍️ أرسل اسم المحاضرة\nمثال: <code>محاضرة 1</code>\n\n"
            "تكدر ترسل عدة أسماء بسطور منفصلة وتنضاف كلها مرة وحدة.",
            kb([[InlineKeyboardButton("إلغاء", callback_data="a:s:%d" % sid)]]),
        )

    if act == "ren":
        kind, rid = p[2], int(p[3])
        STATE[uid] = {"want": "rename", "kind": kind, "id": rid, "prompt_msg_id": q.message.message_id}
        back = "a:s:%d" % rid if kind == "s" else "a:l:%d" % rid
        return await show(
            update, "✍️ أرسل الاسم الجديد:", kb([[InlineKeyboardButton("إلغاء", callback_data=back)]])
        )

    if act == "renadm":
        aid = int(p[2])
        STATE[uid] = {"want": "rename_admin", "aid": aid, "prompt_msg_id": q.message.message_id}
        return await show(
            update,
            "✍️ أرسل الاسم الجديد لهذا المشرف:\n(مثال: ممثل الشعبة A أو د. مصطفى)",
            kb([[InlineKeyboardButton("إلغاء", callback_data="a:adm:%d" % aid)]]),
        )

    if act == "del":
        kind, rid = p[2], int(p[3])
        if kind == "s":
            s = db.subject(rid)
            txt = "⚠️ حذف المادة <b>%s</b> مع كل محاضراتها وملفاتها؟" % esc(s["name"])
        else:
            l = db.lecture(rid)
            txt = "⚠️ حذف المحاضرة <b>%s</b> مع كل ملفاتها؟" % esc(l["name"])
        return await show(update, txt, kb([[
            InlineKeyboardButton("✅ نعم احذف", callback_data="a:delok:%s:%d" % (kind, rid)),
            InlineKeyboardButton("❌ لا", callback_data="a:%s:%d" % (kind, rid)),
        ]]))

    if act == "delok":
        kind, rid = p[2], int(p[3])
        if kind == "s":
            db.delete_subject(rid)
            return await admin_subjects(update)
        l = db.lecture(rid)
        sid = l["subject_id"] if l else None
        db.delete_lecture(rid)
        if sid:
            return await admin_subject(update, sid)
        return await admin_subjects(update)

    if act == "delf":
        fid = int(p[2])
        row = db.file_row(fid)
        lid = row["lecture_id"] if row else None
        db.delete_file(fid)
        if lid:
            return await admin_files(update, lid)
        return await admin_subjects(update)

    if act in ("ord", "omv") and p[2] == "s" and scope_ids(uid) is not None:
        return await q.answer("ترتيب المواد للمالك أو المشرف العام بس", show_alert=True)

    if act == "ord":
        if p[2] == "s":
            return await order_screen(update, "s", sel=int(p[3]))
        return await order_screen(update, "l", sid=int(p[3]), sel=int(p[4]))

    if act == "omv":
        kind, rid, direction = p[2], int(p[3]), p[4]
        if kind == "s":
            db.move("subjects", rid, direction)
            return await order_screen(update, "s", sel=rid)
        l = db.lecture(rid)
        db.move("lectures", rid, direction, "subject_id", l["subject_id"])
        return await order_screen(update, "l", sid=l["subject_id"], sel=rid)

    if act == "up":
        lid = int(p[2])
        STATE[uid] = {"want": "upload", "lid": lid, "count": 0, "last_msg_id": None}
        l = db.lecture(lid)
        return await show(
            update,
            ("📤 <b>وضع الرفع</b> — %s\n\n"
             "أرسل الآن الملفات (PDF، صور، فيديو، صوت…) وحدة وحدة أو دفعة.\n"
             "كل ملف يترسل ينحفظ تلقائياً بهذه المحاضرة.\n\n"
             "لما تخلص اضغط ✅ انتهيت.") % esc(l["name"]),
            kb([[InlineKeyboardButton("✅ انتهيت", callback_data="a:endup:%d" % lid)]]),
        )

    if act == "endup":
        lid = int(p[2])
        st = STATE.pop(uid, {})
        n = st.get("count", 0)
        if st.get("last_msg_id"):
            await safe_delete(context.bot, q.message.chat_id, st["last_msg_id"])
        await context.bot.send_message(q.message.chat_id, "✅ خلصنا الرفع. الملفات المضافة: %d" % n)
        if n > 0:
            asyncio.create_task(notify_new_lecture(context, lid))
        return await admin_lecture(update, context, lid)

    # --- إدارة المشرفين ---
    if act == "admins":
        return await admins_list(update, context)

    if act == "adm":
        return await admin_one(update, context, int(p[2]))

    if act == "addadm":
        STATE[uid] = {"want": "admin_id", "prompt_msg_id": q.message.message_id}
        return await show(
            update,
            "👮 <b>إضافة مشرف جديد</b>\n\n"
            "أرسل آيدي الشخص (رقم)، أو حوّل أي رسالة منه للبوت.\n"
            "سيسحب البوت اسمه من تليجرام فوراً.",
            kb([[InlineKeyboardButton("إلغاء", callback_data="a:admins")]]),
        )

    if act == "perm":
        aid, key = int(p[2]), p[3]
        a = db.admin_row(aid)
        if a and key in ALL_PERMS and a["role"] != "owner":
            cur = {x for x in (a["perms"] or "").split(",") if x}
            cur.discard(key) if key in cur else cur.add(key)
            db.set_admin_field(aid, "perms", ",".join(x for x in ALL_PERMS if x in cur))
        return await admin_one(update, context, aid)

    if act == "role":
        aid = int(p[2])
        if aid == uid:
            return await q.answer("ما تكدر تغير دورك بنفسك", show_alert=True)
        a = db.admin_row(aid)
        if a:
            new_role = "admin" if a["role"] == "owner" else "owner"
            db.set_admin_field(aid, "role", new_role)
        return await admin_one(update, context, aid)

    if act == "scope":
        return await admin_scope(update, int(p[2]))

    if act == "scall":
        aid = int(p[2])
        db.set_admin_field(aid, "scope", "all")
        return await admin_scope(update, aid)

    if act == "sc":
        aid, sid = int(p[2]), int(p[3])
        ids = scope_ids(aid)
        ids = set() if ids is None else set(ids)
        ids.discard(sid) if sid in ids else ids.add(sid)
        db.set_admin_field(aid, "scope", ",".join(str(x) for x in sorted(ids)) or "all")
        return await admin_scope(update, aid)

    if act == "admdel":
        aid = int(p[2])
        if aid == uid:
            return await q.answer("ما تكدر تحذف نفسك", show_alert=True)
        a = db.admin_row(aid)
        return await show(
            update,
            "⚠️ إزالة <b>%s</b> من المشرفين؟" % esc((a["name"] if a else None) or aid),
            kb([[
                InlineKeyboardButton("✅ نعم", callback_data="a:admdelok:%d" % aid),
                InlineKeyboardButton("❌ لا", callback_data="a:adm:%d" % aid),
            ]]),
        )

    if act == "admdelok":
        aid = int(p[2])
        if aid != uid:
            db.remove_admin(aid)
        return await admins_list(update, context)

    if act == "bc":
        STATE[uid] = {"want": "broadcast", "prompt_msg_id": q.message.message_id}
        return await show(
            update,
            "📢 أرسل الرسالة الي تريد تنرسل لكل مستخدمي البوت:",
            kb([[InlineKeyboardButton("إلغاء", callback_data="a:home")]]),
        )


# ============================ الرسائل النصية ============================
async def notify_new_admin(context, target):
    try:
        await context.bot.send_message(
            target,
            "👮 صرت مشرف ببوت المحاضرات.\nأرسل /admin حتى تفتح لوحة التحكم.",
        )
    except Exception as e:
        log.info("ما كدرت أبلغ المشرف الجديد %s: %s", target, e)


def resolve_target(msg, text=""):
    origin = getattr(msg, "forward_origin", None)
    sender = getattr(origin, "sender_user", None) if origin else None
    if sender:
        return sender.id, sender.full_name
    t = (text or "").strip()
    if t.lstrip("-").isdigit():
        return int(t), ""
    return None, ""


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    # --- معالجة الضغط على أزرار الكيبورد السفلي ---
    if text == "📚 مواد قسمي":
        STATE.pop(uid, None)
        await safe_delete(context.bot, chat_id, update.message.message_id)
        return await user_home(update, context)

    if text == "👑 إدارة التحكم" and is_owner(uid):
        STATE.pop(uid, None)
        await safe_delete(context.bot, chat_id, update.message.message_id)
        return await admin_home(update)

    if text == "🛠 إدارة القسم" and is_admin(uid):
        STATE.pop(uid, None)
        await safe_delete(context.bot, chat_id, update.message.message_id)
        return await admin_home(update)

    if text == "💬 الدعم والمساعدة":
        await safe_delete(context.bot, chat_id, update.message.message_id)
        support_url = f"https://t.me/{SUPPORT_USERNAME}"
        support_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 مراسلة الدعم الفني (@raadjv)", url=support_url)]
        ])
        support_txt = (
            "👋 <b>أهلاً بك في قسم الدعم والاستفسارات</b>\n\n"
            "إذا واجهتك أي مشكلة تقنية أو كان لديك استفسار يخص المحاضرات والمواد، "
            "اضغط على الزر بالأسفل للتواصل المباشر عبر تليجرام:"
        )
        return await context.bot.send_message(
            chat_id,
            support_txt,
            parse_mode=ParseMode.HTML,
            reply_markup=support_markup
        )

    st = STATE.get(uid)
    if not st or not is_admin(uid):
        return await user_home(update, context)

    want = st["want"]

    prompt_id = st.get("prompt_msg_id")
    if prompt_id:
        await safe_delete(context.bot, chat_id, prompt_id)
    await safe_delete(context.bot, chat_id, update.message.message_id)

    if want == "subject_name":
        names = [x.strip() for x in text.split("\n") if x.strip()]
        for n in names:
            db.add_subject(n)
        STATE.pop(uid, None)
        return await admin_subjects(update)

    if want == "lecture_name":
        sid = st["sid"]
        names = [x.strip() for x in text.split("\n") if x.strip()]
        last = None
        for n in names:
            last = db.add_lecture(sid, n)
        STATE.pop(uid, None)
        if len(names) == 1 and last:
            STATE[uid] = {"want": "upload", "lid": last, "count": 0, "last_msg_id": None}
            l = db.lecture(last)
            sent_m = await context.bot.send_message(
                chat_id,
                "✅ تمت إضافة <b>%s</b>\n\n📤 أرسل ملفاتها الآن، ولما تخلص اضغط ✅ انتهيت." % esc(l["name"]),
                parse_mode=ParseMode.HTML,
                reply_markup=kb([[InlineKeyboardButton("✅ انتهيت", callback_data="a:endup:%d" % last)]]),
            )
            STATE[uid]["last_msg_id"] = sent_m.message_id
            return
        return await admin_subject(update, sid)

    if want == "rename":
        kind, rid = st["kind"], st["id"]
        STATE.pop(uid, None)
        if kind == "s":
            db.rename_subject(rid, text)
            return await admin_subject(update, rid)
        db.rename_lecture(rid, text)
        return await admin_lecture(update, context, rid)

    if want == "rename_admin":
        aid = st["aid"]
        STATE.pop(uid, None)
        db.set_admin_field(aid, "name", text)
        return await admin_one(update, context, aid)

    if want == "admin_id":
        if not is_owner(uid):
            STATE.pop(uid, None)
            return await context.bot.send_message(chat_id, "هذا القسم لمالك البوت فقط.")
        target, tname = resolve_target(update.message, text)
        if target is None:
            return await context.bot.send_message(
                chat_id,
                "ما كدرت أعرف الشخص 🤔\nأرسل آيديه كرقم، أو حوّل رسالة منه."
            )
        if target == uid:
            return await context.bot.send_message(chat_id, "أنت أصلاً المالك 👑")

        if not tname:
            try:
                c = await context.bot.get_chat(target)
                tname = c.full_name or c.title or c.username or str(target)
            except Exception:
                row = db.user_row(target)
                tname = (row["name"] if row else "") or str(target)

        STATE.pop(uid, None)
        db.add_admin(target, tname, role="admin", perms="add,upload", scope="all")
        await notify_new_admin(context, target)
        return await admin_one(update, context, target)

    if want == "broadcast":
        STATE.pop(uid, None)
        ok = fail = 0
        for tid in db.all_user_ids():
            try:
                await context.bot.send_message(tid, text)
                ok += 1
            except Exception:
                fail += 1
        return await context.bot.send_message(chat_id, "📢 وصلت لـ %d مستخدم، وفشلت مع %d." % (ok, fail))

    if want == "upload":
        await context.bot.send_message(chat_id, "أرسل ملف 📎 أو اضغط ✅ انتهيت.")


# ============================ استقبال الملفات ============================
def extract(msg):
    if msg.document:
        return "document", msg.document.file_id, msg.document.file_name
    if msg.photo:
        return "photo", msg.photo[-1].file_id, "صورة"
    if msg.video:
        return "video", msg.video.file_id, msg.video.file_name or "فيديو"
    if msg.audio:
        return "audio", msg.audio.file_id, msg.audio.file_name or msg.audio.title or "صوت"
    if msg.voice:
        return "voice", msg.voice.file_id, "تسجيل صوتي"
    if msg.animation:
        return "animation", msg.animation.file_id, "صورة متحركة"
    if msg.video_note:
        return "video_note", msg.video_note.file_id, "فيديو دائري"
    return None, None, None


async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = STATE.get(uid)
    if not st or not is_admin(uid):
        return
    if st.get("want") != "upload" or not can(uid, "upload"):
        return
    kind, fid, title = extract(update.message)
    if not fid:
        return

    lid = st["lid"]
    chat_id = update.effective_chat.id
    db.add_file(lid, fid, kind, title, update.message.caption)
    st["count"] = st.get("count", 0) + 1
    total = db.count_files(lid)

    if st.get("last_msg_id"):
        await safe_delete(context.bot, chat_id, st["last_msg_id"])

    sent_m = await update.message.reply_text(
        "✅ انحفظ: %s\n📎 مجموع ملفات المحاضرة: %d" % (esc(title), total),
        parse_mode=ParseMode.HTML,
        reply_markup=kb([[InlineKeyboardButton("✅ انتهيت", callback_data="a:endup:%d" % lid)]]),
    )
    st["last_msg_id"] = sent_m.message_id


# ============================ الأوامر ============================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    db.touch_user(u.id, u.full_name, u.username)
    STATE.pop(u.id, None)

    await safe_delete(context.bot, update.effective_chat.id, update.message.message_id)

    # إظهار وتثبيت الأزرار السفلية بشكل دائم ومستمر
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="👇 تم تفعيل وتثبيت قائمة الأزرار:",
        reply_markup=get_role_reply_keyboard(u.id),
    )

    args = context.args or []
    if args and args[0].startswith("l"):
        try:
            return await send_lecture(update, context, int(args[0][1:]))
        except ValueError:
            pass
    await user_home(update, context)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not admins():
        db.add_admin(u.id, u.full_name, role="owner", perms=",".join(ALL_PERMS))
        log.info("تم تعيين مالك البوت: %s (%s)", u.full_name, u.id)
        await update.message.reply_text("👑 صرت مالك البوت بكل الصلاحيات. آيديك: %d" % u.id)
    if not is_admin(u.id):
        return await update.message.reply_text("هذا الأمر للمشرف فقط.")
    await admin_home(update)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text("🆔 الآيدي مالتك: <code>%d</code>" % u.id, parse_mode=ParseMode.HTML)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    STATE.pop(update.effective_user.id, None)
    await update.message.reply_text("تم الإلغاء.")


async def on_error(update, context):
    log.error("خطأ: %s", context.error)


def run_bot():
    if not TOKEN or TOKEN.startswith("ضع"):
        raise SystemExit("⚠️ ضع توكن البوت داخل config.json أولاً.")
    
    db.init()
    
    # زيادة مهلة الاتصال لمنع الفصل المفاجئ عند ضعف الشبكة
    app = (
        Application.builder()
        .token(TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .build()
    )
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(
        MessageHandler(
            filters.Document.ALL
            | filters.PHOTO
            | filters.VIDEO
            | filters.AUDIO
            | filters.VOICE
            | filters.ANIMATION
            | filters.VIDEO_NOTE,
            on_file,
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    
    log.info("البوت اشتغل بنجاح وبأعلى كفاءة ✅")
    
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
        poll_interval=1.0
    )


def main():
    while True:
        try:
            run_bot()
        except Conflict:
            log.error("❌ نسخة ثانية شغالة بنفس التوكن! تأكد من إغلاق باقي العمليات.")
            break
        except InvalidToken:
            log.error("❌ التوكن غير صحيح، تأكد من ملف config.json.")
            break
        except Exception as e:
            log.warning("⚠️ انقطع الاتصال أو حدث خطأ شبكي: %s", e)
            log.info("🔄 جاري إعادة المحاولة والاتصال بعد 5 ثوانٍ...")
            time.sleep(5)


if __name__ == "__main__":
    main()