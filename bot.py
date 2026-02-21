import logging
import asyncio
import threading
import telebot
from telebot import types
from telebot.types import MessageEntity
import database as db
import ton_monitor
import session_manager
from config import BOT_TOKEN, ADMIN_ID, BOT_WALLET

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

# ─────────────────────── PREMIUM EMOJI ENGINE ─────────
# Bots CAN send premium custom emojis using MessageEntity
# with type="custom_emoji" — no HTML tags needed.
# Each emoji occupies exactly 2 chars in the string (the
# placeholder we put is the fallback Unicode char, 1 char,
# but Telegram counts emoji as 2 UTF-16 code units).
# We build messages as plain text + entity list.

EMOJI_IDS = {
    "👋": "6104922173015597346",   # wave
    "👤": "6107017202228009498",   # profile
    "⚠️": "6106898459267177284",   # warning
    "🔒": "6106902616795519273",   # secure
    "🤖": "6107323579425104140",   # bot
    "🎁": "6107325885822540958",   # gift
    "✅": "6106981506754814207",   # verified tick
    "🏪": "6107212468621154692",   # shop
    "💎": "6107289979895945232",   # fragment logo
    "🪙": "6106898347598027963",   # toncoin
    "💲": "6107061783988542265",   # dollar
    "📈": "6104943961384688402",   # graph
    "🏠": "6008258140108231117",   # main menu
    "⏱": "5900104897885376843",    # timer/clock
    "☑️": "5951665890079544884",    # verified check
}

def build(text: str):
    """
    Parse a string containing emoji placeholders marked with [E:char].
    Returns (plain_text, entities_list) ready to pass to send_message.

    Usage in message strings:
        f"[E:👋] Welcome!\n[E:🪙] Price: {price} TON"

    Each [E:X] is replaced with the emoji char X in the final string,
    and a custom_emoji MessageEntity is attached at that position.
    """
    import re
    plain   = ""
    entities = []
    pattern  = re.compile(r'\[E:(.+?)\]')
    cursor   = 0
    bold_ranges = []

    # First pass — resolve [E:x] markers and **bold** markers
    # We support **text** for bold inline
    i = 0
    segments = pattern.split(text)
    # pattern.split gives: [before, emoji_char, between, emoji_char, ...]
    result_text = ""
    result_entities = []

    parts = pattern.split(text)
    # parts alternates: plain_text, emoji_char, plain_text, emoji_char ...
    pos = 0
    idx = 0
    while idx < len(parts):
        segment = parts[idx]
        if idx % 2 == 0:
            # Plain text segment — handle **bold**
            bold_pat = re.compile(r'\*\*(.+?)\*\*', re.DOTALL)
            last = 0
            for m in bold_pat.finditer(segment):
                before = segment[last:m.start()]
                result_text += before
                pos += len(before.encode('utf-16-le')) // 2
                bold_start = pos
                inner = m.group(1)
                result_text += inner
                inner_len = len(inner.encode('utf-16-le')) // 2
                result_entities.append(
                    MessageEntity(type="bold", offset=bold_start, length=inner_len)
                )
                pos += inner_len
                last = m.end()
            tail = segment[last:]
            result_text += tail
            pos += len(tail.encode('utf-16-le')) // 2
        else:
            # Emoji char
            emoji_char = segment
            emoji_id   = EMOJI_IDS.get(emoji_char)
            result_text += emoji_char
            char_len = len(emoji_char.encode('utf-16-le')) // 2
            if emoji_id:
                result_entities.append(
                    MessageEntity(
                        type="custom_emoji",
                        offset=pos,
                        length=char_len,
                        custom_emoji_id=emoji_id
                    )
                )
            pos += char_len
        idx += 1

    return result_text, result_entities


