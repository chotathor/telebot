import sqlite3
from datetime import datetime

DB_PATH = "bot_data.db"


def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            telegram_id INTEGER UNIQUE,
            username TEXT,
            balance_ton REAL DEFAULT 0.0,
            joined_at TEXT
        );
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT UNIQUE,
            password_2fa TEXT,
            session_string TEXT,
            status TEXT DEFAULT 'available',
            added_at TEXT,
            sold_at TEXT,
            buyer_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount_ton REAL,
            tx_hash TEXT UNIQUE,
            type TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS active_purchases (
            buyer_id INTEGER PRIMARY KEY,
            account_id INTEGER,
            phone TEXT,
            reserved_at TEXT
        );
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            username TEXT,
            rating INTEGER,
            review_text TEXT,
            rewarded INTEGER DEFAULT 0,
            created_at TEXT
        );
    """)
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('price_ton', '0.1')")
    con.commit()
    con.close()


def _con():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


# ─── USER FUNCTIONS ───────────────────────────────────

def add_user(telegram_id: int, username: str):
    con = _con()
    con.execute(
        "INSERT OR IGNORE INTO users (telegram_id, username, balance_ton, joined_at) VALUES (?, ?, 0.0, ?)",
        (telegram_id, username, datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    con.commit()
    con.close()


def get_balance(telegram_id: int) -> float:
    con = _con()
    row = con.execute("SELECT balance_ton FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    con.close()
    return row["balance_ton"] if row else 0.0


def add_balance(telegram_id: int, amount: float):
    con = _con()
    con.execute("UPDATE users SET balance_ton = balance_ton + ? WHERE telegram_id = ?", (amount, telegram_id))
    con.commit()
    con.close()


def get_user_purchase_count(telegram_id: int) -> int:
    con = _con()
    row = con.execute("SELECT COUNT(*) as cnt FROM accounts WHERE buyer_id = ?", (telegram_id,)).fetchone()
    con.close()
    return row["cnt"] if row else 0


def get_user_purchases(telegram_id: int) -> list:
    con = _con()
    rows = con.execute(
        "SELECT phone, sold_at as purchased_at FROM accounts WHERE buyer_id = ? ORDER BY sold_at DESC",
        (telegram_id,)
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_user_count() -> int:
    con = _con()
    row = con.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
    con.close()
    return row["cnt"] if row else 0


def get_all_users() -> list:
    con = _con()
    rows = con.execute("""
        SELECT u.telegram_id, u.username, u.balance_ton,
               COUNT(a.id) as purchases
        FROM users u
        LEFT JOIN accounts a ON a.buyer_id = u.telegram_id
        GROUP BY u.telegram_id
        ORDER BY u.joined_at DESC
    """).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_user_by_id(telegram_id: int):
    con = _con()
    row = con.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    con.close()
    return dict(row) if row else None


# ─── ACTIVE PURCHASE LOCK ─────────────────────────────
# Prevents multiple simultaneous purchases per buyer.
# Account is reserved (status = 'reserved') until OTP is delivered or cancelled.

def has_active_purchase(buyer_id: int) -> bool:
    """Check if buyer already has an ongoing purchase."""
    con = _con()
    row = con.execute("SELECT 1 FROM active_purchases WHERE buyer_id = ?", (buyer_id,)).fetchone()
    con.close()
    return row is not None


def reserve_account(buyer_id: int) -> dict | None:
    """
    Reserve the next available account for buyer.
    Marks it as 'reserved' so nobody else can grab it.
    Does NOT deduct balance yet.
    Returns account dict or None if nothing available.
    """
    con = _con()
    account = con.execute(
        "SELECT * FROM accounts WHERE status = 'available' ORDER BY id ASC LIMIT 1"
    ).fetchone()

    if not account:
        con.close()
        return None

    account = dict(account)

    # Mark account as reserved
    con.execute("UPDATE accounts SET status = 'reserved' WHERE id = ?", (account["id"],))
    # Track the active purchase
    con.execute(
        "INSERT OR REPLACE INTO active_purchases (buyer_id, account_id, phone, reserved_at) VALUES (?, ?, ?, ?)",
        (buyer_id, account["id"], account["phone"], datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    con.commit()
    con.close()
    return account


def finalize_purchase(buyer_id: int) -> bool:
    """
    Called AFTER OTP is successfully delivered.
    Deducts balance and marks account as sold.
    Returns True on success.
    """
    con = _con()
    row = con.execute("SELECT * FROM active_purchases WHERE buyer_id = ?", (buyer_id,)).fetchone()
    if not row:
        con.close()
        return False

    account_id = row["account_id"]
    price = get_price_ton()

    con.execute("UPDATE users SET balance_ton = balance_ton - ? WHERE telegram_id = ?", (price, buyer_id))
    con.execute(
        "UPDATE accounts SET status = 'sold', buyer_id = ?, sold_at = ? WHERE id = ?",
        (buyer_id, datetime.now().strftime("%Y-%m-%d %H:%M"), account_id)
    )
    con.execute(
        "INSERT INTO transactions (user_id, amount_ton, tx_hash, type, created_at) VALUES (?, ?, ?, 'purchase', ?)",
        (buyer_id, price, f"purchase_{account_id}_{buyer_id}", datetime.now().isoformat())
    )
    con.execute("DELETE FROM active_purchases WHERE buyer_id = ?", (buyer_id,))
    con.commit()
    con.close()
    return True


def cancel_purchase(buyer_id: int):
    """
    Cancel a reserved purchase — release the account back to 'available'.
    No balance is deducted.
    """
    con = _con()
    row = con.execute("SELECT account_id FROM active_purchases WHERE buyer_id = ?", (buyer_id,)).fetchone()
    if row:
        con.execute("UPDATE accounts SET status = 'available' WHERE id = ?", (row["account_id"],))
        con.execute("DELETE FROM active_purchases WHERE buyer_id = ?", (buyer_id,))
    con.commit()
    con.close()


def get_reserved_account(buyer_id: int) -> dict | None:
    """Get the currently reserved account for a buyer."""
    con = _con()
    row = con.execute(
        """SELECT a.* FROM accounts a
           JOIN active_purchases ap ON a.id = ap.account_id
           WHERE ap.buyer_id = ?""",
        (buyer_id,)
    ).fetchone()
    con.close()
    return dict(row) if row else None


# ─── ACCOUNT FUNCTIONS ───────────────────────────────

def save_account(phone: str, password_2fa: str, session_string: str) -> bool:
    try:
        con = _con()
        con.execute(
            """INSERT INTO accounts (phone, password_2fa, session_string, status, added_at)
               VALUES (?, ?, ?, 'available', ?)
               ON CONFLICT(phone) DO UPDATE SET
               password_2fa=excluded.password_2fa,
               session_string=excluded.session_string,
               status='available',
               added_at=excluded.added_at""",
            (phone, password_2fa, session_string, datetime.now().strftime("%Y-%m-%d %H:%M"))
        )
        con.commit()
        con.close()
        return True
    except Exception as e:
        print(f"save_account error: {e}")
        return False


def get_available_count() -> int:
    con = _con()
    row = con.execute("SELECT COUNT(*) as cnt FROM accounts WHERE status = 'available'").fetchone()
    con.close()
    return row["cnt"] if row else 0


def get_sold_count() -> int:
    con = _con()
    row = con.execute("SELECT COUNT(*) as cnt FROM accounts WHERE status = 'sold'").fetchone()
    con.close()
    return row["cnt"] if row else 0


def peek_available_account():
    con = _con()
    row = con.execute("SELECT id FROM accounts WHERE status = 'available' ORDER BY id ASC LIMIT 1").fetchone()
    con.close()
    return dict(row) if row else None


def get_account_by_phone_id(account_id: int):
    con = _con()
    row = con.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    con.close()
    return dict(row) if row else None


def get_account_by_phone(phone: str):
    con = _con()
    row = con.execute("SELECT * FROM accounts WHERE phone = ?", (phone,)).fetchone()
    con.close()
    return dict(row) if row else None


# ─── TRANSACTION FUNCTIONS ───────────────────────────

def record_transaction(telegram_id: int, amount: float, tx_hash: str) -> bool:
    try:
        con = _con()
        con.execute(
            "INSERT INTO transactions (user_id, amount_ton, tx_hash, type, created_at) VALUES (?, ?, ?, 'deposit', ?)",
            (telegram_id, amount, tx_hash, datetime.now().isoformat())
        )
        con.commit()
        con.close()
        return True
    except Exception:
        return False


def get_total_revenue() -> float:
    con = _con()
    row = con.execute("SELECT SUM(amount_ton) as total FROM transactions WHERE type = 'deposit'").fetchone()
    con.close()
    return row["total"] if row["total"] else 0.0


# ─── SETTINGS ─────────────────────────────────────────

def get_price_ton() -> float:
    con = _con()
    row = con.execute("SELECT value FROM settings WHERE key = 'price_ton'").fetchone()
    con.close()
    return float(row["value"]) if row else 0.1


def set_price_ton(price: float):
    con = _con()
    con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('price_ton', ?)", (str(price),))
    con.commit()
    con.close()


# ─── STOCK MANAGEMENT ────────────────────────────────

def get_available_accounts() -> list:
    """Return all available accounts for admin to manage."""
    con = _con()
    rows = con.execute(
        "SELECT id, phone, added_at FROM accounts WHERE status = 'available' ORDER BY id ASC"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def delete_account(account_id: int) -> bool:
    """Permanently delete an available account from stock."""
    try:
        con = _con()
        con.execute("DELETE FROM accounts WHERE id = ? AND status = 'available'", (account_id,))
        con.commit()
        con.close()
        return True
    except Exception as e:
        print(f"delete_account error: {e}")
        return False


# ─── REVIEW FUNCTIONS ─────────────────────────────────

def has_reviewed(user_id: int) -> bool:
    con = _con()
    row = con.execute("SELECT 1 FROM reviews WHERE user_id = ?", (user_id,)).fetchone()
    con.close()
    return row is not None


def save_review(user_id: int, username: str, rating: int, review_text: str) -> bool:
    try:
        con = _con()
        con.execute(
            """INSERT OR IGNORE INTO reviews (user_id, username, rating, review_text, rewarded, created_at)
               VALUES (?, ?, ?, ?, 0, ?)""",
            (user_id, username, rating, review_text, datetime.now().strftime("%Y-%m-%d %H:%M"))
        )
        con.commit()
        con.close()
        return True
    except Exception as e:
        print(f"save_review error: {e}")
        return False


def mark_review_rewarded(user_id: int):
    con = _con()
    con.execute("UPDATE reviews SET rewarded = 1 WHERE user_id = ?", (user_id,))
    con.commit()
    con.close()


def get_all_reviews() -> list:
    con = _con()
    rows = con.execute(
        "SELECT * FROM reviews ORDER BY created_at DESC"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]
