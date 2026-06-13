"""
app.py — UnrealOTP Web Backend (Flask)

Replaces the Telegram bot's functionality with a full web app:
  - Auth (register/login) using SQLite
  - Wallet (deposit simulation, balance, transactions)
  - Services + live prices from Grizzly SMS
  - Buy number flow (real activation via Grizzly SMS)
  - OTP polling
  - Order history

Run:
    python app.py
"""
from __future__ import annotations

import os
import sqlite3
import time
import secrets
import threading
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, request, jsonify, session, g, send_from_directory

from grizzly_client import GrizzlyClient, GrizzlyError

# ════════════════════════════════════════════════════════
#  CONFIG
# ════════════════════════════════════════════════════════

GRIZZLY_API_KEY = os.environ.get("GRIZZLY_API_KEY", "YOUR_GRIZZLY_SMS_API_KEY")
DB_PATH = os.path.join(os.path.dirname(__file__), "unrealotp.db")

# Pricing markup applied on top of provider cost (in ₹). Adjust as needed.
MARKUP_INR = 1.5          # flat ₹ markup added per OTP
MARKUP_PCT = 0.0          # percentage markup (0-100)
USD_TO_INR = 88.0          # Grizzly prices are typically in USD/RUB-equivalent; adjust if needed
MIN_DEPOSIT = 10
DEFAULT_COUNTRY = "22"    # 22 = India in SMS-Activate/Grizzly numbering

# Cache for services+prices (avoid hammering Grizzly on every page load)
_price_cache = {"data": None, "ts": 0}
_PRICE_CACHE_TTL = 120  # seconds

grizzly = GrizzlyClient(GRIZZLY_API_KEY)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))