def send(chat_id, text, reply_markup=None):
    """Send a message with premium emojis. Falls back to plain text if entities fail."""
    plain, entities = build(text)
    try:
        bot.send_message(
            chat_id,
            plain,
            entities=entities if entities else None,
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.warning(f"send() with entities failed ({e}), falling back to plain")
        bot.send_message(chat_id, plain, reply_markup=reply_markup)


def edit(chat_id, message_id, text, reply_markup=None):
    """Edit a message with premium emojis. Falls back to plain text if entities fail."""
    plain, entities = build(text)
    try:
        bot.edit_message_text(
            plain,
            chat_id,
            message_id,
            entities=entities if entities else None,
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.warning(f"edit() with entities failed ({e}), falling back to plain")
        bot.edit_message_text(plain, chat_id, message_id, reply_markup=reply_markup)


# ─────────────────────── STATE ────────────────────────
user_states = {}
state_data  = {}

def set_state(uid, state, data=None):
    user_states[uid] = state
    if data is not None:
        state_data[uid] = data

def get_state(uid):      return user_states.get(uid)
def get_state_data(uid): return state_data.get(uid)

def clear_state(uid):
    user_states.pop(uid, None)
    state_data.pop(uid, None)

telethon_loop = asyncio.new_event_loop()

def run_async(coro):
    future = asyncio.run_coroutine_threadsafe(coro, telethon_loop)
    return future.result(timeout=60)


# ─────────────────────── KEYBOARDS ────────────────────

def main_menu(user_id):
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.row(types.KeyboardButton("💰 Add Balance"), types.KeyboardButton("🛒 Buy Account"))
    m.row(types.KeyboardButton("👤 My Profile"),  types.KeyboardButton("📋 My Purchases"))
    if user_id == ADMIN_ID:
        m.row(types.KeyboardButton("⚙️ Admin Panel"))
    return m


def admin_menu():
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.row(types.KeyboardButton("➕ Add Account"),  types.KeyboardButton("📦 Stock Info"))
    m.row(types.KeyboardButton("💵 Change Price"), types.KeyboardButton("👥 All Users"))
    m.row(types.KeyboardButton("📢 Broadcast"),    types.KeyboardButton("💳 Add User Balance"))
    m.row(types.KeyboardButton("📋 Manage Stock"))
    m.row(types.KeyboardButton("🔙 Back to Menu"))
    return m


def manage_stock_kb(accounts: list, page: int = 0):
    """Inline keyboard listing available accounts with delete buttons, paginated 5 per page."""
    m = types.InlineKeyboardMarkup()
    per_page = 5
    start    = page * per_page
    chunk    = accounts[start:start + per_page]

    for acc in chunk:
        added = acc.get("added_at", "—")
        m.add(types.InlineKeyboardButton(
            f"📱 {acc['phone']}  |  {added}",
            callback_data=f"stock_view_{acc['id']}_{page}"
        ))

    # Pagination row
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("◀️ Prev", callback_data=f"stock_page_{page-1}"))
    if start + per_page < len(accounts):
        nav.append(types.InlineKeyboardButton("Next ▶️", callback_data=f"stock_page_{page+1}"))
    if nav:
        m.row(*nav)

    m.add(types.InlineKeyboardButton("❌ Close", callback_data="stock_close"))
    return m


def account_action_kb(account_id: int, page: int = 0):
    """Inline keyboard for a single account — show delete option."""
    m = types.InlineKeyboardMarkup()
    m.add(types.InlineKeyboardButton("🗑 Delete This Account", callback_data=f"stock_delete_{account_id}_{page}"))
    m.add(types.InlineKeyboardButton("◀️ Back to List",        callback_data=f"stock_page_{page}"))
    return m


def price_quick_kb():
    m = types.InlineKeyboardMarkup(row_width=3)
    m.add(
        types.InlineKeyboardButton("🔥 Sale: 0.05", callback_data="qprice_0.05"),
        types.InlineKeyboardButton("✅ Normal: 0.1", callback_data="qprice_0.1"),
        types.InlineKeyboardButton("💎 High: 0.5",  callback_data="qprice_0.5"),
    )
    m.add(types.InlineKeyboardButton("✏️ Enter Custom Price", callback_data="qprice_custom"))
    return m


def confirm_purchase_kb(account_id):
    m = types.InlineKeyboardMarkup()
    m.add(types.InlineKeyboardButton("✅ Confirm Purchase", callback_data=f"buy_{account_id}"))
    m.add(types.InlineKeyboardButton("❌ Cancel",           callback_data="cancel_buy"))
    return m


def cancel_otp_kb():
    m = types.InlineKeyboardMarkup()
    m.add(types.InlineKeyboardButton("❌ Cancel & Get Refund", callback_data="cancel_otp"))
    return m


def add_balance_amount_kb(price: float):
    """Quick amount buttons for add balance — multiples of account price."""
    m = types.InlineKeyboardMarkup(row_width=3)
    m.add(
        types.InlineKeyboardButton(f"x1 — {price:.2f} TON",    callback_data=f"topup_{price:.4f}"),
        types.InlineKeyboardButton(f"x3 — {price*3:.2f} TON",  callback_data=f"topup_{price*3:.4f}"),
        types.InlineKeyboardButton(f"x5 — {price*5:.2f} TON",  callback_data=f"topup_{price*5:.4f}"),
        types.InlineKeyboardButton(f"x10 — {price*10:.2f} TON", callback_data=f"topup_{price*10:.4f}"),
    )
    m.add(types.InlineKeyboardButton("✏️ Custom Amount", callback_data="topup_custom"))
    return m


def payment_method_kb(uid: int, amount_ton: float):
    """Show Tonkeeper deep link button + manual method info."""
    nano       = int(amount_ton * 1_000_000_000)
    tk_link    = f"https://app.tonkeeper.com/transfer/{BOT_WALLET}?amount={nano}&text={uid}"
    m = types.InlineKeyboardMarkup()
    m.add(types.InlineKeyboardButton("💎 Pay with Tonkeeper", url=tk_link))
    m.add(types.InlineKeyboardButton("🔄 Choose Different Amount", callback_data="topup_back"))
    return m


# ─────────────────────── /START ───────────────────────

@bot.message_handler(commands=["start"])
def cmd_start(message):
    uid   = message.from_user.id
    db.add_user(uid, message.from_user.username or "")
    name  = message.from_user.first_name or "User"
    price = db.get_price_ton()
    send(uid,
        f"[E:👋] **Welcome, {name}!**\n\n"
        f"[E:🏠] **Fragment Account Shop**\n"
        f"[E:💎] Buy verified Telegram Fragment accounts instantly.\n\n"
        f"[E:🪙] Price: **{price} TON** per account\n"
        f"[E:☑️] Instant delivery after payment\n"
        f"[E:🔒] Safe & automated",
        reply_markup=main_menu(uid)
    )


# ─────────────────────── ADD BALANCE ──────────────────

def add_balance_choose_kb():
    """Two buttons — manual or Tonkeeper."""
    m = types.InlineKeyboardMarkup()
    m.add(types.InlineKeyboardButton("💎 Pay with Tonkeeper", callback_data="topup_tonkeeper"))
    m.add(types.InlineKeyboardButton("📋 Manual Payment",     callback_data="topup_manual"))
    return m


def tonkeeper_payment_kb(uid: int, amount_ton: float):
    """Tonkeeper deep link button + back."""
    nano    = int(amount_ton * 1_000_000_000)
    tk_link = f"https://app.tonkeeper.com/transfer/{BOT_WALLET}?amount={nano}&text={uid}"
    m = types.InlineKeyboardMarkup()
    m.add(types.InlineKeyboardButton("💎 Open Tonkeeper & Pay", url=tk_link))
    m.add(types.InlineKeyboardButton("🔙 Back",                 callback_data="topup_back"))
    return m


@bot.message_handler(func=lambda m: m.text and "Add Balance" in m.text)
def add_balance(message):
    uid = message.from_user.id
    db.add_user(uid, message.from_user.username or "")
    bot.send_message(
        uid,
        f"<tg-emoji emoji-id=\"6106898347598027963\">🪙</tg-emoji> <b>Add Balance</b>\n\n"
        f"Choose your preferred payment method:",
        parse_mode="HTML",
        reply_markup=add_balance_choose_kb()
    )


# ─────────────── TOPUP CALLBACKS ─────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("topup_"))
def topup_cb(call):
    uid = call.from_user.id
    val = call.data.split("_", 1)[1]

    # ── Back to method selection ───────────────────────
    if val == "back":
        bot.edit_message_text(
            f"<tg-emoji emoji-id=\"6106898347598027963\">🪙</tg-emoji> <b>Add Balance</b>\n\n"
            f"Choose your preferred payment method:",
            call.message.chat.id, call.message.message_id,
            parse_mode="HTML", reply_markup=add_balance_choose_kb()
        )
        bot.answer_callback_query(call.id)
        return

    # ── Manual payment — show wallet + memo immediately ─
    if val == "manual":
        bot.edit_message_text(
            f"<tg-emoji emoji-id=\"6107289979895945232\">💎</tg-emoji> <b>Manual Payment</b>\n\n"
            f"Send any amount of TON to the address below.\n"
            f"<b>You must include your ID as memo</b> or it won\'t be credited.\n\n"
            f"<tg-emoji emoji-id=\"6107289979895945232\">💎</tg-emoji> <b>Wallet Address:</b>\n"
            f"<code>{BOT_WALLET}</code>\n\n"
            f"<tg-emoji emoji-id=\"6106902616795519273\">🔒</tg-emoji> <b>Memo / Comment:</b>\n"
            f"<code>{uid}</code>\n\n"
            f"<tg-emoji emoji-id=\"5900104897885376843\">⏱</tg-emoji> Credited automatically within ~1 minute.\n"
            f"<tg-emoji emoji-id=\"6106898459267177284\">⚠️</tg-emoji> Do not forget the memo!",
            call.message.chat.id, call.message.message_id,
            parse_mode="HTML",
            reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton("🔙 Back", callback_data="topup_back")
            )
        )
        bot.answer_callback_query(call.id)
        return

    # ── Tonkeeper — ask amount as text ─────────────────
    if val == "tonkeeper":
        set_state(uid, "topup_custom")
        bot.edit_message_text(
            f"<tg-emoji emoji-id=\"6106898347598027963\">🪙</tg-emoji> <b>Tonkeeper Payment</b>\n\n"
            f"How much TON do you want to deposit?\n"
            f"Type the amount and send it. Example: <code>1.5</code>",
            call.message.chat.id, call.message.message_id,
            parse_mode="HTML",
            reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton("🔙 Back", callback_data="topup_back")
            )
        )
        bot.answer_callback_query(call.id)
        return

    # ── Amount received — show Tonkeeper link ──────────
    try:
        amount = float(val)
        bot.edit_message_text(
            f"<tg-emoji emoji-id=\"6106898347598027963\">🪙</tg-emoji> <b>Tonkeeper Payment — {amount:.3f} TON</b>\n\n"
            f"Tap the button below. Your wallet address, amount and memo\n"
            f"are all pre-filled — just open and confirm.\n\n"
            f"<tg-emoji emoji-id=\"5951665890079544884\">☑️</tg-emoji> Amount: <b>{amount:.3f} TON</b>\n"
            f"<tg-emoji emoji-id=\"6106902616795519273\">🔒</tg-emoji> Memo: <code>{uid}</code>\n\n"
            f"<tg-emoji emoji-id=\"5900104897885376843\">⏱</tg-emoji> Credited automatically within ~1 minute.",
            call.message.chat.id, call.message.message_id,
            parse_mode="HTML",
            reply_markup=tonkeeper_payment_kb(uid, amount)
        )
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid amount", show_alert=True)
    bot.answer_callback_query(call.id)


