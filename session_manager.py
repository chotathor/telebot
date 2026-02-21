"""
Session Manager
---------------
Handles all Telethon userbot operations:
- Admin flow: send OTP to phone, verify code, save session
- Buyer flow: listen for new login OTP on purchased account, forward to buyer,
              then finalize payment ONLY after OTP is delivered.
              If buyer cancels before OTP arrives → account released, no charge.
"""

import asyncio
import logging
import re
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PasswordHashInvalidError,
    FloodWaitError,
)
import database as db
from config import API_ID, API_HASH

logger = logging.getLogger(__name__)

# ── Review rating inline keyboard (used after purchase) ──
def _review_rating_kb():
    from telebot import types
    m = types.InlineKeyboardMarkup(row_width=5)
    m.add(
        types.InlineKeyboardButton("⭐ 1", callback_data="review_1"),
        types.InlineKeyboardButton("⭐ 2", callback_data="review_2"),
        types.InlineKeyboardButton("⭐ 3", callback_data="review_3"),
        types.InlineKeyboardButton("⭐ 4", callback_data="review_4"),
        types.InlineKeyboardButton("⭐ 5", callback_data="review_5"),
    )
    return m

# Active OTP listeners per buyer — used to cancel mid-wait
# { buyer_id: asyncio.Event (set this to cancel) }
buyer_cancel_events = {}

# Temp storage during admin account-adding flow
# { admin_id: { "phone": ..., "client": ..., "phone_code_hash": ... } }
pending_logins = {}


# ─────────────────── ADMIN: ADD ACCOUNT ───────────────

async def send_otp(admin_id: int, phone: str) -> tuple:
    """Send OTP to phone number. Returns (success, message)."""
    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        result = await client.send_code_request(phone)
        pending_logins[admin_id] = {
            "phone": phone,
            "client": client,
            "phone_code_hash": result.phone_code_hash,
        }
        return True, f"✅ OTP sent to <code>{phone}</code>\n\nNow send me the OTP code you received on that number."
    except FloodWaitError as e:
        return False, f"❌ Flood wait. Try again in {e.seconds} seconds."
    except Exception as e:
        return False, f"❌ Error sending OTP: {e}"


async def verify_otp(admin_id: int, code: str) -> tuple:
    """Verify OTP. Returns (needs_2fa, success, message)."""
    data = pending_logins.get(admin_id)
    if not data:
        return False, False, "❌ No pending login. Start again with the phone number."

    client = data["client"]
    phone = data["phone"]
    phone_code_hash = data["phone_code_hash"]

    try:
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        session_string = client.session.save()
        await client.disconnect()
        db.save_account(phone, "", session_string)
        pending_logins.pop(admin_id, None)
        return False, True, f"✅ Account <code>{phone}</code> saved!\n\n📱 No 2FA — account is ready to sell."
    except SessionPasswordNeededError:
        return True, True, f"🔐 2FA enabled on <code>{phone}</code>\nPlease send the 2FA password."
    except PhoneCodeInvalidError:
        return False, False, "❌ Invalid OTP code. Please try again."
    except Exception as e:
        return False, False, f"❌ Error: {e}"


async def verify_2fa(admin_id: int, password: str) -> tuple:
    """Submit 2FA password. Returns (success, message)."""
    data = pending_logins.get(admin_id)
    if not data:
        return False, "❌ Session expired. Start again with the phone number."

    client = data["client"]
    phone = data["phone"]

    try:
        await client.sign_in(password=password)
        session_string = client.session.save()
        await client.disconnect()
        db.save_account(phone, password, session_string)
        pending_logins.pop(admin_id, None)
        return True, f"✅ Account <code>{phone}</code> saved with 2FA!\n\nAccount is ready to sell."
    except PasswordHashInvalidError:
        return False, "❌ Wrong 2FA password. Try again."
    except Exception as e:
        return False, f"❌ Error: {e}"


async def cancel_pending(admin_id: int):
    """Cancel any pending admin login."""
    data = pending_logins.pop(admin_id, None)
    if data:
        try:
            await data["client"].disconnect()
        except Exception:
            pass


# ─────────────────── BUYER: OTP LISTENER ──────────────

