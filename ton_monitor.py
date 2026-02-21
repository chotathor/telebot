"""
TON Payment Monitor
-------------------
Polls TonCenter API every 30 seconds.
Runs in async loop inside a background thread (no aiohttp/aiogram needed).
Uses httpx for HTTP requests.
"""

import asyncio
import logging
import httpx
import database as db
from config import BOT_WALLET, TON_API_KEY

logger = logging.getLogger(__name__)

TONCENTER_API = "https://toncenter.com/api/v2"
POLL_INTERVAL = 30


async def get_transactions(client: httpx.AsyncClient) -> list:
    url = f"{TONCENTER_API}/getTransactions"
    params = {
        "address": BOT_WALLET,
        "limit": 50,
        "api_key": TON_API_KEY,
    }
    try:
        resp = await client.get(url, params=params, timeout=15)
        data = resp.json()
        if data.get("ok"):
            return data.get("result", [])
        else:
            logger.warning(f"TON API error: {data}")
            return []
    except Exception as e:
        logger.error(f"Error fetching transactions: {e}")
        return []


def extract_memo(tx: dict):
    try:
        msg = tx.get("in_msg", {})
        comment = msg.get("message", "") or msg.get("comment", "")
        return comment.strip() if comment else None
    except Exception:
        return None


def get_amount_ton(tx: dict) -> float:
    try:
        value = tx.get("in_msg", {}).get("value", 0)
        return int(value) / 1_000_000_000
    except Exception:
        return 0.0


async def process_transaction(bot, tx: dict):
    tx_hash = tx.get("transaction_id", {}).get("hash", "")
    if not tx_hash:
        return

    memo = extract_memo(tx)
    if not memo or not memo.isdigit():
        return

    telegram_id = int(memo)
    amount_ton = get_amount_ton(tx)

    if amount_ton <= 0:
        return

    # Returns False if already processed (duplicate tx_hash)
    is_new = db.record_transaction(telegram_id, amount_ton, tx_hash)
    if not is_new:
        return

    db.add_balance(telegram_id, amount_ton)
    new_balance = db.get_balance(telegram_id)
    price = db.get_price_ton()
    accounts_can_buy = int(new_balance // price)

    logger.info(f"Credited {amount_ton:.3f} TON to {telegram_id}. Balance: {new_balance:.3f}")

    try:
        bot.send_message(
            telegram_id,
            f"✅ <b>Balance Added!</b>\n\n"
            f"💰 Received: <b>{amount_ton:.3f} TON</b>\n"
            f"💳 New Balance: <b>{new_balance:.3f} TON</b>\n"
            f"🛒 You can buy: <b>{accounts_can_buy} account(s)</b>\n\n"
            f"Use 🛒 <b>Buy Account</b> to purchase!",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"Could not notify user {telegram_id}: {e}")


async def start_monitoring(bot):
    logger.info(f"TON monitor started for: {BOT_WALLET}")
    processed_hashes = set()

    async with httpx.AsyncClient() as client:
        # Collect existing hashes on startup to skip old transactions
        existing = await get_transactions(client)
        for tx in existing:
            h = tx.get("transaction_id", {}).get("hash", "")
            if h:
                processed_hashes.add(h)
        logger.info(f"Skipped {len(processed_hashes)} old transactions. Watching for new ones...")

        while True:
            try:
                txs = await get_transactions(client)
                for tx in txs:
                    tx_hash = tx.get("transaction_id", {}).get("hash", "")
                    if tx_hash and tx_hash not in processed_hashes:
                        processed_hashes.add(tx_hash)
                        await process_transaction(bot, tx)

                if len(processed_hashes) > 1000:
                    processed_hashes = set(list(processed_hashes)[-500:])

            except Exception as e:
                logger.error(f"Monitor loop error: {e}")

            await asyncio.sleep(POLL_INTERVAL)