# ─────────────────────── BUY ACCOUNT ──────────────────

@bot.message_handler(func=lambda m: m.text and "Buy Account" in m.text)
def buy_account(message):
    uid = message.from_user.id
    db.add_user(uid, message.from_user.username or "")

    if db.has_active_purchase(uid):
        send(uid,
            f"[E:⚠️] **You already have an active purchase in progress.**\n\n"
            f"Please wait for your OTP to arrive or cancel it first."
        )
        return

    balance = db.get_balance(uid)
    stock   = db.get_available_count()
    price   = db.get_price_ton()

    if stock == 0:
        send(uid, f"[E:⚠️] **Out of Stock**\n\nNo accounts available right now. Check back soon!")
        return

    if balance < price:
        needed = price - balance
        send(uid,
            f"[E:🏪] **Buy Fragment Account**\n\n"
            f"[E:🪙] Price: **{price} TON**\n"
            f"[E:💲] Your Balance: **{balance:.3f} TON**\n\n"
            f"[E:⚠️] Insufficient balance. You need **{needed:.3f} more TON**.\n"
            f"Use 💰 Add Balance to top up."
        )
        return

    send(uid,
        f"[E:🏪] **Buy Fragment Account**\n\n"
        f"[E:💎] Available Stock: **{stock}**\n"
        f"[E:🪙] Price: **{price} TON**\n"
        f"[E:💲] Your Balance: **{balance:.3f} TON**\n\n"
        f"[E:✅] Press confirm to proceed.\n"
        f"[E:🔒] **You will only be charged once you receive the login OTP.**\n"
        f"If cancelled before OTP arrives, no charge.",
        reply_markup=confirm_purchase_kb(0)
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("buy_"))
def confirm_buy(call):
    uid = call.from_user.id

    if db.has_active_purchase(uid):
        bot.answer_callback_query(call.id, "⚠️ You already have an active purchase!", show_alert=True)
        return

    balance = db.get_balance(uid)
    price   = db.get_price_ton()

    if balance < price:
        bot.answer_callback_query(call.id, "❌ Insufficient balance!", show_alert=True)
        return

    account = db.reserve_account(uid)
    if not account:
        bot.answer_callback_query(call.id, "❌ No accounts available right now!", show_alert=True)
        return

    phone          = account["phone"]
    session_string = account["session_string"]
    password_2fa   = account["password_2fa"]

    set_state(uid, "awaiting_otp")

    bot.edit_message_text(
        f"<tg-emoji emoji-id=\"6106981506754814207\">✅</tg-emoji> <b>Account Reserved!</b>\n\n"
        f"📱 <b>Phone Number:</b>\n<code>{phone}</code>\n\n"
        f"<tg-emoji emoji-id=\"6107323579425104140\">🤖</tg-emoji> Now open Telegram and try to log in with this number.\n"
        f"I'm listening — as soon as the OTP arrives I'll send it to you here.\n\n"
        f"<tg-emoji emoji-id=\"6106902616795519273\">🔒</tg-emoji> <b>Listener active for 5 minutes.</b>\n"
        f"<tg-emoji emoji-id=\"6106898347598027963\">🪙</tg-emoji> You will only be charged once the OTP is delivered.",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="HTML",
        reply_markup=cancel_otp_kb()
    )
    bot.answer_callback_query(call.id)

    asyncio.run_coroutine_threadsafe(
        session_manager.start_otp_listener(bot, uid, phone, session_string, password_2fa),
        telethon_loop
    )