async def start_otp_listener(bot, buyer_id: int, phone: str, session_string: str, password_2fa: str):
    """
    Spin up a Telethon client on the purchased account's session.
    Waits for the login OTP to arrive (from Telegram service account 777000).
    
    Payment is ONLY deducted after OTP is successfully delivered.
    If buyer cancels or times out → account released, zero charge.
    """
    # Register a cancel event for this buyer
    cancel_event = asyncio.Event()
    buyer_cancel_events[buyer_id] = cancel_event

    try:
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await client.connect()

        otp_delivered = asyncio.Event()

        @client.on(events.NewMessage(from_users=777000))
        async def otp_handler(event):
            text = event.message.message or ""
            match = re.search(r'(\d{5,6})', text)
            if not match:
                return
            code = match.group(1)
            try:
                # Build one clean message with phone, OTP and password all copyable
                msg = (
                    f"<tg-emoji emoji-id=\"6106981506754814207\">✅</tg-emoji> <b>Login Details Ready!</b>\n\n"
                    f"📱 <b>Phone Number:</b>\n<code>{phone}</code>\n\n"
                    f"🔑 <b>OTP Code:</b>\n<code>{code}</code>\n\n"
                )
                if password_2fa:
                    msg += f"<tg-emoji emoji-id=\"6106902616795519273\">🔒</tg-emoji> <b>2FA Password:</b>\n<code>{password_2fa}</code>\n\n"
                msg += (
                    f"<tg-emoji emoji-id=\"6107323579425104140\">🤖</tg-emoji> <i>Enter the phone number, then OTP, then 2FA password to complete login.</i>"
                )
                bot.send_message(buyer_id, msg, parse_mode="HTML")

                # ✅ OTP delivered — NOW deduct payment and finalize
                finalized = db.finalize_purchase(buyer_id)
                if finalized:
                    new_balance = db.get_balance(buyer_id)
                    bot.send_message(
                        buyer_id,
                        f"<tg-emoji emoji-id=\"6106898347598027963\">🪙</tg-emoji> <b>Payment Deducted</b>\n\n"
                        f"Remaining Balance: <b>{new_balance:.3f} TON</b>",
                        parse_mode="HTML"
                    )
                otp_delivered.set()

                # Ask buyer to leave a review after successful purchase
                try:
                    bot.send_message(
                        buyer_id,
                        f"<tg-emoji emoji-id=\"6107325885822540958\">🎁</tg-emoji> <b>Leave a Review & Get Rewarded!</b>\n\n"
                        f"Enjoyed your purchase? Drop a quick review and receive\n"
                        f"<tg-emoji emoji-id=\"6106898347598027963\">🪙</tg-emoji> <b>0.5 TON free balance</b> as a thank you!\n\n"
                        f"<tg-emoji emoji-id=\"6107212468621154692\">🏪</tg-emoji> Simply tap a star rating below to get started.",
                        parse_mode="HTML",
                        reply_markup=_review_rating_kb()
                    )
                except Exception as re:
                    logger.warning(f"Could not send review prompt to {buyer_id}: {re}")

            except Exception as e:
                logger.warning(f"Could not deliver OTP to buyer {buyer_id}: {e}")

        # Wait for OTP, cancellation, or 5-minute timeout
        otp_task = asyncio.ensure_future(otp_delivered.wait())
        cancel_task = asyncio.ensure_future(cancel_event.wait())

        done, pending = await asyncio.wait(
            [otp_task, cancel_task],
            timeout=300,
            return_when=asyncio.FIRST_COMPLETED
        )

        # Cancel remaining tasks cleanly
        for t in pending:
            t.cancel()

        if cancel_event.is_set():
            # Buyer cancelled — release account, no charge
            db.cancel_purchase(buyer_id)
            try:
                bot.send_message(
                    buyer_id,
                    "❌ <b>Purchase Cancelled</b>\n\n"
                    "Your account has been released and you have <b>not been charged</b>.\n"
                    "Your balance remains intact.",
                    parse_mode="HTML"
                )
            except Exception:
                pass

        elif not otp_delivered.is_set():
            # Timeout — OTP never arrived, release account, no charge
            db.cancel_purchase(buyer_id)
            try:
                bot.send_message(
                    buyer_id,
                    "⏰ <b>OTP Listener Timed Out</b>\n\n"
                    "No login code was detected within 5 minutes.\n"
                    "You have <b>not been charged</b>. Your balance is intact.\n\n"
                    "Please try purchasing again or contact support.",
                    parse_mode="HTML"
                )
            except Exception:
                pass

        await client.disconnect()

    except Exception as e:
        logger.error(f"OTP listener error for {phone}: {e}")
        # On any error — release account and don't charge
        db.cancel_purchase(buyer_id)
        try:
            bot.send_message(
                buyer_id,
                f"❌ <b>Error during OTP listening.</b>\nYou have not been charged.\n\n<code>{e}</code>",
                parse_mode="HTML"
            )
        except Exception:
            pass
    finally:
        buyer_cancel_events.pop(buyer_id, None)


def cancel_buyer_listener(buyer_id: int):
    """Signal the OTP listener for a buyer to cancel (called from bot.py)."""
    event = buyer_cancel_events.get(buyer_id)
    if event:
        event.set()
        return True
    return False