# ════════════════════════════════════════════════════════
#  DATABASE
# ════════════════════════════════════════════════════════

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        balance REAL NOT NULL DEFAULT 0,
        total_deposited REAL NOT NULL DEFAULT 0,
        total_orders INTEGER NOT NULL DEFAULT 0,
        api_key TEXT,
        referral_code TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        type TEXT NOT NULL,         -- deposit | purchase | refund
        method TEXT,
        ref_id TEXT,
        note TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        activation_id TEXT,
        service_code TEXT,
        service_name TEXT,
        country TEXT,
        phone TEXT,
        price REAL,
        status TEXT NOT NULL DEFAULT 'pending',  -- pending | waiting | received | cancelled | expired
        otp TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    """)
    conn.commit()
    conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ════════════════════════════════════════════════════════
#  AUTH HELPERS
# ════════════════════════════════════════════════════════

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "AUTH_REQUIRED", "message": "Please sign in"}), 401
        return f(*args, **kwargs)
    return wrapper


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def user_public(u):
    return {
        "id": u["id"],
        "name": u["name"],
        "email": u["email"],
        "balance": round(u["balance"], 2),
        "total_deposited": round(u["total_deposited"], 2),
        "total_orders": u["total_orders"],
        "referral_code": u["referral_code"],
        "api_key": u["api_key"],
    }


# ════════════════════════════════════════════════════════
#  PRICING
# ════════════════════════════════════════════════════════

def selling_price_inr(base_cost):
    """
    base_cost from Grizzly is in USD (provider's native currency).
    Convert to INR, apply markup.
    """
    inr = base_cost * USD_TO_INR
    inr = inr * (1 + MARKUP_PCT / 100) + MARKUP_INR
    return round(inr, 2)


def fetch_services_with_prices(country=DEFAULT_COUNTRY, force=False):
    now = time.time()
    if not force and _price_cache["data"] and (now - _price_cache["ts"] < _PRICE_CACHE_TTL):
        return _price_cache["data"]

    try:
        services = grizzly.get_services_list()
    except GrizzlyError as e:
        services = []

    try:
        prices = grizzly.get_prices_v2(country=country)
    except GrizzlyError:
        prices = {}

    name_map = {s["code"]: s["name"] for s in services}

    merged = []
    for code, countries in prices.items():
        info = countries.get(str(country)) or next(iter(countries.values()), None)
        if not info:
            continue
        cost = info.get("cost") or info.get("price") or 0
        count = info.get("count", 0)
        try:
            cost = float(cost)
        except (TypeError, ValueError):
            cost = 0
        if cost <= 0 or count <= 0:
            continue
        merged.append({
            "code": code,
            "name": name_map.get(code, code.title()),
            "price": selling_price_inr(cost),
            "count": count,
        })

    merged.sort(key=lambda s: s["name"].lower())
    _price_cache["data"] = merged
    _price_cache["ts"] = now
    return merged


# ════════════════════════════════════════════════════════
#  ROUTES — STATIC / FRONTEND
# ════════════════════════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory(app.template_folder, "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(app.static_folder, path)


# ════════════════════════════════════════════════════════
#  ROUTES — AUTH
# ════════════════════════════════════════════════════════

@app.post("/api/register")
def register():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not name or not email or not password:
        return jsonify({"error": "VALIDATION", "message": "All fields are required"}), 400
    if len(password) < 8:
        return jsonify({"error": "VALIDATION", "message": "Password must be at least 8 characters"}), 400

    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if existing:
        return jsonify({"error": "EXISTS", "message": "An account with this email already exists"}), 409

    pw_hash = _hash_password(password)
    api_key = "sk-uotp-" + secrets.token_hex(16)
    ref_code = secrets.token_hex(4).upper()

    cur = db.execute(
        "INSERT INTO users (name, email, password, balance, total_deposited, total_orders, api_key, referral_code, created_at) "
        "VALUES (?, ?, ?, 0, 0, 0, ?, ?, ?)",
        (name, email, pw_hash, api_key, ref_code, now_iso()),
    )
    db.commit()
    session["user_id"] = cur.lastrowid

    u = db.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify({"user": user_public(u)})


@app.post("/api/login")
def login():
    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"error": "VALIDATION", "message": "All fields are required"}), 400

    db = get_db()
    u = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not u or not _check_password(password, u["password"]):
        return jsonify({"error": "INVALID_CREDENTIALS", "message": "Invalid email or password"}), 401

    session["user_id"] = u["id"]
    return jsonify({"user": user_public(u)})


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
def me():
    u = current_user()
    if not u:
        return jsonify({"user": None})
    return jsonify({"user": user_public(u)})


@app.post("/api/regen-key")
@login_required
def regen_key():
    db = get_db()
    new_key = "sk-uotp-" + secrets.token_hex(16)
    db.execute("UPDATE users SET api_key=? WHERE id=?", (new_key, session["user_id"]))
    db.commit()
    return jsonify({"api_key": new_key})


# Simple password hashing without external deps
import hashlib

def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}${digest}"


def _check_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$")
    except ValueError:
        return False
    return hashlib.sha256((salt + password).encode()).hexdigest() == digest


# ════════════════════════════════════════════════════════
#  ROUTES — SERVICES / PRICES
# ════════════════════════════════════════════════════════

@app.get("/api/services")
def services():
    country = request.args.get("country", DEFAULT_COUNTRY)
    try:
        data = fetch_services_with_prices(country=country)
    except Exception as e:
        return jsonify({"error": "PROVIDER_ERROR", "message": str(e)}), 502
    return jsonify({"services": data})


@app.get("/api/countries")
def countries():
    try:
        data = grizzly.get_countries()
    except GrizzlyError as e:
        return jsonify({"error": e.code, "message": e.message}), 502
    return jsonify({"countries": data})


# ════════════════════════════════════════════════════════
#  ROUTES — WALLET
# ════════════════════════════════════════════════════════

@app.get("/api/wallet")
@login_required
def wallet():
    db = get_db()
    u = current_user()
    txns = db.execute(
        "SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 20",
        (u["id"],),
    ).fetchall()
    return jsonify({
        "balance": round(u["balance"], 2),
        "total_deposited": round(u["total_deposited"], 2),
        "transactions": [dict(t) for t in txns],
    })


@app.post("/api/deposit")
@login_required
def deposit():
    """
    Simulated deposit (UPI/Crypto). In production, wire this to a real
    payment gateway webhook and only credit balance after verified payment.
    """
    data = request.get_json(force=True) or {}
    amount = data.get("amount")
    method = data.get("method", "upi")
    utr = data.get("utr", "")

    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return jsonify({"error": "VALIDATION", "message": "Invalid amount"}), 400

    if amount < MIN_DEPOSIT:
        return jsonify({"error": "VALIDATION", "message": f"Minimum deposit is ₹{MIN_DEPOSIT}"}), 400

    if method == "upi" and (not utr or len(utr) < 8):
        return jsonify({"error": "VALIDATION", "message": "Enter a valid UTR number"}), 400

    db = get_db()
    uid = session["user_id"]
    db.execute(
        "UPDATE users SET balance = balance + ?, total_deposited = total_deposited + ? WHERE id=?",
        (amount, amount, uid),
    )
    db.execute(
        "INSERT INTO transactions (user_id, amount, type, method, ref_id, note, created_at) "
        "VALUES (?, ?, 'deposit', ?, ?, ?, ?)",
        (uid, amount, method, utr or None, f"{method.upper()} deposit", now_iso()),
    )
    db.commit()

    u = current_user()
    return jsonify({"balance": round(u["balance"], 2), "message": f"₹{amount:.2f} added to wallet"})


# ════════════════════════════════════════════════════════
#  ROUTES — BUY OTP
# ════════════════════════════════════════════════════════

@app.post("/api/buy")
@login_required
def buy_number():
    data = request.get_json(force=True) or {}
    service_code = data.get("service_code")
    country = str(data.get("country", DEFAULT_COUNTRY))

    if not service_code:
        return jsonify({"error": "VALIDATION", "message": "service_code is required"}), 400

    db = get_db()
    uid = session["user_id"]
    u = current_user()

    # Resolve current selling price for this service+country
    services_list = fetch_services_with_prices(country=country)
    svc = next((s for s in services_list if s["code"] == service_code), None)
    if not svc:
        return jsonify({"error": "NO_NUMBERS", "message": "Service unavailable for this country right now"}), 404

    price = svc["price"]

    if u["balance"] < price:
        return jsonify({"error": "INSUFFICIENT_BALANCE", "message": "Insufficient balance", "price": price}), 402

    # Buy from Grizzly
    try:
        result = grizzly.get_number(service_code, country=country)
    except GrizzlyError as e:
        return jsonify({"error": e.code, "message": e.message}), 502

    # Debit balance + record order
    db.execute("UPDATE users SET balance = balance - ? WHERE id=?", (price, uid))
    db.execute(
        "INSERT INTO transactions (user_id, amount, type, method, ref_id, note, created_at) "
        "VALUES (?, ?, 'purchase', 'wallet', ?, ?, ?)",
        (uid, -price, result["activation_id"], f"{svc['name']} OTP", now_iso()),
    )
    cur = db.execute(
        "INSERT INTO orders (user_id, activation_id, service_code, service_name, country, phone, price, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'waiting', ?)",
        (uid, result["activation_id"], service_code, svc["name"], country, result["phone"], price, now_iso()),
    )
    db.commit()

    return jsonify({
        "order_id": cur.lastrowid,
        "activation_id": result["activation_id"],
        "phone": result["phone"],
        "price": price,
        "balance": round(u["balance"] - price, 2),
    })


@app.get("/api/order/<int:order_id>/status")
@login_required
def order_status(order_id):
    db = get_db()
    uid = session["user_id"]
    order = db.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, uid)).fetchone()
    if not order:
        return jsonify({"error": "NOT_FOUND", "message": "Order not found"}), 404

    if order["status"] in ("received", "cancelled", "expired"):
        return jsonify({"status": order["status"], "otp": order["otp"], "phone": order["phone"]})

    try:
        result = grizzly.get_status(order["activation_id"])
    except GrizzlyError as e:
        return jsonify({"error": e.code, "message": e.message}), 502

    if result["status"] == "OK":
        otp = result.get("code")
        db.execute("UPDATE orders SET status='received', otp=? WHERE id=?", (otp, order_id))
        db.execute("UPDATE users SET total_orders = total_orders + 1 WHERE id=?", (uid,))
        db.commit()
        # Acknowledge receipt to provider (status=1)
        try:
            grizzly.set_status(order["activation_id"], 1)
        except GrizzlyError:
            pass
        return jsonify({"status": "received", "otp": otp, "phone": order["phone"]})

    if result["status"] == "CANCEL":
        db.execute("UPDATE orders SET status='cancelled' WHERE id=?", (order_id,))
        db.commit()
        return jsonify({"status": "cancelled", "phone": order["phone"]})

    return jsonify({"status": "waiting", "phone": order["phone"]})


@app.post("/api/order/<int:order_id>/cancel")
@login_required
def cancel_order(order_id):
    db = get_db()
    uid = session["user_id"]
    order = db.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, uid)).fetchone()
    if not order:
        return jsonify({"error": "NOT_FOUND", "message": "Order not found"}), 404

    if order["status"] not in ("waiting", "pending"):
        return jsonify({"error": "INVALID_STATE", "message": "Order cannot be cancelled"}), 400

    try:
        grizzly.set_status(order["activation_id"], 8)
    except GrizzlyError:
        pass  # best effort

    # Refund
    db.execute("UPDATE users SET balance = balance + ? WHERE id=?", (order["price"], uid))
    db.execute(
        "INSERT INTO transactions (user_id, amount, type, method, ref_id, note, created_at) "
        "VALUES (?, ?, 'refund', 'wallet', ?, ?, ?)",
        (uid, order["price"], order["activation_id"], f"Refund: {order['service_name']}", now_iso()),
    )
    db.execute("UPDATE orders SET status='cancelled' WHERE id=?", (order_id,))
    db.commit()

    u = current_user()
    return jsonify({"status": "cancelled", "balance": round(u["balance"], 2)})


@app.post("/api/order/<int:order_id>/resend")
@login_required
def resend_code(order_id):
    db = get_db()
    uid = session["user_id"]
    order = db.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, uid)).fetchone()
    if not order:
        return jsonify({"error": "NOT_FOUND", "message": "Order not found"}), 404

    try:
        grizzly.set_status(order["activation_id"], 3)
    except GrizzlyError as e:
        return jsonify({"error": e.code, "message": e.message}), 502

    db.execute("UPDATE orders SET status='waiting', otp=NULL WHERE id=?", (order_id,))
    db.commit()
    return jsonify({"status": "waiting"})


@app.get("/api/orders")
@login_required
def list_orders():
    db = get_db()
    uid = session["user_id"]
    orders = db.execute(
        "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 50", (uid,)
    ).fetchall()
    return jsonify({"orders": [dict(o) for o in orders]})


# ════════════════════════════════════════════════════════
#  ENTRY
# ════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