@bot.callback_query_handler(func=lambda c: c.data == "cancel_buy")
def cancel_buy(call):
    bot.edit_message_text("❌ Purchase cancelled.", call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data == "cancel_otp")
def cancel_otp_cb(call):
    uid = call.from_user.id
    cancelled = session_manager.cancel_buyer_listener(uid)
    clear_state(uid)
    bot.answer_callback_query(call.id)
    if not cancelled:
        edit(call.message.chat.id, call.message.message_id,
            f"[E:⚠️] Could not cancel — OTP may have already been delivered. Check your messages."
        )
    else:
        edit(call.message.chat.id, call.message.message_id,
            f"❌ **Purchase Cancelled**\n\n[E:🔒] You have not been charged."
        )


# ─────────────────────── MY PROFILE ───────────────────

@bot.message_handler(func=lambda m: m.text and "My Profile" in m.text)
def my_profile(message):
    uid = message.from_user.id
    db.add_user(uid, message.from_user.username or "")
    balance   = db.get_balance(uid)
    purchases = db.get_user_purchase_count(uid)
    price     = db.get_price_ton()
    send(uid,
        f"[E:👤] **Your Profile**\n\n"
        f"[E:🤖] Telegram ID: {uid}\n"
        f"[E:🪙] Balance: **{balance:.3f} TON**\n"
        f"[E:📈] Total Purchases: **{purchases}**\n"
        f"[E:💲] Account Price: **{price} TON**"
    )


