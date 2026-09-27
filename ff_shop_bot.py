#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOT BÁN ACC FREE FIRE - chạy trên Termux
Yêu cầu: pip install python-telegram-bot --break-system-packages

Chạy: python ff_shop_bot.py
(Bot sẽ hỏi TOKEN khi chạy lần đầu và lưu lại vào file token.txt)

===================== LỆNH NGƯỜI DÙNG =====================
/start          - bắt đầu, kiểm tra tham gia nhóm, hiện menu
==================== LỆNH ADMIN (uid 7907990385) ===========
/admin                      - hiện panel quản trị
/addxu <id> <số_xu>         - cộng xu cho user
/chinhkho                   - bật chế độ thêm kho bằng menu bấm
/newadmin <uid>             - nâng uid lên admin
/stop                       - bật/tắt chế độ bảo trì
/thongbao <nội dung>        - gửi thông báo tới toàn bộ user
==============================================================
"""

import os
import random
import string
import logging
import threading
import psycopg2
import psycopg2.extras
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================= CẤU HÌNH =================
DEFAULT_ADMIN_ID = 7907990385
DATABASE_URL = os.environ.get("DATABASE_URL", "")  # connection string Postgres (Render/Supabase)
TOKEN_FILE = "token.txt"

REQUIRED_GROUPS = [
    {"name": "Anh Em Hot Tele", "username": "@anhemhottele", "link": "https://t.me/anhemhottele"},
    {"name": "Nhóm Chat Hot War", "username": "@nhomchathotwar", "link": "https://t.me/nhomchathotwar"},
]

LEVEL_PRICES = {5: 20, 30: 30}
XU_PER_REF = 2
# ==============================================

logging.basicConfig(level=logging.INFO)

# state tạm cho admin khi đang ở chế độ /chinhkho
ADMIN_STATE = {}

# ---------------- DATABASE (Postgres, 1 kết nối dùng chung) ----------------
class _DBWrapper:
    """Bọc psycopg2 để dùng được y hệt kiểu conn.execute(...).fetchone() như sqlite3."""

    def __init__(self, dsn):
        self._dsn = dsn
        self._conn = psycopg2.connect(dsn)
        self._conn.autocommit = False

    def _ensure_alive(self):
        # Nếu kết nối bị rớt (Render/Supabase free hay ngắt kết nối khi rảnh), tự kết nối lại
        if self._conn.closed:
            self._conn = psycopg2.connect(self._dsn)

    def execute(self, query, params=()):
        self._ensure_alive()
        cur = self._conn.cursor()
        try:
            cur.execute(query, params)
        except psycopg2.Error:
            self._conn.rollback()
            raise
        return cur

    def commit(self):
        self._conn.commit()


_CONN = _DBWrapper(DATABASE_URL)
_CONN.execute("""CREATE TABLE IF NOT EXISTS users (
    id BIGINT PRIMARY KEY, name TEXT, xu INTEGER DEFAULT 0,
    is_admin INTEGER DEFAULT 0, ref_count INTEGER DEFAULT 0,
    joined INTEGER DEFAULT 0)""")
_CONN.execute("""CREATE TABLE IF NOT EXISTS accounts (
    id SERIAL PRIMARY KEY, level INTEGER,
    username TEXT, password TEXT, price INTEGER, sold INTEGER DEFAULT 0)""")
_CONN.execute("""CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT)""")
_CONN.commit()


def db():
    """Trả về kết nối chung (không tự đóng - tái sử dụng để đỡ lag)."""
    return _CONN


def get_setting(key, default=None):
    row = db().execute("SELECT value FROM settings WHERE key=%s", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key, value):
    db().execute(
        "INSERT INTO settings (key,value) VALUES (%s,%s) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value))
    )
    db().commit()


def is_admin(uid: int) -> bool:
    if uid == DEFAULT_ADMIN_ID:
        return True
    row = db().execute("SELECT is_admin FROM users WHERE id=%s", (uid,)).fetchone()
    return bool(row and row[0])


def ensure_user(uid: int, name: str):
    row = db().execute("SELECT id FROM users WHERE id=%s", (uid,)).fetchone()
    if not row:
        db().execute("INSERT INTO users (id, name) VALUES (%s,%s)", (uid, name))
        db().commit()


def is_maintenance() -> bool:
    return get_setting("maintenance", "0") == "1"


def gen_account():
    """Sinh username/password ngẫu nhiên cho 1 acc."""
    username = "FF" + "".join(random.choices(string.digits, k=8))
    password = "".join(random.choices(string.ascii_letters + string.digits, k=8))
    return username, password


# ---------------- GIAO DIỆN ----------------
DIVIDER = "✨━━━━━━━━━━━━━✨"


def main_menu_text():
    return (
        "👑 <b>SHOP ACC FREE FIRE VIP</b> 👑\n"
        f"{DIVIDER}\n"
        "💎 Chọn kho acc bên dưới để mua\n"
        f"🎁 Mời bạn = +{XU_PER_REF}xu / người ✨"
    )


def main_menu_kb():
    kb = [
        [InlineKeyboardButton(f"🔥 Kho ACC Lv5 — {LEVEL_PRICES[5]}xu ⚡", callback_data="buy_5")],
        [InlineKeyboardButton(f"💎 Kho ACC Lv30 — {LEVEL_PRICES[30]}xu 👑", callback_data="buy_30")],
        [InlineKeyboardButton("💰 Số Dư Xu", callback_data="balance"),
         InlineKeyboardButton("🔗 Giới Thiệu ✨", callback_data="reflink")],
        [InlineKeyboardButton("🔄 Làm Mới", callback_data="back_menu")],
    ]
    return InlineKeyboardMarkup(kb)


def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Quay lại menu", callback_data="back_menu")]])


def join_kb():
    kb = [[InlineKeyboardButton(f"➕ Vào {g['name']}", url=g["link"])] for g in REQUIRED_GROUPS]
    kb.append([InlineKeyboardButton("✅ Tôi đã tham gia", callback_data="check_join")])
    return InlineKeyboardMarkup(kb)


async def check_membership(context: ContextTypes.DEFAULT_TYPE, uid: int) -> bool:
    for g in REQUIRED_GROUPS:
        try:
            member = await context.bot.get_chat_member(g["username"], uid)
            if member.status in ("left", "kicked"):
                return False
        except Exception:
            return False
    return True


async def show_main_menu(update_or_query, edit: bool):
    if edit:
        await update_or_query.message.edit_text(
            main_menu_text(), parse_mode="HTML", reply_markup=main_menu_kb()
        )
    else:
        await update_or_query.reply_text(
            main_menu_text(), parse_mode="HTML", reply_markup=main_menu_kb()
        )


# ---------------- MAINTENANCE GUARD ----------------
async def blocked_by_maintenance(update: Update) -> bool:
    uid = update.effective_user.id
    if is_maintenance() and not is_admin(uid):
        await update.effective_message.reply_text(
            "🛡️ <b>Hệ Thống Đang Bảo Trì</b> ⚙️\nQuay lại sau ít phút nhé!", parse_mode="HTML"
        )
        return True
    return False


# ---------------- USER: /start ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await blocked_by_maintenance(update):
        return
    user = update.effective_user
    ensure_user(user.id, user.full_name)

    if context.args:
        try:
            ref_id = int(context.args[0])
            row = db().execute("SELECT joined FROM users WHERE id=%s", (user.id,)).fetchone()
            already_joined = row and row[0]
            if ref_id != user.id and not already_joined:
                db().execute("UPDATE users SET xu = xu + %s WHERE id=%s", (XU_PER_REF, ref_id))
                db().execute("UPDATE users SET ref_count = ref_count + 1 WHERE id=%s", (ref_id,))
                db().commit()
        except ValueError:
            pass

    if not await check_membership(context, user.id):
        await update.message.reply_text(
            "👑 <b>CHÀO MỪNG ĐẾN SHOP ACC FREE FIRE VIP</b> 👑\n"
            f"{DIVIDER}\n"
            "Tham gia đủ 2 nhóm bên dưới rồi bấm ✅ Tôi đã tham gia:",
            parse_mode="HTML",
            reply_markup=join_kb(),
        )
        return

    db().execute("UPDATE users SET joined=1 WHERE id=%s", (user.id,))
    db().commit()
    await show_main_menu(update.message, edit=False)


# ---------------- CALLBACK QUERIES (menu bấm) ----------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    data = query.data

    if is_maintenance() and not is_admin(uid):
        await query.answer("🛡️ Hệ thống đang bảo trì", show_alert=True)
        return

    # phản hồi tức thì để không có cảm giác lag khi chờ xử lý
    await query.answer()

    if data == "back_menu":
        await show_main_menu(query, edit=True)
        return

    if data == "check_join":
        if await check_membership(context, uid):
            ensure_user(uid, query.from_user.full_name)
            db().execute("UPDATE users SET joined=1 WHERE id=%s", (uid,))
            db().commit()
            await show_main_menu(query, edit=True)
        else:
            await query.answer("❌ Bạn chưa tham gia đủ 2 nhóm!", show_alert=True)
        return

    if data == "balance":
        row = db().execute("SELECT xu, ref_count FROM users WHERE id=%s", (uid,)).fetchone()
        xu, refs = (row if row else (0, 0))
        await query.message.edit_text(
            f"💰 <b>VÍ XU CỦA BẠN</b> 💎\n{DIVIDER}\n"
            f"⚡ Xu hiện có: <b>{xu} xu</b>\n"
            f"🎁 Số người đã mời: <b>{refs}</b>",
            parse_mode="HTML",
            reply_markup=back_kb(),
        )
        return

    if data == "reflink":
        bot_username = (await context.bot.get_me()).username
        link = f"https://t.me/{bot_username}?start={uid}"
        await query.message.edit_text(
            f"🔗 <b>LINK GIỚI THIỆU VIP</b> ✨\n{DIVIDER}\n"
            f"{link}\n\n"
            f"🎁 Mỗi người vào qua link = +{XU_PER_REF}xu",
            parse_mode="HTML",
            reply_markup=back_kb(),
        )
        return

    if data in ("buy_5", "buy_30"):
        level = 5 if data == "buy_5" else 30
        price = LEVEL_PRICES[level]
        urow = db().execute("SELECT xu FROM users WHERE id=%s", (uid,)).fetchone()
        xu = urow[0] if urow else 0
        if xu < price:
            await query.message.edit_text(
                f"❌ <b>KHÔNG ĐỦ XU</b> 💔\n{DIVIDER}\n"
                f"⚡ Cần {price}xu, bạn đang có {xu}xu.\n"
                "🎁 Mời bạn bè để kiếm thêm xu nhé!",
                parse_mode="HTML",
                reply_markup=back_kb(),
            )
            return
        acc = db().execute(
            "SELECT id, username, password FROM accounts WHERE level=%s AND sold=0 LIMIT 1", (level,)
        ).fetchone()
        if not acc:
            await query.message.edit_text(
                f"📦 <b>TẠM HẾT HÀNG</b> 😢\n{DIVIDER}\nKho acc Lv{level} đang hết, quay lại sau nhé.",
                parse_mode="HTML",
                reply_markup=back_kb(),
            )
            return
        db().execute("UPDATE users SET xu = xu - %s WHERE id=%s", (price, uid))
        db().execute("UPDATE accounts SET sold=1 WHERE id=%s", (acc[0],))
        db().commit()
        await query.message.edit_text(
            f"🎉 <b>MUA THÀNH CÔNG ACC LV{level}</b> 👑\n{DIVIDER}\n"
            f"👤 Tài khoản: <code>{acc[1]}</code>\n"
            f"🔑 Mật khẩu: <code>{acc[2]}</code>\n\n"
            "⚠️ Đổi mật khẩu ngay sau khi nhận.",
            parse_mode="HTML",
            reply_markup=back_kb(),
        )
        return

    # ---- callback dùng trong /chinhkho ----
    if data.startswith("kho_level_"):
        level = int(data.split("_")[-1])
        ADMIN_STATE[uid] = {"mode": "adding_stock", "level": level}
        await query.message.edit_text(
            f"📥 <b>THÊM KHO LV{level}</b> 📦\n{DIVIDER}\n"
            "Nhập <b>số lượng</b> acc muốn thêm.\nVD: gõ <code>10</code> để tự sinh 10 acc ngẫu nhiên.",
            parse_mode="HTML",
        )
        return


# ---------------- ADMIN: /admin ----------------
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    total_users = db().execute("SELECT COUNT(*) FROM users").fetchone()[0]
    stock5 = db().execute("SELECT COUNT(*) FROM accounts WHERE level=5 AND sold=0").fetchone()[0]
    stock30 = db().execute("SELECT COUNT(*) FROM accounts WHERE level=30 AND sold=0").fetchone()[0]
    status = "🛡️ Đang bảo trì" if is_maintenance() else "🟢 Đang hoạt động ⚡"
    await update.message.reply_text(
        f"👑 <b>PANEL QUẢN TRỊ VIP</b> 🛡️\n{DIVIDER}\n"
        f"📡 Trạng thái: {status}\n"
        f"👥 Tổng user: {total_users}\n"
        f"📦 Kho Lv5: {stock5} | 💎 Kho Lv30: {stock30}\n"
        f"{DIVIDER}\n"
        "⚙️ <b>Lệnh quản trị:</b>\n"
        "💰 /addxu &lt;id&gt; &lt;số_xu&gt;\n"
        "📥 /chinhkho\n"
        "👑 /newadmin &lt;uid&gt;\n"
        "🛡️ /stop\n"
        "📢 /thongbao &lt;nội_dung&gt;",
        parse_mode="HTML",
    )


async def addxu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    if len(context.args) < 2:
        await update.message.reply_text("⚠️ Dùng: /addxu <id> <số_xu>")
        return
    target, amount = int(context.args[0]), int(context.args[1])
    ensure_user(target, "")
    db().execute("UPDATE users SET xu = xu + %s WHERE id=%s", (amount, target))
    db().commit()
    await update.message.reply_text(f"✅ Đã cộng {amount}xu 💎 cho user {target}.")
    try:
        await context.bot.send_message(target, f"🎁 Bạn được admin cộng {amount}xu! 💎")
    except Exception:
        pass


async def chinhkho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 Thêm Kho Lv5 ⚡", callback_data="kho_level_5")],
        [InlineKeyboardButton("💎 Thêm Kho Lv30 👑", callback_data="kho_level_30")],
    ])
    await update.message.reply_text("📦 <b>Chọn kho cần thêm:</b>", reply_markup=kb, parse_mode="HTML")


async def xong(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in ADMIN_STATE:
        ADMIN_STATE.pop(uid, None)
        await update.message.reply_text("✅ Đã thoát chế độ thêm kho.")


async def newadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    if not context.args:
        await update.message.reply_text("⚠️ Dùng: /newadmin <uid>")
        return
    target = int(context.args[0])
    ensure_user(target, "")
    db().execute("UPDATE users SET is_admin=1 WHERE id=%s", (target,))
    db().commit()
    await update.message.reply_text(f"👑 Đã nâng {target} lên admin.")


async def stop_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    new_val = "0" if is_maintenance() else "1"
    set_setting("maintenance", new_val)
    msg = "🛡️ Bot đã chuyển sang chế độ bảo trì ⚙️" if new_val == "1" else "🟢 Bot đã hoạt động trở lại ⚡"
    await update.message.reply_text(msg)


async def thongbao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    if not context.args:
        await update.message.reply_text("⚠️ Dùng: /thongbao <nội_dung>")
        return
    text = f"📢 <b>THÔNG BÁO TỪ ADMIN</b> 🔔\n{DIVIDER}\n" + " ".join(context.args)
    ids = [r[0] for r in db().execute("SELECT id FROM users").fetchall()]
    sent = 0
    for i in ids:
        try:
            await context.bot.send_message(i, text, parse_mode="HTML")
            sent += 1
        except Exception:
            pass
    await update.message.reply_text(f"✅ Đã gửi thông báo tới {sent}/{len(ids)} user.")


# ---------------- nhận số lượng khi admin đang /chinhkho ----------------
async def handle_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = ADMIN_STATE.get(uid)
    if not state or state.get("mode") != "adding_stock":
        return
    level = state["level"]
    price = LEVEL_PRICES.get(level, 0)
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("⚠️ Vui lòng nhập một số nguyên, VD: 10")
        return
    qty = int(text)
    if qty <= 0 or qty > 500:
        await update.message.reply_text("⚠️ Số lượng phải từ 1 đến 500.")
        return
    for _ in range(qty):
        username, password = gen_account()
        db().execute(
            "INSERT INTO accounts (level, username, password, price) VALUES (%s,%s,%s,%s)",
            (level, username, password, price),
        )
    db().commit()
    ADMIN_STATE.pop(uid, None)
    await update.message.reply_text(f"✅ Đã tự sinh và thêm {qty} acc ngẫu nhiên vào kho Lv{level}.")


def get_token() -> str:
    # Ưu tiên biến môi trường BOT_TOKEN (dùng khi deploy trên Render)
    env_token = os.environ.get("BOT_TOKEN")
    if env_token:
        return env_token.strip()
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r") as f:
            token = f.read().strip()
            if token:
                return token
    token = input("🔑 Nhập TOKEN bot Telegram: ").strip()
    with open(TOKEN_FILE, "w") as f:
        f.write(token)
    return token


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Bot dang chay.".encode("utf-8"))

    def log_message(self, format, *args):
        pass  # tắt log HTTP cho đỡ rác console


def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    server.serve_forever()


def main():
    # Render Web Service bắt buộc phải bind vào $PORT, nên chạy 1 web server
    # nhỏ song song ở thread riêng chỉ để "sống", còn bot vẫn chạy polling như cũ.
    threading.Thread(target=run_health_server, daemon=True).start()

    token = get_token()
    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("addxu", addxu))
    app.add_handler(CommandHandler("chinhkho", chinhkho))
    app.add_handler(CommandHandler("xong", xong))
    app.add_handler(CommandHandler("newadmin", newadmin))
    app.add_handler(CommandHandler("stop", stop_toggle))
    app.add_handler(CommandHandler("thongbao", thongbao))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_text))

    print("✅ Bot đã hoạt động!")
    app.run_polling()


if __name__ == "__main__":
    main()