# ─────────────────────── MY PURCHASES ─────────────────

@bot.message_handler(func=lambda m: m.text and "My Purchases" in m.text)
def my_purchases(message):
    uid       = message.from_user.id
    purchases = db.get_user_purchases(uid)
    if not purchases:
        send(uid, f"[E:🏪] You haven't purchased any accounts yet.")
        return
    lines = f"[E:📈] **Your Purchases**\n\n"
    for i, p in enumerate(purchases[-10:], 1):
        lines += f"{i}. [E:✅] {p['phone']} — {p['purchased_at']}\n"
    lines += "\nShowing last 10 purchases"
    send(uid, lines)


# ─────────────────────── ADMIN PANEL ──────────────────

@bot.message_handler(func=lambda m: m.text and "Admin Panel" in m.text)
def admin_panel(message):
    if message.from_user.id != ADMIN_ID:
        return
    send(message.chat.id, f"[E:🤖] **Admin Panel**", reply_markup=admin_menu())


@bot.message_handler(func=lambda m: m.text and "Back to Menu" in m.text)
def back_to_menu(message):
    uid   = message.from_user.id
    state = get_state(uid)
    if state in ("enter_otp", "enter_2fa", "enter_phone"):
        run_async(session_manager.cancel_pending(uid))
    clear_state(uid)
    bot.send_message(message.chat.id, "🏠 Main Menu", reply_markup=main_menu(uid))


@bot.message_handler(func=lambda m: m.text and "Stock Info" in m.text)
def stock_info(message):
    if message.from_user.id != ADMIN_ID:
        return
    send(message.chat.id,
        f"[E:📈] **Stock Info**\n\n"
        f"[E:✅] Available: **{db.get_available_count()}**\n"
        f"🔴 Sold: **{db.get_sold_count()}**\n"
        f"[E:🪙] Total Revenue: **{db.get_total_revenue():.3f} TON**\n"
        f"[E:💲] Current Price: **{db.get_price_ton()} TON**"
    )


@bot.message_handler(func=lambda m: m.text and "All Users" in m.text)
def all_users(message):
    if message.from_user.id != ADMIN_ID:
        return
    users = db.get_all_users()
    if not users:
        bot.send_message(message.chat.id, "No users yet.")
        return
    lines = f"[E:👤] **All Users ({len(users)})**\n\n"
    for u in users[:30]:
        uname = f"@{u['username']}" if u['username'] else "no username"
        lines += f"• {u['telegram_id']} {uname} — {u['balance_ton']:.3f} TON — {u['purchases']} purchase(s)\n"
    if len(users) > 30:
        lines += f"\n...and {len(users)-30} more"
    send(message.chat.id, lines)


# ─────────────── ADD ACCOUNT FLOW ─────────────────────

@bot.message_handler(func=lambda m: m.text and "Add Account" in m.text)
def add_account_start(message):
    if message.from_user.id != ADMIN_ID:
        return
    set_state(message.from_user.id, "enter_phone")
    send(message.chat.id,
        f"[E:💎] **Add Account — Step 1**\n\n"
        f"Send the **phone number** to add.\n"
        f"Include country code. Example: +14155552671\n\n"
        f"Send /cancel to abort."
    )


# ─────────────── CHANGE PRICE ─────────────────────────

@bot.message_handler(func=lambda m: m.text and "Change Price" in m.text)
def change_price_menu(message):
    if message.from_user.id != ADMIN_ID:
        return
    price = db.get_price_ton()
    send(message.chat.id,
        f"[E:💲] **Change Account Price**\n\n"
        f"[E:🪙] Current price: **{price} TON**\n\n"
        f"Pick a quick preset or enter a custom price:",
        reply_markup=price_quick_kb()
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("qprice_"))
def quick_price_cb(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id)
        return
    val = call.data.split("_", 1)[1]
    if val == "custom":
        set_state(call.from_user.id, "set_price")
        edit(call.message.chat.id, call.message.message_id,
            f"[E:💲] Send the new price in TON (e.g. 0.25):"
        )
    else:
        try:
            new_price = float(val)
            db.set_price_ton(new_price)
            edit(call.message.chat.id, call.message.message_id,
                f"[E:✅] Price updated to **{new_price} TON**"
            )
        except ValueError:
            bot.answer_callback_query(call.id, "Invalid price", show_alert=True)
    bot.answer_callback_query(call.id)


# ─────────────── BROADCAST ────────────────────────────

@bot.message_handler(func=lambda m: m.text and "Broadcast" in m.text)
def broadcast_start(message):
    if message.from_user.id != ADMIN_ID:
        return
    set_state(message.from_user.id, "broadcast")
    send(message.chat.id,
        f"[E:🤖] **Broadcast Message**\n\n"
        f"Send the message you want to broadcast to **all users**.\n"
        f"Plain text only in broadcast — no special formatting needed.\n\n"
        f"Send /cancel to abort."
    )


# ─────────────── ADD USER BALANCE ─────────────────────

@bot.message_handler(func=lambda m: m.text and "Add User Balance" in m.text)
def add_user_balance_start(message):
    if message.from_user.id != ADMIN_ID:
        return
    set_state(message.from_user.id, "add_bal_uid")
    send(message.chat.id,
        f"[E:🎁] **Add User Balance — Step 1**\n\n"
        f"Send the **Telegram ID** of the user you want to credit.\n"
        f"Example: 987654321\n\n"
        f"Send /cancel to abort."
    )


# ─────────────── CANCEL ───────────────────────────────

@bot.message_handler(commands=["cancel"])
def cancel_cmd(message):
    uid   = message.from_user.id
    state = get_state(uid)
    if state in ("enter_otp", "enter_2fa", "enter_phone"):
        run_async(session_manager.cancel_pending(uid))
    clear_state(uid)
    bot.send_message(message.chat.id, "❌ Cancelled.", reply_markup=main_menu(uid))


# ─────────────── STATE MACHINE ────────────────────────


# ─────────────────────── MANAGE STOCK (ADMIN) ─────────

def stock_list_text(accounts):
    return f"📦 <b>Manage Stock</b>\n\n<b>{len(accounts)}</b> account(s) in stock.\nTap an account to view details or delete it."


@bot.message_handler(func=lambda m: m.text and "Manage Stock" in m.text)
def manage_stock(message):
    if message.from_user.id != ADMIN_ID:
        return
    accounts = db.get_available_accounts()
    if not accounts:
        bot.send_message(message.chat.id, "⚠️ No accounts in stock to manage.")
        return
    bot.send_message(
        message.chat.id,
        stock_list_text(accounts),
        parse_mode="HTML",
        reply_markup=manage_stock_kb(accounts, page=0)
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("stock_page_"))
def stock_page_cb(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id)
        return
    page     = int(call.data.split("_")[2])
    accounts = db.get_available_accounts()
    if not accounts:
        bot.edit_message_text("📦 No accounts in stock.", call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id)
        return
    bot.edit_message_text(
        stock_list_text(accounts),
        call.message.chat.id, call.message.message_id,
        parse_mode="HTML",
        reply_markup=manage_stock_kb(accounts, page=page)
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("stock_view_"))
def stock_view_cb(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id)
        return
    parts      = call.data.split("_")
    account_id = int(parts[2])
    page       = int(parts[3]) if len(parts) > 3 else 0
    account    = db.get_account_by_phone_id(account_id)
    if not account:
        bot.answer_callback_query(call.id, "Account not found!", show_alert=True)
        return
    bot.edit_message_text(
        f"📱 <b>Account Details</b>\n\n"
        f"📞 Phone: <code>{account['phone']}</code>\n"
        f"🔒 2FA: <code>{account['password_2fa'] or 'None'}</code>\n"
        f"📅 Added: {account['added_at']}\n\n"
        f"Tap delete to remove from stock:",
        call.message.chat.id, call.message.message_id,
        parse_mode="HTML",
        reply_markup=account_action_kb(account_id, page)
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("stock_delete_"))
def stock_delete_cb(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id)
        return
    parts      = call.data.split("_")
    account_id = int(parts[2])
    page       = int(parts[3]) if len(parts) > 3 else 0
    deleted    = db.delete_account(account_id)
    bot.answer_callback_query(call.id, "✅ Deleted!" if deleted else "❌ Could not delete.", show_alert=not deleted)
    accounts = db.get_available_accounts()
    if not accounts:
        bot.edit_message_text("📦 No more accounts in stock.", call.message.chat.id, call.message.message_id)
        return
    if page > 0 and page * 5 >= len(accounts):
        page = max(0, page - 1)
    bot.edit_message_text(
        stock_list_text(accounts),
        call.message.chat.id, call.message.message_id,
        parse_mode="HTML",
        reply_markup=manage_stock_kb(accounts, page=page)
    )


@bot.callback_query_handler(func=lambda c: c.data == "stock_close")
def stock_close_cb(call):
    bot.delete_message(call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id)



@bot.message_handler(func=lambda m: True)
def handle_text(message):
    uid   = message.from_user.id
    state = get_state(uid)
    text  = message.text.strip() if message.text else ""

    if state == "enter_phone" and uid == ADMIN_ID:
        send(message.chat.id, f"[E:💎] Sending OTP to {text}...")
        ok, msg = run_async(session_manager.send_otp(uid, text))
        set_state(uid, "enter_otp") if ok else clear_state(uid)
        bot.send_message(message.chat.id, msg)

    elif state == "enter_otp" and uid == ADMIN_ID:
        code = text.replace(" ", "")
        needs_2fa, ok, msg = run_async(session_manager.verify_otp(uid, code))
        if not ok:
            bot.send_message(message.chat.id, msg)
        elif needs_2fa:
            set_state(uid, "enter_2fa")
            bot.send_message(message.chat.id, msg)
        else:
            clear_state(uid)
            bot.send_message(message.chat.id, msg, reply_markup=admin_menu())

    elif state == "enter_2fa" and uid == ADMIN_ID:
        ok, msg = run_async(session_manager.verify_2fa(uid, text))
        if ok:
            clear_state(uid)
            bot.send_message(message.chat.id, msg, reply_markup=admin_menu())
        else:
            bot.send_message(message.chat.id, msg)

    elif state == "set_price" and uid == ADMIN_ID:
        try:
            new_price = float(text)
            db.set_price_ton(new_price)
            clear_state(uid)
            send(message.chat.id,
                f"[E:✅] Price updated to **{new_price} TON**",
                reply_markup=admin_menu()
            )
        except ValueError:
            send(message.chat.id, f"[E:⚠️] Invalid. Send a number like 0.25")

    elif state == "broadcast" and uid == ADMIN_ID:
        clear_state(uid)
        all_users_list = db.get_all_users()
        sent = 0
        failed = 0
        bot.send_message(message.chat.id, f"📤 Sending to {len(all_users_list)} users...")
        # Build once with premium emojis — reuse plain+entities for all users
        plain, entities = build(f"[E:📢] **Message from Shop**\n\n{text}")
        for user in all_users_list:
            try:
                bot.send_message(
                    user["telegram_id"],
                    plain,
                    entities=entities if entities else None
                )
                sent += 1
            except Exception:
                failed += 1
        send(message.chat.id,
            f"[E:✅] **Broadcast Complete**\n\n"
            f"[E:📈] Sent: **{sent}**\n"
            f"❌ Failed: **{failed}** (blocked/deleted)",
            reply_markup=admin_menu()
        )

    elif state == "add_bal_uid" and uid == ADMIN_ID:
        if not text.isdigit():
            send(message.chat.id, f"[E:⚠️] Not a valid Telegram ID. Numbers only.")
            return
        target_uid = int(text)
        user = db.get_user_by_id(target_uid)
        if not user:
            send(message.chat.id, f"[E:⚠️] User {target_uid} not found in database.")
            return
        uname = f"@{user['username']}" if user['username'] else "no username"
        set_state(uid, "add_bal_amount", data=target_uid)
        send(message.chat.id,
            f"[E:🎁] **Add Balance — Step 2**\n\n"
            f"[E:👤] User: {target_uid} {uname}\n"
            f"[E:🪙] Current Balance: **{user['balance_ton']:.3f} TON**\n\n"
            f"How much TON to add? (e.g. 1.5)"
        )

    elif state == "add_bal_amount" and uid == ADMIN_ID:
        target_uid = get_state_data(uid)
        try:
            amount = float(text)
            if amount <= 0:
                raise ValueError
            db.add_balance(target_uid, amount)
            new_bal = db.get_balance(target_uid)
            clear_state(uid)
            send(message.chat.id,
                f"[E:✅] **Balance Added!**\n\n"
                f"[E:👤] User: {target_uid}\n"
                f"[E:🪙] Added: +{amount} TON\n"
                f"[E:📈] New Balance: **{new_bal:.3f} TON**",
                reply_markup=admin_menu()
            )
            try:
                send(target_uid,
                    f"[E:🎁] **Balance Added by Admin!**\n\n"
                    f"[E:🪙] Added: **+{amount} TON**\n"
                    f"[E:💲] New Balance: **{new_bal:.3f} TON**"
                )
            except Exception:
                pass
        except ValueError:
            send(message.chat.id, f"[E:⚠️] Invalid amount. Send a positive number like 1.5")

    # ── Buyer: tonkeeper amount input ─────────────────────
    elif state == "topup_custom":
        try:
            amount = float(text)
            if amount <= 0:
                raise ValueError
            clear_state(uid)
            nano    = int(amount * 1_000_000_000)
            tk_link = f"https://app.tonkeeper.com/transfer/{BOT_WALLET}?amount={nano}&text={uid}"
            tk_kb   = types.InlineKeyboardMarkup()
            tk_kb.add(types.InlineKeyboardButton("💎 Open Tonkeeper & Pay", url=tk_link))
            tk_kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="topup_back"))
            bot.send_message(
                uid,
                f"<tg-emoji emoji-id=\"6106898347598027963\">🪙</tg-emoji> <b>Tonkeeper Payment — {amount:.3f} TON</b>\n\n"
                f"Tap the button below. Your wallet address, amount and memo\n"
                f"are all pre-filled — just open and confirm.\n\n"
                f"<tg-emoji emoji-id=\"5951665890079544884\">☑️</tg-emoji> Amount: <b>{amount:.3f} TON</b>\n"
                f"<tg-emoji emoji-id=\"6106902616795519273\">🔒</tg-emoji> Memo: <code>{uid}</code>\n\n"
                f"<tg-emoji emoji-id=\"5900104897885376843\">⏱</tg-emoji> Credited automatically within ~1 minute.",
                parse_mode="HTML",
                reply_markup=tk_kb
            )
        except ValueError:
            bot.send_message(
                uid,
                f"<tg-emoji emoji-id=\"6106898459267177284\">⚠️</tg-emoji> Invalid amount. Send a number like <code>2.5</code>",
                parse_mode="HTML"
            )
        return

    # ── Buyer: writing review text ─────────────────────
    elif state == "writing_review":
        rating   = get_state_data(uid) or 5
        stars    = "⭐" * rating
        username = message.from_user.username or ""

        if db.has_reviewed(uid):
            clear_state(uid)
            bot.send_message(uid, "✅ You already submitted a review. Thank you!")
            return

        saved = db.save_review(uid, username, rating, text)
        if not saved:
            bot.send_message(uid, "❌ Could not save review. Please try again.")
            return

        # Reward buyer with 0.5 TON
        db.add_balance(uid, REVIEW_REWARD)
        db.mark_review_rewarded(uid)
        new_bal = db.get_balance(uid)
        clear_state(uid)

        # Thank buyer
        bot.send_message(
            uid,
            f"<tg-emoji emoji-id=\"6106981506754814207\">✅</tg-emoji> <b>Review Submitted! Thank you!</b>\n\n"
            f"<tg-emoji emoji-id=\"6106898347598027963\">🪙</tg-emoji> <b>+{REVIEW_REWARD} TON</b> has been added to your balance.\n"
            f"<tg-emoji emoji-id=\"6107061783988542265\">💲</tg-emoji> New Balance: <b>{new_bal:.3f} TON</b>",
            parse_mode="HTML",
            reply_markup=main_menu(uid)
        )

        # Forward review to admin
        uname_display = f"@{username}" if username else f"ID: {uid}"
        try:
            bot.send_message(
                ADMIN_ID,
                f"<tg-emoji emoji-id=\"6104943961384688402\">📈</tg-emoji> <b>New Review Received!</b>\n\n"
                f"<tg-emoji emoji-id=\"6107017202228009498\">👤</tg-emoji> User: {uname_display} (<code>{uid}</code>)\n"
                f"⭐ Rating: <b>{stars} ({rating}/5)</b>\n\n"
                f"💬 <b>Review:</b>\n{text}",
                parse_mode="HTML"
            )
        except Exception:
            pass




# ─────────────────────── REVIEW FLOW ──────────────────
# Triggered after purchase. Buyer taps star → types review → gets 0.5 TON reward.

REVIEW_REWARD = 0.5  # TON rewarded for leaving a review

@bot.callback_query_handler(func=lambda c: c.data.startswith("review_"))
def handle_rating(call):
    uid = call.from_user.id

    # Already reviewed
    if db.has_reviewed(uid):
        bot.answer_callback_query(call.id, "✅ You already left a review. Thank you!", show_alert=True)
        return

    rating = int(call.data.split("_")[1])
    stars  = "⭐" * rating

    set_state(uid, "writing_review", data=rating)
    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        '<tg-emoji emoji-id="6107325885822540958">🎁</tg-emoji> <b>You rated us ' + stars + '</b>\n\n'
        'Now write a short review in a few words and hit send.\n'
        '<tg-emoji emoji-id="6106898347598027963">🪙</tg-emoji> <b>0.5 TON</b> will be added to your balance right after!',
        call.message.chat.id,
        call.message.message_id,
        parse_mode="HTML"
    )


# ─────────────────────── STARTUP ──────────────────────

def run_telethon_loop():
    asyncio.set_event_loop(telethon_loop)
    telethon_loop.run_forever()


if __name__ == "__main__":
    db.init_db()
    loop_thread = threading.Thread(target=run_telethon_loop, daemon=True)
    loop_thread.start()
    asyncio.run_coroutine_threadsafe(ton_monitor.start_monitoring(bot), telethon_loop)
    logger.info("✅ Bot started!")
    bot.infinity_polling()
