import os
import sys
import json
import math
import hashlib
import re
import random
import asyncio
import time
import base64
import struct
import d1_database as database
from d1_wallet import Wallet
from datetime import datetime, timezone, timedelta
import contextlib
from contextlib import asynccontextmanager

import aiohttp
from aiohttp import web
from telethon import TelegramClient, events, Button, errors

from suppliers import SupplierService, SupplierError
from supplier_panel import SupplierPanel

# ── credentials ──────────────────────────────────────────────────────────────
API_ID = int(os.environ.get("TELEGRAM_API_ID", "0"))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
# Bot token is read from the TELEGRAM_BOT_TOKEN secret.
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BOT_USERNAME = "HMarketplacebot"  # Auto-updated from get_me() at startup.

# ── Premium (custom) emoji ─────────────────────────────────────────────────────
# Works only if this bot's owner account has Telegram Premium (or the bot has a
# Fragment username). Two ways to supply the custom-emoji document IDs:
#   1) PREMIUM_EMOJI_PACK  — short name of a custom-emoji pack (t.me/addemoji/<name>);
#      every emoji in it is auto-mapped (its `alt` emoji → its document id) at startup.
#   2) PREMIUM_EMOJI_MAP   — JSON of {"💰": 5368324170671202286, ...} explicit overrides.
# Any standard emoji in an outgoing message/caption that is present in the map is
# rendered as its premium version; anything unmapped falls back to the normal emoji.
PREMIUM_EMOJI_PACK = os.environ.get("PREMIUM_EMOJI_PACK", "").strip().split("/")[-1]
# Built-in custom-emoji IDs (public identifiers, safe to hardcode). Add more here as
# you collect their document ids, or override/extend via the PREMIUM_EMOJI_MAP env JSON.
_DEFAULT_PREMIUM_EMOJI_MAP = {
    "🔥": 5352727529511723136,
    "🛒": 5071058265660457858,
    "🪙": 6001287064589439895,
    "💲": 5445353829304387411,
    "🧾": 5444856076954520455,
    "👤": 5904630315946611415,
    "👋": 5985813388343644502,
    "💬": 5285350148451344065,
    "💰": 5472027899789843495,
    "✅": 5852871561983299073,
    "⚡️": 5409139210525366954,
    "2️⃣":  5253931337399674296,
      "🗃": 5280553894047392913,
    "👇": 5228931982037116397,
    "💳": 5328098344495490329,
    "💵": 5474139383021986749,
    "💸": 5077907879504118499,
    "✖️": 5818804920773512716,
    "🇮🇳":  6242055415010429981,
    "🍌": 5077907879504118499,
    "😜": 5206173732019659003,
    "🙊": 5282806754718012568,
    "🫥": 5276398496008663230,
    "💋": 5332714936122484922,
    "⬅️": 5983279327574233274,
    "📈": 4925053699996255088,
    "⚡": 5456140674028019486,
    "📖": 5409020553463878958,  # How to Use messagebox icon
    "💵": 5388591696139269118,
    "💯": 5422814644093868925,
    "🔢": 5206607081334906820,
    "🙌": 4925053699996255088,
    "✏️": 6025873488911274008,
    "➕": 6025873488911274008,
    "✔️": 5206607081334906820,
    "📦": 5323289282499064033,
    "🎁": 5440502878491930005,
    "🙊": 5350658940117986785,
    "🖊": 5985774024968379294,
    "👛": 5256186332669035163,
    "🍃": 5256110612395605858,
    # UPI payment page (page 1) — serial premium emojis
    "📲": 6147530606528959460,  # Pay via UPI ID or QR CODE header
    "☑️": 5244849092181385744,  # Pay EXACTLY line
    "❔": 6030848053177486888,  # Need help line
    # UTR entry page (page 2) — serial premium emojis
    "⏩": 6264982255379157511,  # You're adding line
    "⁉️": 5294404476382233385,  # Stuck? Contact line
    "📊": 4925053699996255088,

      # USDT tether coin — deposit heading
}
try:
    PREMIUM_EMOJI_MAP = dict(_DEFAULT_PREMIUM_EMOJI_MAP)
    PREMIUM_EMOJI_MAP.update(
        {
            k: int(v)
            for k, v in json.loads(os.environ.get("PREMIUM_EMOJI_MAP", "{}")).items()
        }
    )
except Exception:
    PREMIUM_EMOJI_MAP = dict(_DEFAULT_PREMIUM_EMOJI_MAP)

# Per-button premium-emoji icons (used only as button icons, NOT text replacement,
# so a bare "$" in message text is never touched).
USDT_ICON_ID = 5388591696139269118   # USDT tether coin premium emoji → crypto deposit
_USDT_ICON_OLD = 6222250474700018327  # kept in case callers reference it directly
INR_ICON_ID = 6242055415010429981
usdqq = 5893501660545682340  # ❤️ premium emoji → UPI (₹) / INR deposit button

# ── Admin Configuration ───────────────────────────────────────────────────────
ADMIN_IDS = [5010778910, 6568910046]   # Replace with your actual admin user ID(s)

# ── RPC config ───────────────────────────────────────────────────────────────
RPC_ENDPOINTS = {
    "eth": [
        "https://ethereum.publicnode.com",
        "https://eth.llamarpc.com",
        "https://rpc.ankr.com/eth",
    ],
    "bsc": [
        "https://bsc-dataseed.bnbchain.org",
        "https://bsc-rpc.publicnode.com",
        "https://rpc.ankr.com/bsc",
    ],
    "polygon": [
        "https://polygon-rpc.com",
        "https://polygon.publicnode.com",
        "https://polygon-bor-rpc.publicnode.com",
    ],
}

CHAIN_INFO = {
    "eth": {"name": "Ethereum", "native": "ETH", "block_time": 12},
    "bsc": {"name": "BNB Smart Chain", "native": "BNB", "block_time": 1.5},
    "polygon": {"name": "Polygon", "native": "MATIC/POL", "block_time": 2},
}

EVM_TOKENS = {
    "eth": [
        {
            "symbol": "USDT",
            "contract": "0xdac17f958d2ee523a2206206994597c13d831ec7",
            "decimals": 6,
            "price_key": "USDT",
        },
        {
            "symbol": "USDC",
            "contract": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
            "decimals": 6,
            "price_key": "USDC",
        },
        {
            "symbol": "DAI",
            "contract": "0x6b175474e89094c44da98b954eedeac495271d0f",
            "decimals": 18,
            "price_key": "DAI",
        },
        {
            "symbol": "WBTC",
            "contract": "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",
            "decimals": 8,
            "price_key": "BTC",
        },
        {
            "symbol": "WETH",
            "contract": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
            "decimals": 18,
            "price_key": "ETH",
        },
    ],
    "bsc": [
        {
            "symbol": "USDT",
            "contract": "0x55d398326f99059ff775485246999027b3197955",
            "decimals": 18,
            "price_key": "USDT",
        },
        {
            "symbol": "USDC",
            "contract": "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",
            "decimals": 18,
            "price_key": "USDC",
        },
        {
            "symbol": "DAI",
            "contract": "0x1af3f329e8be154074d8769d1ffa4ee058b1dbc3",
            "decimals": 18,
            "price_key": "DAI",
        },
        {
            "symbol": "BTCB",
            "contract": "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c",
            "decimals": 18,
            "price_key": "BTC",
        },
        {
            "symbol": "WBNB",
            "contract": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
            "decimals": 18,
            "price_key": "BNB",
        },
        {
            "symbol": "ETH",
            "contract": "0x2170ed0880ac9a755fd29b2688956bd959f933f8",
            "decimals": 18,
            "price_key": "ETH",
        },
    ],
    "polygon": [
        {
            "symbol": "USDT",
            "contract": "0xc2132d05d31c914a87c6611c10748aeb04b58e8f",
            "decimals": 6,
            "price_key": "USDT",
        },
        {
            "symbol": "USDC.e",
            "contract": "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",
            "decimals": 6,
            "price_key": "USDC",
        },
        {
            "symbol": "USDC",
            "contract": "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
            "decimals": 6,
            "price_key": "USDC",
        },
        {
            "symbol": "DAI",
            "contract": "0x8f3cf7ad23cd3cadbd9735aff958023239c6a063",
            "decimals": 18,
            "price_key": "DAI",
        },
        {
            "symbol": "WBTC",
            "contract": "0x1bfd67037b42c0d4067b8955404b1e40f3db87b1",
            "decimals": 8,
            "price_key": "BTC",
        },
        {
            "symbol": "WETH",
            "contract": "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619",
            "decimals": 18,
            "price_key": "ETH",
        },
    ],
}
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TRX_USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TRX_USDC_CONTRACT = "TE7oViNDFDADuLVH57eRX8Vus976oK2R45"

MAX_ADDRESSES_PER_MESSAGE = 3
PRICE_REFRESH_SECONDS = 600
STARTING_CREDITS = 0
REFERRAL_REWARD_CREDITS = 3
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wallet_bot.db")

# ── Store / Deposit configuration ─────────────────────────────────────────────
# Your OWN self-custody EVM receiving address. The SAME address works on both
# BNB Smart Chain and Polygon (an EVM address is controlled by one private key
# across all EVM chains). Users send USDT here on whichever network they pick; the
# bot verifies the on-chain transfer on that network and credits them.
# MUST start with 0x. Leave "" to disable deposits until you set it.
DEPOSIT_ADDRESS = "0xF784131662428Cd7AeF98267b04B3d4d30DeeADD"

# Tron is NOT an EVM chain — it uses its own address format (starts with T…) and its
# own network, so it needs a SEPARATE receiving address from the EVM one above.
DEPOSIT_ADDRESS_TRON = "TYXx89DRdCK8zRawzhatjXNKMTe8PkNYJP"

# TronGrid is Tron's public API used to look up a transaction and confirm the TRC20
# transfer. It works without a key but is rate-limited; set TRONGRID_API_KEY (free
# from trongrid.io) to raise the limit for a busy bot.
TRONGRID_API = "https://api.trongrid.io"
TRONGRID_API_KEY = os.environ.get("TRONGRID_API_KEY", "")

# Solana is NOT EVM and NOT Tron — its own network, address format, and USDT (an SPL
# token). It needs its OWN receiving address (a base58 32-byte pubkey).
DEPOSIT_ADDRESS_SOLANA = "HP53vu8whQhDeGJnqMs7LEWEz33K2XYEfaqJE7N2PcKq"

# TON (GRAM) receiving wallet — editable at runtime via Admin → Addresses.
DEPOSIT_ADDRESS_TON = "UQCo9urvJ7Z5-_J0hU3XYcO61Ncgej_zP03uFlASKw42UHk6"

# GRAM (TON) premium emoji for the coin button in the crypto deposit screen.
GRAM_ICON_ID = 5814462683067454974

# LTC (Litecoin) receiving wallet — editable at runtime via Admin → Addresses.
DEPOSIT_ADDRESS_LTC = "ltc1qg4akfvy9rzn0l7slynq3e2dajjtlu4suqjrhd7"

# LTC premium emoji for the coin button in the crypto deposit screen.
LTC_ICON_ID = 5273967072072706245

# BTC (Bitcoin) receiving wallet — editable at runtime via Admin → Addresses.
DEPOSIT_ADDRESS_BTC = "bc1qg4akfvy9rzn0l7slynq3e2dajjtlu4suywen4w"

# BTC premium emoji for the coin button in the crypto deposit screen.
BTC_ICON_ID = 5247247771286654614

# Solana JSON-RPC endpoint used to look up a transaction and confirm the SPL USDT
# transfer. The public node works but is heavily rate-limited; set SOLANA_RPC_URL
# (e.g. a free Helius/QuickNode endpoint) for a busy bot.
SOLANA_RPC = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

# ── UPI (India) — MANUAL approval, separate ₹ balance ───────────────────────────
# UPI has no public ledger, so UPI deposits can't be auto-verified on-chain like
# crypto. Flow: user enters a ₹ amount → we show our UPI ID + QR → user pays and
# submits their UPI reference number (UTR) → an admin approves it in the panel →
# the requested ₹ amount is credited to the user's SEPARATE INR balance
# (users.balance_inr), kept completely apart from the USD crypto balance.
# UPI_ID + the QR image are set from the admin panel (UPI_ID overlaid from the
# `upi_id` setting; the QR is stored on disk at UPI_QR_PATH).
UPI_ID = ""  # our receiving UPI VPA, e.g. name@bank — set from the admin panel
UPI_QR_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "upi_qr.png")
MIN_UPI_INR = 1.0  # ignore dust UPI deposits below this (₹); admin-editable (min_upi)


def upi_ready() -> bool:
    """True when UPI deposits are set up (both a UPI ID and a QR image exist)."""
    return bool(UPI_ID) and os.path.exists(UPI_QR_PATH)


# Networks accepted for deposits. All are USDT. EVM chains (bsc, polygon) share one
# 0x address; Tron uses its own T… address. Each chain has its own USDT contract and
# decimals (BSC=18, Polygon=6, Tron=6). `kind` routes on-chain verification.
DEPOSIT_TOKENS = {
    "bsc": {
        "symbol": "USDT",
        "chain": "bsc",
        "kind": "evm",
        "label": "BNB Smart Chain (BEP20)",
        "short": "BEP20",
        "address": DEPOSIT_ADDRESS,
        "contract": "0x55d398326f99059ff775485246999027b3197955",
        "decimals": 18,
    },
    "polygon": {
        "symbol": "USDT",
        "chain": "polygon",
        "kind": "evm",
        "label": "Polygon (POL network)",
        "short": "Polygon",
        "address": DEPOSIT_ADDRESS,
        "contract": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
        "decimals": 6,
    },
    "tron": {
        "symbol": "USDT",
        "chain": "tron",
        "kind": "tron",
        "label": "Tron (TRC20)",
        "short": "TRC20",
        "address": DEPOSIT_ADDRESS_TRON,
        "contract": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",  # USDT on Tron
        "decimals": 6,
    },
    "solana": {
        "symbol": "USDT",
        "chain": "solana",
        "kind": "solana",
        "label": "Solana (SPL)",
        "short": "Solana",
        "address": DEPOSIT_ADDRESS_SOLANA,
        "contract": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT (SPL) mint
        "decimals": 6,
    },
    # GRAM (TON) — native coin deposit, not USDT. USD value is converted at the
    # live TON/USD price. Verified on-chain via the public toncenter API using
    # the same unique-amount matching as the other chains.
    "ton": {
        "symbol": "TON",
        "chain": "ton",
        "kind": "ton",
        "label": "GRAM ( TON )",
        "short": "TON",
        "address": DEPOSIT_ADDRESS_TON,
        "contract": None,  # native coin — no token contract
        "decimals": 9,  # nanotons
    },
    # LTC (Litecoin) — native coin deposit. USD value is converted at the live
    # LTC/USD price. Verified on-chain via the public BlockCypher API using the
    # same unique-amount matching as TON.
    "ltc": {
        "symbol": "LTC",
        "chain": "ltc",
        "kind": "ltc",
        "label": "Litecoin",
        "short": "LTC",
        "address": DEPOSIT_ADDRESS_LTC,
        "contract": None,  # native coin — no token contract
        "decimals": 8,  # litoshis
    },
    # BTC (Bitcoin) — native coin deposit. USD value is converted at the live
    # BTC/USD price. Verified on-chain via the public BlockCypher API using the
    # same unique-amount matching as LTC.
    "btc": {
        "symbol": "BTC",
        "chain": "btc",
        "kind": "btc",
        "label": "Bitcoin",
        "short": "BTC",
        "address": DEPOSIT_ADDRESS_BTC,
        "contract": None,  # native coin — no token contract
        "decimals": 8,  # satoshis
    },
}
# Order the networks are offered to users. First entry is the default/fallback.
DEPOSIT_CHAINS = ["bsc", "polygon", "tron"]


def deposit_address_for(chain: str) -> str:
    """The receiving address for a given deposit network (EVM chains share one 0x
    address; Tron has its own T… address)."""
    tok = DEPOSIT_TOKENS.get(chain) or {}
    return tok.get("address") or DEPOSIT_ADDRESS


MIN_DEPOSIT_USD = 1.0  # ignore dust / accidental tiny sends below this


def intent_unit_scale(chain: str) -> int:
    """Units-per-coin used for a deposit intent's reserved amount on `chain`.

    USDT networks and TON use thousandths (milli) — a 0.001..0.099 unique
    suffix there is worth fractions of a cent. LTC/BTC use satoshis/litoshis
    (1e8 per coin) so the unique suffix stays negligible in USD; in milli-coin
    it would add up to ~0.099 BTC/LTC, wildly inflating the payable amount."""
    kind = (DEPOSIT_TOKENS.get(chain) or {}).get("kind")
    return 10**8 if kind in ("ltc", "btc") else 1000


def format_intent_amount(chain: str, units: int) -> str:
    """Human display of a reserved intent amount for `chain` (exact, no rounding)."""
    scale = intent_unit_scale(chain)
    decimals = 8 if scale == 10**8 else 3
    return f"{units / scale:.{decimals}f}"

# Public support handle shown to users on deposit / help / error screens.
ADMIN_CONTACT = "@realdrastic"

# Unique-amount matching: each deposit is assigned a slightly odd amount (e.g. the
# user asks for 5 USDT and is told to send exactly 5.017) so an incoming payment can
# be matched to the exact person who requested it — no wallet registration needed.
# The reservation lives DEPOSIT_INTENT_TTL_MINUTES minutes, then the odd amount is
# freed and can be handed to someone else.
DEPOSIT_INTENT_TTL_MINUTES = 15
DEPOSIT_SUFFIX_MAX = 99  # unique suffix range in thousandths (adds 0.001 .. 0.099 USDT)

# Credit packs sold in the shop. (credits, price_in_usd)
CREDIT_PACKS = [
    (10, 1.0),
    (30, 2.5),
    (75, 5.0),
    (200, 10.0),
]

# ── global price cache ────────────────────────────────────────────────────────
PRICES: dict[str, float] = {}
_prices_fetched_at: float = 0.0


class ApiError(Exception):
    pass


# ═══════════════════════════════════════════════════════════════════════════════
#  DATABASE — persistent connection
# ═════════════════════════════════════════════════════════════════════════════
# Each operation gets its own request scope; D1 is the only live database.
get_db = database.connect
_D1_WALLET = Wallet(globals())


async def init_db():
    async with get_db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                credits INTEGER DEFAULT 4,
                referred_by INTEGER,
                referral_rewarded INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER,
                referred_user INTEGER,
                reward INTEGER DEFAULT 3,
                created_at TEXT
            )
        """)
        # Deposits: one row per verified on-chain payment. tx_hash is UNIQUE so a
        # transaction can never be credited twice.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                tx_hash TEXT UNIQUE,
                token TEXT,
                amount REAL,
                usd_value REAL,
                created_at TEXT
            )
        """)
        # Deposit intents: one row per "I want to deposit X" request. Each holds a
        # unique odd amount (amount_milli, in thousandths of USDT) reserved until
        # expires_at. An incoming payment is matched to the pending, non-expired intent
        # whose amount equals what was actually sent.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS deposit_intents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                base_milli INTEGER NOT NULL,
                amount_milli INTEGER NOT NULL,
                chain TEXT NOT NULL DEFAULT 'bsc',
                status TEXT NOT NULL DEFAULT 'pending',
                tx_hash TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
        """)
        # Migration: add chain column to older deposit_intents tables if missing.
        async with db.execute("PRAGMA table_info(deposit_intents)") as cursor:
            intent_cols = [r[1] for r in await cursor.fetchall()]
        if "chain" not in intent_cols:
            await db.execute(
                "ALTER TABLE deposit_intents ADD COLUMN chain TEXT NOT NULL DEFAULT 'bsc'"
            )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_intents_live "
            "ON deposit_intents(status, expires_at)"
        )
        # DB-enforced uniqueness: no two pending intents may share an odd amount.
        # (Time-expired intents are swept to status='expired' before allocating a new
        # one, so this never blocks legitimate reuse after the 15-min window.)
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_intents_pending_amount "
            "ON deposit_intents(amount_milli) WHERE status='pending'"
        )
        # Migration: legacy DBs (from before one-live-intent-per-user was enforced)
        # may hold multiple pending rows for one user. Collapse them to the newest one
        # BEFORE creating the unique index below, or the index creation would fail.
        await db.execute(
            "UPDATE deposit_intents SET status='expired' "
            "WHERE status='pending' AND id NOT IN ("
            "  SELECT MAX(id) FROM deposit_intents WHERE status='pending' GROUP BY user_id"
            ")"
        )
        # DB-enforced: a user may have at most one pending reservation at a time. This
        # is what lets us infer the deposit network from the user's single live intent.
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_intents_pending_user "
            "ON deposit_intents(user_id) WHERE status='pending'"
        )
        # Migration: add USD balance column to existing users tables if missing.
        async with db.execute("PRAGMA table_info(users)") as cursor:
            cols = [r[1] for r in await cursor.fetchall()]
        if "balance_usd" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN balance_usd REAL DEFAULT 0")
        # Migration: wallet binding — the sending address a user is locked to for deposits.
        if "bound_wallet" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN bound_wallet TEXT")
        # Migration: when the wallet was bound — deposits must be dated AFTER this
        # (stops a watcher from registering someone else's address to claim an old tx).
        if "bound_at" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN bound_at TEXT")
        # Migration: admin ban flag. A banned user can't deposit or use the bot.
        if "banned" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0")
        if "banned_at" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN banned_at TEXT")
        # Migration: separate INR balance for UPI deposits (kept apart from USD).
        if "balance_inr" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN balance_inr REAL DEFAULT 0")
        # Settings: editable-from-admin config (deposit addresses, min deposit, …).
        # Overlays the hardcoded defaults at startup via apply_settings_overrides().
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # Audit log: every manual admin action (balance edits, bans, address changes)
        # so money/permission changes are always traceable to an admin + timestamp.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER,
                target_user INTEGER,
                action TEXT,
                amount REAL,
                note TEXT,
                created_at TEXT
            )
        """)
        # UPI deposit requests (manual approval). One row per "I want to add ₹X" ask.
        # Lifecycle: awaiting_utr → pending (UTR submitted) → approved / rejected.
        # On approval the requested amount_inr is credited to users.balance_inr.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS upi_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount_inr REAL NOT NULL,
                utr TEXT,
                status TEXT NOT NULL DEFAULT 'awaiting_utr',
                created_at TEXT NOT NULL,
                decided_by INTEGER,
                decided_at TEXT
            )
        """)
        # ── Digital product shop ────────────────  ───────────────────────    ───────
        # Products users can buy with their existing USD or INR balance. Dual
        # pricing: price_usd / price_inr (0 = not sold in that currency). A product
        # must have at least one non-zero price to be buyable.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                price_usd REAL NOT NULL DEFAULT 0,
                price_inr REAL NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """)
        # Deliverables: one row per one-time link/code. status 'available' until a
        # purchase claims it → 'sold' (never reused). Stock = COUNT(available).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS product_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'available',
                sold_to INTEGER,
                sold_at TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_items_available "
            "ON product_items(product_id, status)"
        )
        # One row per completed purchase. Records which item was delivered and what
        # currency/amount was charged (never mixes USD and INR).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                currency TEXT NOT NULL,
                amount REAL NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, id)"
        )
        # Migration: refund/re-deliver support. `status` gates a one-time refund
        # ('completed' → 'refunded'); `refunded_at` records when.
        async with db.execute("PRAGMA table_info(orders)") as cursor:
            order_cols = [r[1] for r in await cursor.fetchall()]
        if "status" not in order_cols:
            await db.execute(
                "ALTER TABLE orders ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'"
            )
        if "refunded_at" not in order_cols:
            await db.execute("ALTER TABLE orders ADD COLUMN refunded_at TEXT")
        # Migration: manual (email-fulfilled) orders store the buyer's email here.
        if "email" not in order_cols:
            await db.execute("ALTER TABLE orders ADD COLUMN email TEXT")
        # Migration: products can be 'instant' (pool of codes, delivered in-chat) or
        # 'manual' (admin sends an invite to the buyer's email; 2-way confirm flow).
        async with db.execute("PRAGMA table_info(products)") as cursor:
            product_cols = [r[1] for r in await cursor.fetchall()]
        if "fulfillment" not in product_cols:
            await db.execute(
                "ALTER TABLE products ADD COLUMN fulfillment TEXT NOT NULL "
                "DEFAULT 'instant'"
            )
        # ── Shop categories ────────────────────────────────────────────────────
        # Every product lives inside a category. The shop shows categories first,
        # then the products inside the tapped category. Products with a NULL
        # category_id (e.g. created before this migration) appear under an
        # automatic "Other" bucket until the admin assigns them a category.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        if "category_id" not in product_cols:
            await db.execute("ALTER TABLE products ADD COLUMN category_id INTEGER")
        async with db.execute("PRAGMA table_info(categories)") as cursor:
            category_cols = [r[1] for r in await cursor.fetchall()]
        # Optional premium (custom) emoji per category: emoji_id is the Telegram
        # custom-emoji document id, emoji_char is its standard-emoji fallback
        # (shown in buttons, upgraded to the premium emoji in message text).
        if "emoji_id" not in category_cols:
            await db.execute("ALTER TABLE categories ADD COLUMN emoji_id TEXT")
        if "emoji_char" not in category_cols:
            await db.execute("ALTER TABLE categories ADD COLUMN emoji_char TEXT")
        # Optional image per category (local file path, like product images).
        if "image_file_id" not in category_cols:
            await db.execute("ALTER TABLE categories ADD COLUMN image_file_id TEXT")
        # Same optional premium emoji for products.
        if "emoji_id" not in product_cols:
            await db.execute("ALTER TABLE products ADD COLUMN emoji_id TEXT")
        if "emoji_char" not in product_cols:
            await db.execute("ALTER TABLE products ADD COLUMN emoji_char TEXT")
        # Migration: per-product "How to Use" guide (markdown, admin-written).
        # Shown to buyers via a button after purchase and in order details.
        if "howto" not in product_cols:
            await db.execute("ALTER TABLE products ADD COLUMN howto TEXT")
        if "learn_more" not in product_cols:
            await db.execute("ALTER TABLE products ADD COLUMN learn_more TEXT")
        if "warranty_days" not in product_cols:
            await db.execute(
                "ALTER TABLE products ADD COLUMN warranty_days INTEGER NOT NULL DEFAULT 0"
            )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS product_stock_subscriptions (
                product_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (product_id, user_id)
            )
        """)
        # Extra premium-emoji mappings (e.g. emojis used inside product
        # descriptions) so they survive restarts.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS emoji_map (
                char TEXT PRIMARY KEY,
                doc_id TEXT NOT NULL
            )
        """)
        # Migration: product images (stored as local file paths).
        product_cols = await db.execute("PRAGMA table_info(products)")
        product_cols = {row[1] for row in await product_cols.fetchall()}
        if "image_file_id" not in product_cols:
            await db.execute(
                "ALTER TABLE products ADD COLUMN image_file_id TEXT"
            )
        else:
            # Clear legacy values from the old numeric-file-id format — they
            # can't be re-sent by Telegram. Only real paths (contain '/') stay.
            await db.execute(
                "UPDATE products SET image_file_id = NULL "
                "WHERE image_file_id IS NOT NULL AND image_file_id NOT LIKE '%/%'"
            )
        # ── Broadcasts ───────────────────────────────────────────────────────────
        # One row per admin broadcast. Every message delivered to a user is
        # recorded in broadcast_deliveries so a whole broadcast can later be
        # DELETED from every user's chat with one tap.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                preview TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'sending',
                total INTEGER NOT NULL DEFAULT 0,
                sent INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                finished_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_deliveries (
                broadcast_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY (broadcast_id, user_id, message_id)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_bdel_bcast "
            "ON broadcast_deliveries(broadcast_id)"
        )
        await db.commit()
    await SUPPLIERS.init()


async def get_or_create_user(user_id: int, referred_by: int = None) -> dict:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()

        is_new = row is None
        if row is None:
            now = datetime.now(timezone.utc).isoformat()
            await db.execute(
                "INSERT OR IGNORE INTO users (user_id, credits, referred_by, referral_rewarded, created_at) VALUES (?, ?, ?, 0, ?)",
                (user_id, STARTING_CREDITS, referred_by, now),
            )
            await db.commit()
            async with db.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ) as cursor:
                row = await cursor.fetchone()

        d = dict(row)
        d["_is_new"] = (
            is_new  # transient flag (not a column) so callers can notify on first join
        )
        return d


async def get_user(user_id: int) -> dict | None:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None


async def get_credits(user_id: int) -> int:
    user = await get_user(user_id)
    return user["credits"] if user else 0


async def deduct_credit(user_id: int):
    return await _D1_WALLET.deduct_credit(user_id)


async def add_credits(user_id: int, amount: int):
    return await _D1_WALLET.add_credits(user_id, amount)


async def remove_credits(user_id: int, amount: int):
    return await _D1_WALLET.remove_credits(user_id, amount)


# ── USD deposit balance ────────────────────────────────────────────────────────


async def get_balance_usd(user_id: int) -> float:
    user = await get_user(user_id)
    return (
        float(user["balance_usd"])
        if user and user.get("balance_usd") is not None
        else 0.0
    )


async def get_balance_inr(user_id: int) -> float:
    user = await get_user(user_id)
    return (
        float(user["balance_inr"])
        if user and user.get("balance_inr") is not None
        else 0.0
    )


async def delete_user_data(uid: int):
    return await _D1_WALLET.delete_user_data(uid)


async def get_balances(user_id: int) -> tuple[float, float]:
    """(usd, inr) in ONE user read — use instead of calling both getters,
    which each do their own full get_user() query."""
    user = await get_user(user_id)
    if not user:
        return 0.0, 0.0
    usd = user.get("balance_usd")
    inr = user.get("balance_inr")
    return (
        float(usd) if usd is not None else 0.0,
        float(inr) if inr is not None else 0.0,
    )


async def get_bound_wallet(user_id: int) -> str | None:
    """The sending wallet a user is locked to for deposits (lowercase 0x…), or None."""
    user = await get_user(user_id)
    return user["bound_wallet"] if user and user.get("bound_wallet") else None


async def get_bound_at(user_id: int) -> datetime | None:
    """When the user registered their current wallet (UTC), or None."""
    user = await get_user(user_id)
    ts = user.get("bound_at") if user else None
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


async def set_bound_wallet(user_id: int, wallet: str) -> None:
    """Bind a wallet and stamp the moment — deposits made before now won't count."""
    async with get_db() as db:
        await db.execute(
            "UPDATE users SET bound_wallet = ?, bound_at = ? WHERE user_id = ?",
            (wallet.lower(), datetime.now(timezone.utc).isoformat(), user_id),
        )
        await db.commit()


async def add_balance_usd(user_id: int, amount: float) -> float:
    return await _D1_WALLET.add_balance_usd(user_id, amount)


async def spend_balance_usd(user_id: int, amount: float) -> bool:
    """Atomically deduct USD if the user can afford it. Returns True on success.

    Uses a single conditional UPDATE so two concurrent purchases can never both
    succeed on the same funds (no read-then-write lost-update race).
    """
    async with get_db() as db:
        cursor = await db.execute(
            "UPDATE users SET balance_usd = ROUND(balance_usd - ?, 6) "
            "WHERE user_id = ? AND balance_usd >= ?",
            (amount, user_id, amount),
        )
        await db.commit()
        return cursor.rowcount > 0


async def spend_balance_inr(user_id: int, amount: float) -> bool:
    """Atomically deduct ₹ if the user can afford it. Returns True on success.
    Mirrors spend_balance_usd — a single conditional UPDATE so concurrent
    purchases can't both spend the same funds."""
    async with get_db() as db:
        cursor = await db.execute(
            "UPDATE users SET balance_inr = ROUND(balance_inr - ?, 2) "
            "WHERE user_id = ? AND balance_inr >= ?",
            (amount, user_id, amount),
        )
        await db.commit()
        return cursor.rowcount > 0


# ── Digital product shop ───────────────────────────────────────────────   ────────


# ─    Shop categories ────   ─────────────────────────────────────────────────────


async def create_category(
    name: str, emoji_id: int | None = None, emoji_char: str | None = None
) -> int:
    """Insert a new shop category and return its id."""
    async with get_db() as db:
        cursor = await db.execute(
            "INSERT INTO categories (name, created_at, emoji_id, emoji_char) "
            "VALUES (?, ?, ?, ?)",
            (name, datetime.now(timezone.utc).isoformat(), emoji_id, emoji_char),
        )
        await db.commit()
        return cursor.lastrowid


def cat_emoji(cat: dict | None) -> str:
    """Fallback-emoji label for a category (premium upgrade happens in parse mode)."""
    if cat and cat.get("emoji_char"):
        return cat["emoji_char"]
    return "   "


async def get_category(category_id: int) -> dict | None:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM categories WHERE id = ?", (category_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None


# ── Shop browse cache ────────────────────────────────────────────────────────
# list_categories() and list_active_products() are called on every shop screen
# tap (category list, product list, product detail). Caching them for 30 s
# eliminates the most-repeated DB reads with no visible staleness for users.
# Any admin write that changes products or categories busts the cache instantly
# via invalidate_shop_cache() so admins always see live data.

import time as _time

_SHOP_CACHE: dict = {}          # key → (result, expires_at)
_SHOP_CACHE_TTL = 30            # seconds


def invalidate_shop_cache():
    """Call after any product/category admin write."""
    _SHOP_CACHE.clear()


async def list_categories() -> list[dict]:
    """All categories plus how many products each contains (any status)."""
    key = "categories"
    hit = _SHOP_CACHE.get(key)
    if hit and _time.monotonic() < hit[1]:
        return hit[0]
    async with get_db() as _db:
        async with _db.execute(
            "SELECT c.*, ("
            "  SELECT COUNT(*) FROM products p WHERE p.category_id = c.id"
            ") AS product_count "
            "FROM categories c ORDER BY c.id"
        ) as cursor:
            rows = await cursor.fetchall()
    result = [dict(r) for r in rows]
    _SHOP_CACHE[key] = (result, _time.monotonic() + _SHOP_CACHE_TTL)
    return result


async def rename_category(
    category_id: int,
    name: str,
    emoji_id: int | None = None,
    emoji_char: str | None = None,
) -> None:
    """Rename a category. If emoji_id is given, the emoji is updated too;
    otherwise the existing emoji is kept."""
    async with get_db() as db:
        if emoji_id is not None:
            await db.execute(
                "UPDATE categories SET name = ?, emoji_id = ?, emoji_char = ? "
                "WHERE id = ?",
                (name, emoji_id, emoji_char, category_id),
            )
        else:
            await db.execute(
                "UPDATE categories SET name = ? WHERE id = ?", (name, category_id)
            )
        await db.commit()
    invalidate_shop_cache()


async def delete_category(category_id: int) -> None:
    async with database.connect() as db:
        await db.batch([
            database.statement("UPDATE products SET category_id=NULL WHERE category_id=?", (category_id,)),
            database.statement("DELETE FROM categories WHERE id=?", (category_id,)),
        ])
    invalidate_shop_cache()


async def set_product_category(product_id: int, category_id: int | None) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE products SET category_id = ? WHERE id = ?",
            (category_id, product_id),
        )
        await db.commit()
    invalidate_shop_cache()


async def count_uncategorized_products(active_only: bool = False) -> int:
    """How many products have no category (the "Other" bucket)."""
    q = "SELECT COUNT(*) FROM products WHERE category_id IS NULL"
    if active_only:
        q += " AND active = 1"
    async with get_db() as db:
        async with db.execute(q) as cursor:
            row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def create_product(
    name: str,
    description: str,
    price_usd: float,
    price_inr: float,
    category_id: int | None = None,
    emoji_id: int | None = None,
    emoji_char: str | None = None,
) -> int:
    """Insert a new product and return its id."""
    async with get_db() as db:
        cursor = await db.execute(
            "INSERT INTO products (name, description, price_usd, price_inr, active, created_at, category_id, emoji_id, emoji_char) "
            "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)",
            (
                name,
                description,
                round(price_usd, 6),
                round(price_inr, 2),
                datetime.now(timezone.utc).isoformat(),
                category_id,
                emoji_id,
                emoji_char,
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def get_product(product_id: int) -> dict | None:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM products WHERE id = ?", (product_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None


async def set_product_image(product_id: int, file_id: str | None):
    """Save a product image (Telegram file_id; None clears it)."""
    async with get_db() as db:
        await db.execute(
            "UPDATE products SET image_file_id = ? WHERE id = ?",
            (file_id, product_id),
        )
        await db.commit()


async def set_category_image(category_id: int, file_id: str | None):
    """Save a category image (local file path; None clears it)."""
    async with get_db() as db:
        await db.execute(
            "UPDATE categories SET image_file_id = ? WHERE id = ?",
            (file_id, category_id),
        )
        await db.commit()


async def set_product_howto(product_id: int, howto: str):
    """Save the admin-written 'How to Use' guide (markdown; '' clears it)."""
    async with get_db() as db:
        await db.execute(
            "UPDATE products SET howto = ? WHERE id = ?", (howto, product_id)
        )
        await db.commit()


def product_howto(p: dict | None) -> str:
    """The product's How-to-Use text, or '' if unset/blank."""
    return ((p or {}).get("howto") or "").strip()


async def set_product_learn_more(product_id: int, learn_more: str):
    """Save the admin-written 'Learn More' text (markdown; '' clears it)."""
    async with get_db() as db:
        await db.execute(
            "UPDATE products SET learn_more = ? WHERE id = ?",
            (learn_more, product_id),
        )
        await db.commit()


def product_learn_more(p: dict | None) -> str:
    """The product's Learn More text, or '' if unset/blank."""
    return ((p or {}).get("learn_more") or "").strip()


async def product_howto_by_id(product_id: int | None) -> str:
    if not product_id:
        return ""
    return product_howto(await get_product(product_id))


async def product_stock(product_id: int) -> int:
    """How many deliverables are still available for a product."""
    p = await get_product(product_id)
    if p and p.get("supplier_id"):
        supplier = await SUPPLIERS.product(product_id)
        if not supplier or not supplier["supplier_enabled"] or not supplier["supplier_available"]:
            return 0
        return supplier["supplier_stock"] if supplier["supplier_stock"] is not None else 99
    async with get_db() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM product_items "
            "WHERE product_id = ? AND status = 'available'",
            (product_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return int(row[0]) if row else 0


# When a purchase leaves a product with this many (or fewer) deliverables left,
# admins get a proactive low-stock alert so they can restock before it sells out.
LOW_STOCK_THRESHOLD = 3


async def get_low_stock_threshold() -> int:
    """Admin-editable low-stock alert threshold (settings key `low_stock`).
    Falls back to LOW_STOCK_THRESHOLD; clamped to >= 0."""
    raw = await get_setting("low_stock")
    if raw is not None:
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            pass
    return LOW_STOCK_THRESHOLD


async def list_active_products(category_id: int | None = None) -> list[dict]:
    """Active products plus their available stock, for the user-facing shop.
    category_id: None = all products, 0 = uncategorized only ("Other" bucket),
    otherwise only that category's products."""
    key = f"active_products:{category_id}"
    hit = _SHOP_CACHE.get(key)
    if hit and _time.monotonic() < hit[1]:
        return hit[0]
    q = (
        "SELECT p.*, CASE WHEN p.supplier_id IS NOT NULL THEN "
        "CASE WHEN p.supplier_available=1 AND EXISTS(SELECT 1 FROM wholesale_suppliers s WHERE s.id=p.supplier_id AND s.enabled=1) "
        "THEN COALESCE(p.supplier_stock,99) ELSE 0 END ELSE ("
        "  SELECT COUNT(*) FROM product_items pi "
        "  WHERE pi.product_id = p.id AND pi.status = 'available'"
        ") END AS stock "
        "FROM products p WHERE p.active = 1"
    )
    params: tuple = ()
    if category_id == 0:
        q += " AND p.category_id IS NULL"
    elif category_id is not None:
        q += " AND p.category_id = ?"
        params = (category_id,)
    q += " ORDER BY p.id"
    async with get_db() as _db:
        async with _db.execute(q, params) as cursor:
            rows = await cursor.fetchall()
    result = [dict(r) for r in rows]
    _SHOP_CACHE[key] = (result, _time.monotonic() + _SHOP_CACHE_TTL)
    return result


async def list_all_products(category_id: int | None = None) -> list[dict]:
    """Every product (active or not) plus stock, for the admin panel.
    category_id filter works like list_active_products (0 = uncategorized)."""
    q = (
        "SELECT p.*, CASE WHEN p.supplier_id IS NOT NULL THEN COALESCE(p.supplier_stock,99) ELSE ("
        "  SELECT COUNT(*) FROM product_items pi "
        "  WHERE pi.product_id = p.id AND pi.status = 'available'"
        ") END AS stock "
        "FROM products p"
    )
    params: tuple = ()
    if category_id == 0:
        q += " WHERE p.category_id IS NULL"
    elif category_id is not None:
        q += " WHERE p.category_id = ?"
        params = (category_id,)
    q += " ORDER BY p.id"
    async with get_db() as db:
        async with db.execute(q, params) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def add_product_items(product_id: int, contents: list[str]) -> int:
    """Add deliverables (one per line) to a product's stock. Returns how many added."""
    now_iso = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        await db.executemany(
            "INSERT INTO product_items (product_id, content, status, created_at) "
            "VALUES (?, ?, 'available', ?)",
            [(product_id, c, now_iso) for c in contents],
        )
        await db.commit()
    invalidate_shop_cache()
    return len(contents)


async def set_product_prices(
    product_id: int, price_usd: float, price_inr: float
) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE products SET price_usd = ?, price_inr = ? WHERE id = ?",
            (round(price_usd, 6), round(price_inr, 2), product_id),
        )
        await db.commit()
    invalidate_shop_cache()


async def set_product_warranty(product_id: int, days: int) -> None:
    if not isinstance(days, int) or isinstance(days, bool) or not 0 <= days <= 36500:
        raise ValueError("Warranty must be a whole number from 0 to 36500 days.")
    async with get_db() as db:
        await db.execute(
            "UPDATE products SET warranty_days = ? WHERE id = ?", (days, product_id)
        )
        await db.commit()


async def product_counts(product_id: int) -> tuple[int, int]:
    """Return (available, sold) deliverable counts for a product."""
    async with get_db() as db:
        async with db.execute(
            "SELECT "
            "  SUM(CASE WHEN status = 'available' THEN 1 ELSE 0 END), "
            "  SUM(CASE WHEN status = 'sold' THEN 1 ELSE 0 END) "
            "FROM product_items WHERE product_id = ?",
            (product_id,),
        ) as cursor:
            row = await cursor.fetchone()
    avail = int(row[0]) if row and row[0] is not None else 0
    sold = int(row[1]) if row and row[1] is not None else 0
    return avail, sold


async def set_product_meta(
    product_id: int,
    name: str,
    description: str,
    emoji_id: int | None = None,
    emoji_char: str | None = None,
) -> None:
    """Update name/description. If emoji_id is given, the emoji is updated too;
    otherwise the existing emoji is kept."""
    async with get_db() as db:
        if emoji_id is not None:
            await db.execute(
                "UPDATE products SET name = ?, description = ?, emoji_id = ?, "
                "emoji_char = ? WHERE id = ?",
                (name, description, emoji_id, emoji_char, product_id),
            )
        else:
            await db.execute(
                "UPDATE products SET name = ?, description = ? WHERE id = ?",
                (name, description, product_id),
            )
        await db.commit()


async def set_product_fulfillment(product_id: int, fulfillment: str) -> None:
    """Set a product's delivery type: 'instant' (code pool) or 'manual' (email invite)."""
    p = await get_product(product_id)
    if p and p.get("supplier_id"):
        return  # Supplier delivery is owned by its API, never the local stock pool.
    fulfillment = "manual" if fulfillment == "manual" else "instant"
    async with get_db() as db:
        await db.execute(
            "UPDATE products SET fulfillment = ? WHERE id = ?",
            (fulfillment, product_id),
        )
        await db.commit()


async def build_admin_product_view(product_id: int):
    """(text, button_rows) for the admin single-product management screen.
    Returns (None, None) if the product no longer exists."""
    p = await get_product(product_id)
    if not p:
        return None, None
    if p.get("supplier_id"):
        return await SUPPLIER_PANEL.product_admin(product_id)
    avail, sold = await product_counts(product_id)
    desc = (p["description"] or "").strip() or "—"
    is_manual = (p.get("fulfillment") or "instant") == "manual"
    if is_manual:
        stock_line = f"Delivery: 📧 Manual (email invite) · {sold} sold"
    else:
        stock_line = f"Delivery: ⚡ Instant · {avail} available · {sold} sold"
    cat = await get_category(p["category_id"]) if p.get("category_id") else None
    cat_line = (
        f"Category: 🗂 {cat['name']}" if cat else "Category: 🗂 Other / uncategorized"
    )
    text = (
        f"{p.get('emoji_char') or '📦'}  {p['name']}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{desc}\n\n"
        f"Price: {shop_price_label(p)}\n"
        f"Warranty: {p.get('warranty_days') or 0} days\n"
        f"{stock_line}\n"
        f"{cat_line}\n"
        f"Status: {'🟢 active (visible)' if p['active'] else '🔴 hidden'}\n"
        f"How to Use: {'📖 set' if product_howto(p) else '— not set'}\n"
        f"Learn More: {'ℹ️ set' if product_learn_more(p) else '— not set'}\n\n"
        f"🔗 Share link (opens this product directly):\n"
        f"{product_deep_link(product_id)}"
    )
    rows = []
    if not is_manual:
        rows.append(
            [
                Button.inline("➕ Add Stock", f"adm_stock:{product_id}".encode()),
                Button.inline("📦 View Stock", f"adm_viewstock:{product_id}".encode()),
            ]
        )
    rows.append(
        [Button.inline("💲 Edit Prices", f"adm_setprice:{product_id}".encode())]
    )
    rows.append(
        [Button.inline("🛡 Edit Warranty", f"adm_warranty:{product_id}".encode())]
    )
    rows.append(
        [
            Button.inline(
                "⚡ Switch to Instant" if is_manual else "📧 Switch to Manual (email)",
                f"adm_ftype:{product_id}".encode(),
            )
        ]
    )
    rows.append(
        [
            Button.inline("✏️ Name", f"adm_rename:{product_id}".encode()),
            Button.inline("📝 Description", f"adm_redesc:{product_id}".encode()),
        ]
    )
    rows.append(
        [
            Button.inline(
                "How to Use",
                f"adm_rehowto:{product_id}".encode(),
                icon=PREMIUM_EMOJI_MAP.get("📖"),
            ),
            Button.inline(
                "Learn More",
                f"adm_relearn:{product_id}".encode(),
                icon=5339399017631591044,
            ),
        ]
    )
    rows.append(
        [Button.inline("🖼️ Product Image", f"adm_reimg:{product_id}".encode())]
    )
    rows.append(
        [Button.inline("🗑 Delete Product", f"adm_delprod:{product_id}".encode())]
    )
    rows.append([btn_back()])
    return text, rows


async def build_admin_stock_view(product_id: int):
    """(text, button_rows) for the admin stock list with per-item delete
    buttons, or None if the product doesn't exist."""
    p = await get_product(product_id)
    if not p:
        return None
    async with get_db() as db:
        async with db.execute(
            "SELECT id, content FROM product_items "
            "WHERE product_id = ? AND status = 'available' "
            "ORDER BY id LIMIT 30",
            (product_id,),
        ) as cursor:
            items = await cursor.fetchall()
    stock = await product_stock(product_id)
    home_row = [
        Button.inline("🏠 Home to Product", f"adm_prod:{product_id}".encode())
    ]
    if not items:
        return (
            f"📦  Stock — {p['name']}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "No available stock.",
            [
                [Button.inline("➕ Add Stock", f"adm_stock:{product_id}".encode())],
                home_row,
            ],
        )
    item_lines = []
    rows = []
    for iid, content in items:
        preview = (content or "").replace("\n", " ")
        if len(preview) > 60:
            preview = preview[:57] + "..."
        item_lines.append(f"#{iid}: `{preview}`")
        rows.append(
            [
                Button.inline(
                    f"🗑 Delete #{iid}",
                    f"adm_delitem:{product_id}:{iid}".encode(),
                )
            ]
        )
    rows.append(home_row)
    more = "" if stock <= 30 else f"\n(showing first 30 of {stock})"
    return (
        f"📦  Stock — {p['name']} ({stock} available){more}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n" + "\n".join(item_lines),
        rows,
    )


async def build_admin_order_view(order_id: int):
    """(text, button_rows) for the admin single-order screen with refund / re-deliver.
    Falls back to a not-found screen if the order is gone."""
    if await SUPPLIERS.order(order_id):
        return await SUPPLIER_PANEL.order_view(order_id, 0, admin=True)
    o = await get_order(order_id)
    if not o:
        return (
            "🧾  Order not found.\n━━━━━━━━━━━━━━━━━━━━━━━\n\nIt may have been removed.",
            [[Button.inline("🏠 Home to Orders", b"adm_orders")]],
        )
    sym = "$" if o["currency"] == "usd" else "₹"
    ts = (o["created_at"] or "")[:16].replace("T", " ")
    name = o.get("product_name") or "(removed product)"
    status = o["status"]
    is_manual = (o.get("fulfillment") or "instant") == "manual"
    if status == "refunded":
        rts = (o.get("refunded_at") or "")[:16].replace("T", " ")
        state = f"↩️ Refunded ({rts})" if rts else "↩️ Refunded"
    else:
        state = ORDER_STATUS_LABELS.get(status, status or "✅ Completed")
    text = (
        f"🧾  Order #{o['id']}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"User: `{o['user_id']}`\n"
        f"Product: {name}\n"
        f"Paid: {sym}{o['amount']:.2f}\n"
        f"Status: {state}\n"
        f"Placed: {ts}\n\n"
    )
    if is_manual:
        text += f"📧 Delivery: Manual (email invite)\nEmail: `{o.get('email') or '—'}`"
    else:
        text += f"Delivered item:\n`{o.get('content') or '(unavailable)'}`"
    rows = []
    if is_manual:
        if status == "processing":
            rows.append(
                [Button.inline("✅ Mark Mail Sent", f"omail_sent:{order_id}".encode())]
            )
        if status != "refunded":
            rows.append(
                [
                    Button.inline(
                        "↩️ Refund to balance", f"adm_refund:{order_id}".encode()
                    )
                ]
            )
    else:
        if status == "completed":
            rows.append(
                [
                    Button.inline(
                        "↩️ Refund to balance", f"adm_refund:{order_id}".encode()
                    )
                ]
            )
            rows.append(
                [
                    Button.inline(
                        "🔁 Re-deliver item", f"adm_redeliver:{order_id}".encode()
                    )
                ]
            )
    rows.append([Button.inline("🏠 Home to Orders", b"adm_orders")])
    return text, rows


async def set_product_active(product_id: int, active: bool) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE products SET active = ? WHERE id = ?",
            (1 if active else 0, product_id),
        )
        await db.commit()
    invalidate_shop_cache()


async def delete_product(product_id: int) -> None:
    async with database.connect() as db:
        await db.batch([
            database.statement("DELETE FROM product_items WHERE product_id=? AND status='available'", (product_id,)),
            database.statement("DELETE FROM products WHERE id=?", (product_id,)),
        ])
    invalidate_shop_cache()


async def buy_product(user_id: int, product_id: int, currency: str, qty: int=1, *, supplier_token=None):
    p = await get_product(product_id)
    if p and p.get("supplier_id"):
        try:
            if supplier_token is None:
                checkout = await SUPPLIERS.checkout(user_id, product_id, currency, qty)
                supplier_token = checkout["token"]
            else:
                checkout = await SUPPLIERS.one(
                    "SELECT * FROM wholesale_checkouts WHERE token=? AND user_id=?",
                    (supplier_token, user_id))
                if not checkout or (checkout["product_id"], checkout["currency"], checkout["quantity"]) != (product_id, currency, qty):
                    raise ValueError("Invalid checkout. Open the product and try again.")
            order = await SUPPLIERS.confirm(user_id, supplier_token)
        except (ValueError, SupplierError) as exc:
            return "supplier_error", str(exc)
        if order["state"] != "completed":
            return "supplier_order", order["order_id"]
        balances = await get_balances(user_id)
        return "ok", {
            "order_id": order["order_id"], "name": order["product_name"],
            "qty": order["quantity"], "amount": order["amount"],
            "new_balance": balances[0 if currency == "usd" else 1],
            "content": (
                "Open My Orders to view your delivered items."
                if len(order["content"] or "") > 2000 or "`" in (order["content"] or "")
                else order["content"] or "Activation completed."
            ),
        }
    return await _D1_WALLET.buy_product(user_id, product_id, currency, qty)


async def purchase_confirmation_data(user_id, p, currency, qty):
    if p.get("supplier_id"):
        checkout = await SUPPLIERS.checkout(user_id, p["id"], currency, qty)
        return f"wc:confirm:{checkout['token']}".encode()
    return f"shop_ok:{p['id']}:{currency}:{qty}".encode()


async def get_user_orders(user_id: int, limit: int = 20) -> list[dict]:
    """A user's purchase history with the delivered content and product name."""
    async with get_db() as db:
        async with db.execute(
            "SELECT o.id, o.product_id, o.currency, o.amount, o.created_at, "
            "  o.status, o.email, p.fulfillment, p.category_id, "
            "  p.name AS product_name, pi.content AS content, "
            # Per-customer order number: this user's 1st purchase is #1, their
            # 10th is #10 — independent of the bot-wide order id.
            "  (SELECT COUNT(*) FROM orders o2 "
            "   WHERE o2.user_id = o.user_id AND o2.id <= o.id) AS user_order_no "
            "FROM orders o "
            "LEFT JOIN products p ON p.id = o.product_id "
            "LEFT JOIN product_items pi ON pi.id = o.item_id "
            "WHERE o.user_id = ? ORDER BY o.id DESC LIMIT ?",
            (user_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_recent_orders(limit: int = 10) -> list[dict]:
    """Recent purchases across all users, for the admin panel."""
    async with get_db() as db:
        async with db.execute(
            "SELECT o.id, o.user_id, o.product_id, o.currency, o.amount, o.status, "
            "  o.created_at, o.email, p.fulfillment, p.name AS product_name "
            "FROM orders o LEFT JOIN products p ON p.id = o.product_id "
            "ORDER BY o.id DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_order(order_id: int) -> dict | None:
    """A single order with its product name and currently-linked deliverable."""
    async with get_db() as db:
        async with db.execute(
            "SELECT o.id, o.user_id, o.product_id, o.item_id, o.currency, o.amount, "
            "  o.status, o.created_at, o.refunded_at, o.email, p.fulfillment, "
            "  p.name AS product_name, pi.content AS content, "
            "  (SELECT COUNT(*) FROM orders o2 "
            "   WHERE o2.user_id = o.user_id AND o2.id <= o.id) AS user_order_no "
            "FROM orders o "
            "LEFT JOIN products p ON p.id = o.product_id "
            "LEFT JOIN product_items pi ON pi.id = o.item_id "
            "WHERE o.id = ?",
            (order_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


async def build_user_order_view(order_id: int, user_id: int):
    """(text, button_rows) for a buyer's single-order detail screen.

    Returns None if the order doesn't exist or isn't owned by this user."""
    o = await get_order(order_id)
    if not o or o["user_id"] != user_id:
        return None
    supplier_order = await SUPPLIERS.order(order_id)
    if supplier_order:
        content = supplier_order["content"] or "Activation completed."
        if supplier_order["state"] not in ("completed", "refunded") or len(content) > 2000 or "`" in content:
            return await SUPPLIER_PANEL.order_view(order_id, user_id)
        o["content"] = content
    sym = "$" if o["currency"] == "usd" else "₹"
    ts = (o["created_at"] or "")[:16].replace("T", " ")
    name = o.get("product_name") or "(removed product)"
    status = o["status"]
    is_manual = (o.get("fulfillment") or "instant") == "manual"
    if status == "refunded":
        state = "↩️ Refunded to your balance"
    elif is_manual:
        state = {
            "awaiting_email": "✉️ Waiting for your email",
            "processing": "🕐 Preparing your invite (within 1h)",
            "delivered": "📬 Invite sent — check your email",
            "completed": "✅ Received",
        }.get(status, "🕐 Processing")
    else:
        state = "✅ Delivered"
    # Buyers see THEIR order count (#1 = their first purchase), not the
    # bot-wide order id. Admin screens keep the global id for management.
    order_no = o.get("user_order_no") or o["id"]
    text = (
        f"🧾  Order #{order_no}\n"
        "━━━━━━━━━━━━━━  ━━━━━\n\n"
        f"📦 {name}\n"
        f"💵 Paid: {sym}{o['amount']:.2f}\n"
        f"📅 {ts}\n"
        f"📌 {state}\n"
    )
    rows = []
    if is_manual:
        if o.get("email"):
            text += f"\n📧 Email: `{o.get('email')}`\n"
        if status == "delivered":
            text += "\nReceived your invite? Let us know below."
            rows.append(
                [
                    Button.inline("✅ Got it!", f"ogot:{order_id}".encode()),
                    Button.inline("🆘 Help", f"osupport:{order_id}".encode()),
                ]
            )
        elif status == "processing":
            text += (
                "\nYour invite is on its way to the email above — "
                "usually a few minutes (up to 1h)."
            )
        elif status == "awaiting_email":
            text += "\nWe still need your email to send the invite."
    elif status == "refunded":
        text += "\nThis item was refunded, so it's no longer active."
    else:
        text += (
            "\n🎁 Your item:\n"
            f"{o.get('content') or '(unavailable)'}\n\n"
            "Tap the text above to copy it."
        )
    # Product usage guide (admin-written): show for any non-refunded order.
    if status != "refunded" and await product_howto_by_id(o.get("product_id")):
        rows.append(
            [
                Button.inline(
                    "How to Use",
                    f"howto:{o['product_id']}".encode(),
                    icon=PREMIUM_EMOJI_MAP.get("📖"),
                )
            ]
        )
    rows.append([btn_dep_history()])
    rows.append([Button.inline("🧾 All Orders", b"orders")])
    rows.append([btn_back()])
    return text, rows


async def refund_order(order_id: int):
    return await _D1_WALLET.refund_order(order_id)


async def redeliver_order(order_id: int):
    return await _D1_WALLET.redeliver_order(order_id)


async def attach_order_email(order_id: int, user_id: int, email: str):
    return await _D1_WALLET.attach_order_email(order_id, user_id, email)


async def mark_order_mailed(order_id: int):
    return await _D1_WALLET.mark_order_mailed(order_id)


async def mark_order_received(order_id: int, user_id: int):
    return await _D1_WALLET.mark_order_received(order_id, user_id)


# Human-readable label for an order's lifecycle status (manual + instant).
ORDER_STATUS_LABELS = {
    "supplier_pending": "🕐 Supplier processing",
    "supplier_activation": "✉️ Awaiting activation details",
    "awaiting_email": "📧 Awaiting email",
    "processing": "🕐 Processing — send invite",
    "delivered": "📬 Invite sent — awaiting user",
    "completed": "✅ Completed",
    "refunded": "↩️ Refunded",
}


# ── Settings (admin-editable config) ─────────  ──  ─────────────────────   ────────


async def get_setting(key: str) -> str | None:
    async with get_db() as db:
        async with db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None


async def set_setting(key: str, value: str) -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()


async def apply_settings_overrides() -> None:
    """Overlay DB-stored settings on top of the hardcoded config constants so an
    admin can change deposit addresses / min deposit at runtime without a redeploy."""
    global \
        DEPOSIT_ADDRESS, \
        DEPOSIT_ADDRESS_TRON, \
        DEPOSIT_ADDRESS_SOLANA, \
        DEPOSIT_ADDRESS_TON, \
        DEPOSIT_ADDRESS_LTC, \
        DEPOSIT_ADDRESS_BTC, \
        MIN_DEPOSIT_USD
    global UPI_ID, MIN_UPI_INR
    evm = await get_setting("addr_evm")
    if evm:
        DEPOSIT_ADDRESS = evm
        DEPOSIT_TOKENS["bsc"]["address"] = evm
        DEPOSIT_TOKENS["polygon"]["address"] = evm
    tron = await get_setting("addr_tron")
    if tron:
        DEPOSIT_ADDRESS_TRON = tron
        DEPOSIT_TOKENS["tron"]["address"] = tron
    sol = await get_setting("addr_solana")
    if sol:
        DEPOSIT_ADDRESS_SOLANA = sol
        if "solana" in DEPOSIT_TOKENS:
            DEPOSIT_TOKENS["solana"]["address"] = sol
    ton = await get_setting("addr_ton")
    if ton:
        DEPOSIT_ADDRESS_TON = ton
        if "ton" in DEPOSIT_TOKENS:
            DEPOSIT_TOKENS["ton"]["address"] = ton
    ltc = await get_setting("addr_ltc")
    if ltc:
        DEPOSIT_ADDRESS_LTC = ltc
        if "ltc" in DEPOSIT_TOKENS:
            DEPOSIT_TOKENS["ltc"]["address"] = ltc
    btc = await get_setting("addr_btc")
    if btc:
        DEPOSIT_ADDRESS_BTC = btc
        if "btc" in DEPOSIT_TOKENS:
            DEPOSIT_TOKENS["btc"]["address"] = btc
    mind = await get_setting("min_deposit")
    if mind:
        try:
            MIN_DEPOSIT_USD = float(mind)
        except ValueError:
            pass
    upi = await get_setting("upi_id")
    if upi:
        UPI_ID = upi
    minu = await get_setting("min_upi")
    if minu:
        try:
            MIN_UPI_INR = float(minu)
        except ValueError:
            pass


# ── Ban + admin balance actions + audit log ────  ──────────────────────  ─   ──────


# Fast-path cache for the "known user + banned?" check that runs on every
# single button tap and message. Without it each interaction pays two extra
# SQLite round-trips (new connection each) before the handler even starts.
# Invalidated by set_banned(), so bans/unbans always apply instantly.
_USER_BAN_CACHE: dict[int, bool] = {}


async def ensure_user_not_banned(user_id: int) -> bool:
    """Ensure the user row exists and return True if they are banned.
    Cached in memory; one DB hit per user per bot run."""
    banned = _USER_BAN_CACHE.get(user_id)
    if banned is None:
        user = await get_or_create_user(user_id)
        banned = bool(user and user.get("banned"))
        _USER_BAN_CACHE[user_id] = banned
    return banned


async def is_banned(user_id: int) -> bool:
    user = await get_user(user_id)
    return bool(user and user.get("banned"))


async def set_banned(user_id: int, banned: bool) -> None:
    _USER_BAN_CACHE[user_id] = banned
    await get_or_create_user(user_id)
    async with get_db() as db:
        await db.execute(
            "UPDATE users SET banned = ?, banned_at = ? WHERE user_id = ?",
            (
                1 if banned else 0,
                datetime.now(timezone.utc).isoformat() if banned else None,
                user_id,
            ),
        )
        await db.commit()


async def admin_adjust_balance(user_id: int, delta: float):
    return await _D1_WALLET.admin_adjust_balance(user_id, delta)


async def log_admin_action(
    admin_id: int,
    target_user: int | None,
    action: str,
    amount: float | None,
    note: str = "",
) -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT INTO admin_actions (admin_id, target_user, action, amount, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                admin_id,
                target_user,
                action,
                amount,
                note,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db.commit()


async def get_recent_deposits(limit: int = 10) -> list[dict]:
    async with get_db() as db:
        async with db.execute(
            "SELECT user_id, token, amount, usd_value, created_at "
            "FROM deposits ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_user_deposits(user_id: int, limit: int = 20) -> list[dict]:
    """A user's deposit history, newest first: verified crypto deposits (USD) plus
    approved UPI deposits (INR), merged into one list. Each row has:
    {kind: 'crypto'|'upi', token, amount, currency, created_at}."""
    async with get_db() as db:
        async with db.execute(
            "SELECT token, amount, usd_value, created_at "
            "FROM deposits WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ) as cursor:
            crypto_rows = await cursor.fetchall()
        async with db.execute(
            "SELECT amount_inr, created_at FROM upi_requests "
            "WHERE user_id = ? AND status = 'approved' ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ) as cursor:
            upi_rows = await cursor.fetchall()
    items = [
        {
            "kind": "crypto",
            "token": r["token"],
            "amount": float(r["usd_value"] or 0.0),
            "currency": "usd",
            "created_at": r["created_at"] or "",
        }
        for r in crypto_rows
    ] + [
        {
            "kind": "upi",
            "token": "UPI",
            "amount": float(r["amount_inr"] or 0.0),
            "currency": "inr",
            "created_at": r["created_at"] or "",
        }
        for r in upi_rows
    ]
    items.sort(key=lambda d: d["created_at"], reverse=True)
    return items[:limit]


async def get_user_deposit_total(user_id: int) -> tuple[int, float]:
    async with get_db() as db:
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(usd_value), 0) FROM deposits WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return (int(row[0]), float(row[1])) if row else (0, 0.0)


# ── UPI deposit requests (manual approval, INR balance) ─────────────────────────


async def admin_adjust_balance_inr(user_id: int, delta: float):
    return await _D1_WALLET.admin_adjust_balance_inr(user_id, delta)


async def create_upi_request(user_id: int, amount_inr: float) -> int:
    """Create a UPI deposit request (status 'awaiting_utr') and return its id."""
    now_iso = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        cursor = await db.execute(
            "INSERT INTO upi_requests (user_id, amount_inr, status, created_at) "
            "VALUES (?, ?, 'awaiting_utr', ?)",
            (user_id, round(amount_inr, 2), now_iso),
        )
        await db.commit()
        return cursor.lastrowid


async def attach_upi_utr(req_id: int, user_id: int, utr: str) -> bool:
    """Attach the user-submitted UTR and move the request to 'pending' (awaiting an
    admin decision). Only flips a request that is still 'awaiting_utr' AND belongs to
    this user. Returns True on success."""
    now_iso = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        cursor = await db.execute(
            "UPDATE upi_requests SET utr = ?, status = 'pending', created_at = ? "
            "WHERE id = ? AND user_id = ? AND status = 'awaiting_utr'",
            (utr, now_iso, req_id, user_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def get_upi_request(req_id: int) -> dict | None:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM upi_requests WHERE id = ?", (req_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


async def get_pending_upi_requests(limit: int = 10) -> list[dict]:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM upi_requests WHERE status = 'pending' ORDER BY id ASC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def approve_upi_request(req_id: int, admin_id: int):
    return await _D1_WALLET.approve_upi_request(req_id, admin_id)


async def reject_upi_request(req_id: int, admin_id: int) -> tuple[int, float] | None:
    """Reject a pending UPI request. Returns (user_id, amount_inr) or None if it
    wasn't pending."""
    now_iso = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        async with db.execute(
            "SELECT user_id, amount_inr FROM upi_requests WHERE id = ? AND status = 'pending'",
            (req_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        cursor = await db.execute(
            "UPDATE upi_requests SET status = 'rejected', decided_by = ?, decided_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (admin_id, now_iso, req_id),
        )
        await db.commit()
        if cursor.rowcount != 1:
            return None
    return (int(row[0]), float(row[1]))


# ─    Deposit-address validation (used by the admin address editor) ───────────────


def validate_deposit_address(kind: str, addr: str) -> str | None:
    """Return a cleaned address if it's valid for the chain kind, else None."""
    addr = (addr or "").strip()
    if kind == "evm":
        return addr if re.fullmatch(r"0x[0-9a-fA-F]{40}", addr) else None
    if kind == "tron":
        try:
            tron_base58_to_hex20(addr)
            return addr
        except Exception:
            return None
    if kind == "solana":
        return addr if solana_pubkey_valid(addr) else None
    if kind == "ton":
        # User-friendly base64url form: flags byte (E/U + Q) + 46 chars.
        return addr if re.fullmatch(r"[EU]Q[A-Za-z0-9_-]{46}", addr) else None
    if kind == "ltc":
        # Bech32 (ltc1…) or legacy base58 (L/M…).
        if re.fullmatch(r"ltc1[a-z0-9]{20,80}", addr):
            return addr
        return addr if re.fullmatch(r"[LM][a-km-zA-HJ-NP-Z1-9]{25,34}", addr) else None
    if kind == "btc":
        # Bech32/bech32m (bc1…) or legacy base58 (1…/3…).
        if re.fullmatch(r"bc1[a-z0-9]{20,80}", addr):
            return addr
        return addr if re.fullmatch(r"[13][a-km-zA-HJ-NP-Z1-9]{25,34}", addr) else None
    return None


async def claim_and_credit_deposit(intent_id: int, user_id: int, amount_milli: int, tx_hash: str, token: str, amount: float, usd_value: float):
    return await _D1_WALLET.claim_and_credit_deposit(intent_id, user_id, amount_milli, tx_hash, token, amount, usd_value)


async def deposit_exists(tx_hash: str) -> bool:
    async with get_db() as db:
        async with db.execute(
            "SELECT 1 FROM deposits WHERE tx_hash = ?", (tx_hash,)
        ) as cursor:
            return (await cursor.fetchone()) is not None


async def get_total_deposit_usd() -> float:
    async with get_db() as db:
        async with db.execute(
            "SELECT COALESCE(SUM(usd_value), 0) FROM deposits"
        ) as cursor:
            row = await cursor.fetchone()
        return float(row[0]) if row else 0.0


async def get_total_upi_deposit_inr() -> float:
    async with get_db() as db:
        async with db.execute(
            "SELECT COALESCE(SUM(amount_inr), 0) FROM upi_requests WHERE status = 'approved'"
        ) as cursor:
            row = await cursor.fetchone()
        return float(row[0]) if row else 0.0


async def create_deposit_intent(user_id: int, base_amount: float, chain: str='bsc', usd_amount: float | None=None):
    return await _D1_WALLET.create_deposit_intent(user_id, base_amount, chain, usd_amount)


async def get_live_intent_for_user(user_id: int):
    """Return this user's single pending, non-expired reservation as
    {"id", "amount_milli", "chain", "created_at"} (created_at as ISO string), or None.
    A user only ever has one live intent (create_deposit_intent expires older ones),
    so this tells us which network to verify their pasted TxID against."""
    now_iso = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        async with db.execute(
            "SELECT id, amount_milli, chain, created_at FROM deposit_intents "
            "WHERE user_id=? AND status='pending' AND expires_at > ? "
            "ORDER BY id DESC LIMIT 1",
            (user_id, now_iso),
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    return {"id": row[0], "amount_milli": row[1], "chain": row[2], "created_at": row[3]}


async def process_referral(referrer_id: int, referred_user_id: int):
    return await _D1_WALLET.process_referral(referrer_id, referred_user_id)


async def get_referral_count(user_id: int) -> int:
    async with get_db() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else 0


# ══════════════════════════════════════════════════════════════   ══════   ═════════
#  PRICES
# ═════════════════════════════════════════════════     ═════════════════════════════

ALL_PRICE_KEYS = (
    "ETH",
    "BNB",
    "POL",
    "MATIC",
    "BTC",
    "LTC",
    "TRX",
    "TON",
    "USDT",
    "USDC",
    "DAI",
)


async def _prices_coingecko(session: aiohttp.ClientSession) -> dict[str, float]:
    ids = "ethereum,binancecoin,polygon-ecosystem-token,bitcoin,litecoin,tron,the-open-network,usd-coin,dai"
    async with session.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": ids, "vs_currencies": "usd"},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as r:
        d = await r.json(content_type=None)
    pol_price = d.get("polygon-ecosystem-token", {}).get("usd", 0)
    return {
        "ETH": d.get("ethereum", {}).get("usd", 0),
        "BNB": d.get("binancecoin", {}).get("usd", 0),
        "POL": pol_price,
        "MATIC": pol_price,
        "BTC": d.get("bitcoin", {}).get("usd", 0),
        "LTC": d.get("litecoin", {}).get("usd", 0),
        "TRX": d.get("tron", {}).get("usd", 0),
        "TON": d.get("the-open-network", {}).get("usd", 0),
        "USDT": 1.0,
        "USDC": d.get("usd-coin", {}).get("usd", 1.0),
        "DAI": d.get("dai", {}).get("usd", 1.0),
    }


async def _prices_coinbase(session: aiohttp.ClientSession) -> dict[str, float]:
    coins = {
        "BTC": "BTC",
        "ETH": "ETH",
        "BNB": "BNB",
        "LTC": "LTC",
        "TRX": "TRX",
        "TON": "TON",
        "POL": "POL",
        "USDC": "USDC",
        "DAI": "DAI",
    }
    out: dict[str, float] = {"USDT": 1.0, "USDC": 1.0, "DAI": 1.0}

    async def _fetch_one(coin_code, api_symbol):
        try:
            url = f"https://api.coinbase.com/v2/prices/{api_symbol}-USD/spot"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    d = await r.json(content_type=None)
                    val = float(d.get("data", {}).get("amount", 0))
                    if val > 0:
                        out[coin_code] = val
                        if coin_code == "POL":
                            out["MATIC"] = val
        except Exception:
            pass

    await asyncio.gather(*[_fetch_one(k, v) for k, v in coins.items()])
    return out


async def _prices_binance(session: aiohttp.ClientSession) -> dict[str, float]:
    sym_map = {
        "ETHUSDT": "ETH",
        "BNBUSDT": "BNB",
        "BTCUSDT": "BTC",
        "LTCUSDT": "LTC",
        "TRXUSDT": "TRX",
        "TONUSDT": "TON",
        "POLUSDT": "POL",
        "USDCUSDT": "USDC",
        "DAIUSDT": "DAI",
    }
    symbols = list(sym_map.keys())
    async with session.get(
        "https://api.binance.com/api/v3/ticker/price",
        params={"symbols": str(symbols).replace("'", '"').replace(" ", "")},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as r:
        data = await r.json(content_type=None)
    out: dict[str, float] = {"USDT": 1.0, "USDC": 1.0, "DAI": 1.0}
    for item in data:
        key = sym_map.get(item.get("symbol", ""))
        if key:
            val = float(item.get("price", 0))
            out[key] = val
            if key == "POL":
                out["MATIC"] = val
    return out


async def fetch_prices(session: aiohttp.ClientSession) -> dict[str, float]:
    sources = await asyncio.gather(
        _prices_coingecko(session),
        _prices_coinbase(session),
        _prices_binance(session),
        return_exceptions=True,
    )
    merged: dict[str, float] = {"USDT": 1.0}
    for res in sources:
        if isinstance(res, Exception):
            continue
        for k in ALL_PRICE_KEYS:
            if merged.get(k, 0) == 0:
                v = res.get(k, 0)
                if v and v > 0:
                    merged[k] = v
    for k in ALL_PRICE_KEYS:
        merged.setdefault(k, 0.0)
    return merged


async def ensure_prices(session: aiohttp.ClientSession):
    global PRICES, _prices_fetched_at
    if time.monotonic() - _prices_fetched_at > PRICE_REFRESH_SECONDS or not PRICES:
        PRICES = await fetch_prices(session)
        _prices_fetched_at = time.monotonic()


# ══════════  ════════════════════════════════════════════════════════   ════   ══════
#  ASYNC RPC HELPERS
# ═══════════════════════════════════════════════════════════════════════════════


async def _rpc_one(
    session: aiohttp.ClientSession,
    url: str,
    method: str,
    params: list,
    timeout: int = 8,
) -> any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with session.post(
        url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)
    ) as r:
        data = await r.json(content_type=None)
    if "result" in data:
        return data["result"]
    raise ApiError(data.get("error", "no result"))


async def rpc_race(
    session: aiohttp.ClientSession,
    chain: str,
    method: str,
    params: list,
    timeout: int = 8,
) -> any:
    tasks = [
        asyncio.create_task(_rpc_one(session, url, method, params, timeout))
        for url in RPC_ENDPOINTS[chain]
    ]
    errors = []
    for coro in asyncio.as_completed(tasks):
        try:
            result = await coro
            for t in tasks:
                t.cancel()
            return result
        except Exception as e:
            errors.append(str(e))
    raise ApiError(f"all RPCs failed for {chain}/{method}: {errors[-1]}")


# ════════════════════════════════════════════════════════════   ═       ════════════════
#  EVM HELPERS
# ═══════   ═══════════════════════════════════════════════════════════════   ═══════


def normalize_evm_address(address: str) -> str:
    addr = address.strip()
    if addr.lower().startswith("0x"):
        addr = addr[2:]
    addr = addr.lower()
    if len(addr) != 40 or any(c not in "0123456789abcdef" for c in addr):
        raise ValueError(f"'{address}' is not a valid EVM address")
    return "0x" + addr


def normalize_tx_hash(txh: str) -> str | None:
    """Return a clean 0x + 64-hex tx hash, or None if it isn't one."""
    if not txh:
        return None
    h = txh.strip().lower()
    if h.startswith("0x"):
        h = h[2:]
    if len(h) == 64 and all(c in "0123456789abcdef" for c in h):
        return "0x" + h
    return None


def normalize_tx_ref(text: str, chain: str) -> str | None:
    """Normalize a pasted on-chain reference for the given deposit network. EVM and
    Tron use a hex tx hash (→ 0x + 64-hex); Solana uses a base58 signature (kept
    as-is). Returns the canonical stored form, or None if it doesn't look valid."""
    kind = (DEPOSIT_TOKENS.get(chain) or {}).get("kind", "evm")
    if kind == "solana":
        return normalize_solana_signature(text)
    if kind in ("ton", "ltc", "btc"):
        # These coins are verified by scanning our own address rather than
        # trusting the pasted ref, and wallets show hashes in inconsistent
        # formats. Accept any plausible single-token reference.
        t = (text or "").strip()
        return t if 10 <= len(t) <= 120 and " " not in t else None
    return normalize_tx_hash(text)


class DepositError(Exception):
    """Raised when a deposit tx is found but is not a valid payment to us."""


# ═════════════════  ═══════════════════════     ══════════════════════  ══════════════
#  TRON (TRC20) HELPERS
# ══════════════════════════   ═════════  ═     ═══════════════════════════════════════

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58decode(s: str) -> bytes:
    """Minimal base58 decode (no external dep) for Tron address conversion."""
    num = 0
    for ch in s:
        idx = _B58_ALPHABET.find(ch)
        if idx < 0:
            raise ValueError(f"invalid base58 character: {ch!r}")
        num = num * 58 + idx
    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    pad = len(s) - len(s.lstrip("1"))  # leading '1's are leading zero bytes
    return b"\x00" * pad + body


def tron_base58_to_hex20(addr: str) -> str:
    """Tron base58check address (T…) → its 20-byte hex form (no 0x/41 prefix),
    which is exactly what appears inside TRC20 event topics and log addresses.
    Validates the 21-byte payload, 0x41 mainnet prefix, and 4-byte SHA256d checksum
    so a mistyped config address fails loudly instead of silently mismatching."""
    raw = _b58decode(addr.strip())
    if len(raw) != 25:
        raise ValueError(f"'{addr}' is not a valid Tron address (bad length)")
    payload, checksum = raw[:21], raw[21:]
    if payload[0] != 0x41:
        raise ValueError(f"'{addr}' is not a Tron mainnet address (bad prefix)")
    if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != checksum:
        raise ValueError(f"'{addr}' has an invalid Tron address checksum")
    return payload[1:21].hex().lower()  # drop the 0x41 network byte


async def verify_tron_usdt_deposit(session, tx_hash: str, token: dict) -> dict:
    """Verify a confirmed TRC20 USDT transfer INTO our Tron address via TronGrid.
    Returns the same shape as verify_usdt_deposit; raises DepositError otherwise."""
    dep_addr = token.get("address") or DEPOSIT_ADDRESS_TRON
    if not dep_addr:
        raise DepositError("Deposits are not configured yet. Please contact the admin.")

    txh = normalize_tx_hash(tx_hash)
    if not txh:
        raise DepositError(
            "That doesn't look like a valid transaction hash (should be 64 characters)."
        )
    txid = txh[2:]  # Tron identifies a tx by the bare 64-hex id (no 0x)

    headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY} if TRONGRID_API_KEY else {}
    try:
        async with session.post(
            f"{TRONGRID_API}/wallet/gettransactioninfobyid",
            json={"value": txid},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=12),
        ) as r:
            if r.status == 429:
                raise DepositError(
                    "The Tron network is busy right now. Please try again in a minute."
                )
            if r.status >= 500:
                raise DepositError(
                    "Could not reach the Tron network to check that transaction. Try again in a moment."
                )
            info = await r.json(content_type=None)
    except DepositError:
        raise
    except Exception:
        raise DepositError(
            "Could not reach the Tron network to check that transaction. Try again in a moment."
        )

    # gettransactioninfobyid returns {} until the tx is mined into a block.
    if not info or "id" not in info:
        raise DepositError(
            "Transaction not found yet. If you just sent it, wait ~30 seconds for it to confirm, then try again."
        )

    result = (info.get("receipt") or {}).get("result")
    if result is not None and result != "SUCCESS":
        raise DepositError("That transaction failed on-chain, so nothing was received.")

    want_contract = tron_base58_to_hex20(token["contract"])
    want_to = tron_base58_to_hex20(dep_addr)
    decimals = token["decimals"]

    for log in info.get("log", []):
        # Tron logs carry the contract + topic addresses as bare 20-byte hex (no 41).
        if log.get("address", "").lower() != want_contract:
            continue
        topics = log.get("topics", [])
        if len(topics) < 3 or topics[0].lower() != TRANSFER_TOPIC.lower():
            continue
        to_addr = topics[2][-40:].lower()
        if to_addr != want_to:
            continue
        raw = int(log.get("data", "0") or "0", 16)
        amount = raw / (10**decimals)
        from_addr = "41" + topics[1][-40:].lower()
        block_time = None
        ts = info.get("blockTimeStamp")
        if ts:
            try:
                block_time = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            except Exception:
                block_time = None
        return {
            "amount": amount,
            "from": from_addr,
            "tx_hash": txh,
            "block_time": block_time,
        }

    raise DepositError(
        f"That transaction is not a USDT payment to our deposit address on {token['label']}. "
        f"Make sure you sent USDT on {token['label']} to the exact address shown."
    )


# ════════════════════════════     ═══   ════════════════════════   ═════════════════════
#  SOLANA (SPL) HELPERS
# ═   ═════════════════════════════════════════════════════════════════════════════


def normalize_solana_signature(sig: str) -> str | None:
    """Solana tx signatures are base58 (NOT hex), typically 87-88 chars. Return the
    cleaned signature if it looks valid, else None."""
    if not sig:
        return None
    s = sig.strip()
    if not (80 <= len(s) <= 90):
        return None
    if any(c not in _B58_ALPHABET for c in s):
        return None
    try:
        if len(_b58decode(s)) != 64:  # a signature is 64 raw bytes
            return None
    except ValueError:
        return None
    return s


def solana_pubkey_valid(addr: str) -> bool:
    """A Solana address is a base58-encoded 32-byte public key."""
    try:
        return len(_b58decode(addr.strip())) == 32
    except ValueError:
        return False


async def verify_solana_usdt_deposit(session, signature: str, token: dict) -> dict:
    """Verify a confirmed SPL USDT transfer INTO our Solana address via JSON-RPC.
    Returns the same shape as verify_usdt_deposit; raises DepositError otherwise.

    Solana has no per-transfer 'to log' like EVM/Tron. Instead we compare the token
    balance of OUR address's USDT account before vs after the tx (pre/postTokenBalances)
    and credit the increase — this is robust to however the sender routed the transfer."""
    dep_addr = token.get("address") or DEPOSIT_ADDRESS_SOLANA
    if not dep_addr:
        raise DepositError("Deposits are not configured yet. Please contact the admin.")

    sig = normalize_solana_signature(signature)
    if not sig:
        raise DepositError(
            "That doesn't look like a valid Solana transaction signature."
        )

    mint = token["contract"]
    decimals = token["decimals"]
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        # "finalized" (not "confirmed"): a finalized tx can't be rolled back by a fork,
        # so we never credit a deposit that later disappears in a reorg.
        "params": [
            sig,
            {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
                "commitment": "finalized",
            },
        ],
    }
    try:
        async with session.post(
            SOLANA_RPC, json=payload, timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status == 429:
                raise DepositError(
                    "The Solana network is busy right now. Please try again in a minute."
                )
            if r.status >= 500:
                raise DepositError(
                    "Could not reach the Solana network to check that transaction. Try again in a moment."
                )
            body = await r.json(content_type=None)
    except DepositError:
        raise
    except Exception:
        raise DepositError(
            "Could not reach the Solana network to check that transaction. Try again in a moment."
        )

    result = body.get("result")
    if result is None:
        raise DepositError(
            "Transaction not found yet. If you just sent it, wait ~30 seconds for it to confirm, then try again."
        )

    meta = result.get("meta") or {}
    if meta.get("err") is not None:
        raise DepositError("That transaction failed on-chain, so nothing was received.")

    pre = meta.get("preTokenBalances") or []
    post = meta.get("postTokenBalances") or []

    def _amt(entry):
        return int((entry.get("uiTokenAmount") or {}).get("amount", "0") or "0")

    # Our USDT balance before (0 if the token account didn't exist yet), keyed by the
    # token account index so pre/post line up even if several accounts are touched.
    pre_by_idx = {
        e.get("accountIndex"): _amt(e)
        for e in pre
        if e.get("mint") == mint and e.get("owner") == dep_addr
    }
    received = 0
    for e in post:
        if e.get("mint") != mint or e.get("owner") != dep_addr:
            continue
        delta = _amt(e) - pre_by_idx.get(e.get("accountIndex"), 0)
        if delta > 0:
            received += delta

    if received <= 0:
        raise DepositError(
            f"That transaction is not a USDT payment to our deposit address on {token['label']}. "
            f"Make sure you sent USDT (SPL) on Solana to the exact address shown."
        )

    # Best-effort sender: the USDT account whose balance dropped the most.
    from_addr = ""
    biggest = 0
    post_by_idx = {
        e.get("accountIndex"): _amt(e) for e in post if e.get("mint") == mint
    }
    for e in pre:
        if e.get("mint") != mint or e.get("owner") == dep_addr:
            continue
        drop = _amt(e) - post_by_idx.get(e.get("accountIndex"), 0)
        if drop > biggest:
            biggest, from_addr = drop, e.get("owner") or ""

    block_time = None
    ts = result.get("blockTime")
    if ts:
        try:
            block_time = datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            block_time = None

    return {
        "amount": received / (10**decimals),
        "from": from_addr,
        "tx_hash": sig,
        "block_time": block_time,
    }


# ═══════════════════════════════════       ═══════════════════════════════════════════
#  TON (GRAM) HELPERS
# ════════════   ═══════════════════════════════     ═════════════════════════════════

TONCENTER_API = "https://toncenter.com/api/v2"

# CoinGecko IDs for native-coin deposits priced at the live USD rate.
_COIN_GECKO_IDS = {
    "ton": ("the-open-network", "TON"),
    "ltc": ("litecoin", "LTC"),
    "btc": ("bitcoin", "BTC"),
}

# 60-second per-coin USD price cache so deposit bursts don't hammer CoinGecko.
_COIN_PRICE_CACHE: dict = {k: {"price": 0.0, "ts": 0.0} for k in _COIN_GECKO_IDS}


async def get_coin_price_usd(coin: str, session=None) -> float:
    """Live coin/USD price from CoinGecko (public, no key), cached for 60s.
    Raises DepositError if the price can't be fetched (never guess a price)."""
    gecko_id, symbol = _COIN_GECKO_IDS[coin]
    cache = _COIN_PRICE_CACHE[coin]
    now = time.time()
    if cache["price"] > 0 and now - cache["ts"] < 60:
        return cache["price"]
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        f"?ids={gecko_id}&vs_currencies=usd"
    )
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
        price = float(data[gecko_id]["usd"])
        if price <= 0:
            raise ValueError("non-positive price")
        cache["price"] = price
        cache["ts"] = now
        return price
    except Exception:
        if cache["price"] > 0:
            return cache["price"]  # stale beats failing outright
        raise DepositError(
            f"Couldn't fetch the live {symbol} price. Please try again in a moment."
        )
    finally:
        if own_session:
            await session.close()


async def get_ton_price_usd(session=None) -> float:
    return await get_coin_price_usd("ton", session)


async def coin_price_refresh_loop():
    """Keep the per-coin USD price cache warm in the background so deposit
    taps (GRAM/LTC/BTC amounts) never block on a live CoinGecko call, which
    could take up to 10 seconds and made those screens feel very slow."""
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                for coin in _COIN_GECKO_IDS:
                    try:
                        # Expire the cache entry so the call really refreshes.
                        # On failure the stale price is still served, so user
                        # taps keep working either way.
                        _COIN_PRICE_CACHE[coin]["ts"] = 0.0
                        await get_coin_price_usd(coin, session)
                    except Exception:
                        pass  # keep stale price; user taps still work
        except Exception:
            pass
        await asyncio.sleep(50)  # refresh just inside the 60s cache TTL


async def verify_ton_deposit(
    session, token: dict, expected_milli: int, created_at_iso: str | None
) -> dict:
    """Verify a native TON transfer INTO our wallet via the public toncenter API.

    TON wallets show tx hashes in inconsistent formats, so instead of trusting a
    pasted hash we scan our own address's recent incoming transactions for one
    whose value EXACTLY equals the reserved unique amount and which happened
    after the reservation was created. Returns the same shape as the other
    verifiers; raises DepositError otherwise."""
    dep_addr = token.get("address") or DEPOSIT_ADDRESS_TON
    if not dep_addr:
        raise DepositError("TON deposits are not configured yet. Contact the admin.")

    expected_nano = expected_milli * 10**6  # milli-TON → nanotons
    not_before = None
    if created_at_iso:
        try:
            not_before = datetime.fromisoformat(created_at_iso) - timedelta(minutes=2)
        except ValueError:
            not_before = None

    try:
        async with session.get(
            f"{TONCENTER_API}/getTransactions",
            params={"address": dep_addr, "limit": 50},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            data = await r.json()
    except Exception:
        raise DepositError(
            "Could not reach the TON network to check for your payment. "
            "Try again in a moment."
        )
    if not data.get("ok"):
        raise DepositError(
            "The TON network explorer returned an error. Try again shortly."
        )

    for tx in data.get("result", []):
        in_msg = tx.get("in_msg") or {}
        try:
            value = int(in_msg.get("value") or 0)
        except (TypeError, ValueError):
            continue
        if value != expected_nano:
            continue
        block_time = None
        try:
            block_time = datetime.fromtimestamp(int(tx["utime"]), tz=timezone.utc)
        except (KeyError, TypeError, ValueError):
            pass
        if not_before and block_time and block_time < not_before:
            continue  # old coincidental tx of the same amount — not this deposit
        chain_hash = (tx.get("transaction_id") or {}).get("hash") or ""
        if not chain_hash:
            continue
        return {
            "amount": value / 1e9,
            "from": in_msg.get("source") or "",
            "tx_hash": f"ton:{chain_hash}",
            "block_time": block_time,
        }

    raise DepositError(
        "We couldn't find your TON payment yet. If you just sent it, wait "
        "~30 seconds for it to confirm and try again. Make sure you sent the "
        "EXACT amount shown to the exact address."
    )


# BlockCypher-backed UTXO coins (LTC, BTC): API path, coin name for error
# messages, and the block-time hint shown when the payment isn't found yet.
_BLOCKCYPHER_COINS = {
    "ltc": {
        "api": "https://api.blockcypher.com/v1/ltc/main",
        "name": "Litecoin",
        "symbol": "LTC",
        "wait_hint": "Litecoin blocks take ~2.5 minutes",
    },
    "btc": {
        "api": "https://api.blockcypher.com/v1/btc/main",
        "name": "Bitcoin",
        "symbol": "BTC",
        "wait_hint": "Bitcoin blocks take ~10 minutes",
    },
}


async def verify_blockcypher_deposit(
    session, coin: str, token: dict, expected_milli: int, created_at_iso: str | None
) -> dict:
    """Verify a native UTXO-coin transfer (LTC/BTC) INTO our wallet via the
    public BlockCypher API. Same strategy as TON: scan our own address's recent
    transactions for an output to us whose value EXACTLY equals the reserved
    unique amount and which happened after the reservation was created. Returns
    the same shape as the other verifiers; raises DepositError otherwise."""
    cfg = _BLOCKCYPHER_COINS[coin]
    dep_addr = token.get("address")
    if not dep_addr:
        raise DepositError(
            f"{cfg['symbol']} deposits are not configured yet. Contact the admin."
        )

    expected_sats = expected_milli  # intent units for LTC/BTC ARE satoshis/litoshis
    not_before = None
    if created_at_iso:
        try:
            not_before = datetime.fromisoformat(created_at_iso) - timedelta(minutes=2)
        except ValueError:
            not_before = None

    try:
        async with session.get(
            f"{cfg['api']}/addrs/{dep_addr}/full",
            params={"limit": 50, "txlimit": 50},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as r:
            data = await r.json()
    except Exception:
        raise DepositError(
            f"Could not reach the {cfg['name']} network to check for your "
            "payment. Try again in a moment."
        )
    if data.get("error"):
        raise DepositError(
            f"The {cfg['name']} network explorer returned an error. "
            "Try again shortly."
        )

    for tx in data.get("txs", []):
        # Sum outputs paying OUR address in this tx (usually a single output).
        paid = 0
        for out in tx.get("outputs", []):
            if dep_addr in (out.get("addresses") or []):
                try:
                    paid += int(out.get("value") or 0)
                except (TypeError, ValueError):
                    continue
        if paid != expected_sats:
            continue
        block_time = None
        ts = tx.get("confirmed") or tx.get("received")
        if ts:
            try:
                block_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                pass
        if not_before and block_time and block_time < not_before:
            continue  # old coincidental tx of the same amount — not this deposit
        chain_hash = tx.get("hash") or ""
        if not chain_hash:
            continue
        sender = ""
        for inp in tx.get("inputs", []):
            addrs = inp.get("addresses") or []
            if addrs:
                sender = addrs[0]
                break
        return {
            "amount": paid / 1e8,
            "from": sender,
            "tx_hash": f"{coin}:{chain_hash}",
            "block_time": block_time,
        }

    raise DepositError(
        f"We couldn't find your {cfg['symbol']} payment yet. "
        f"{cfg['wait_hint']} — if you just sent it, wait a couple of minutes "
        "and try again. Make sure you sent the EXACT amount shown to the "
        "exact address."
    )


async def verify_ltc_deposit(session, token, expected_milli, created_at_iso):
    return await verify_blockcypher_deposit(
        session, "ltc", token, expected_milli, created_at_iso
    )


async def verify_btc_deposit(session, token, expected_milli, created_at_iso):
    return await verify_blockcypher_deposit(
        session, "btc", token, expected_milli, created_at_iso
    )


async def verify_usdt_deposit(session, tx_hash: str, chain: str = "bsc") -> dict:
    """
    Verify that `tx_hash` is a confirmed USDT transfer INTO our deposit address on
    `chain` (bsc, polygon, tron, or solana). Returns {"amount": float, "from": str,
    "tx_hash": str, "block_time": datetime|None} on success.
    Raises DepositError with a human-readable reason otherwise.
    """
    token = DEPOSIT_TOKENS.get(chain)
    if token is None:
        raise DepositError("Unsupported deposit network. Please start a new deposit.")

    # Tron is not EVM — it has its own verifier and its own receiving address.
    if token.get("kind") == "tron":
        return await verify_tron_usdt_deposit(session, tx_hash, token)

    # Solana is not EVM/Tron — SPL token, balance-delta verification.
    if token.get("kind") == "solana":
        return await verify_solana_usdt_deposit(session, tx_hash, token)

    dep_addr = token.get("address") or DEPOSIT_ADDRESS
    if not dep_addr:
        raise DepositError("Deposits are not configured yet. Please contact the admin.")

    txh = normalize_tx_hash(tx_hash)
    if not txh:
        raise DepositError(
            "That doesn't look like a valid transaction hash (should be 0x + 64 characters)."
        )

    try:
        receipt = await rpc_race(
            session, token["chain"], "eth_getTransactionReceipt", [txh]
        )
    except Exception:
        raise DepositError(
            "Could not reach the network to check that transaction. Try again in a moment."
        )

    if receipt is None:
        raise DepositError(
            "Transaction not found yet. If you just sent it, wait ~30 seconds for it to confirm, then try again."
        )

    if str(receipt.get("status", "0x1")).lower() not in ("0x1", "1"):
        raise DepositError("That transaction failed on-chain, so nothing was received.")

    want_contract = token["contract"].lower()
    want_to = normalize_evm_address(dep_addr)[2:].lower()
    decimals = token["decimals"]

    for log in receipt.get("logs", []):
        if log.get("address", "").lower() != want_contract:
            continue
        topics = log.get("topics", [])
        if len(topics) < 3 or topics[0].lower() != TRANSFER_TOPIC.lower():
            continue
        to_addr = topics[2][-40:].lower()  # last 20 bytes of the 32-byte topic
        if to_addr != want_to:
            continue
        raw = int(log.get("data", "0x0"), 16)
        amount = raw / (10**decimals)
        from_addr = "0x" + topics[1][-40:].lower()
        block_time = None
        try:
            block_num = int(receipt["blockNumber"], 16)
            block_time = await get_block_timestamp(session, token["chain"], block_num)
        except Exception:
            block_time = None  # non-fatal; caller decides how strict to be
        return {
            "amount": amount,
            "from": from_addr,
            "tx_hash": txh,
            "block_time": block_time,
        }

    raise DepositError(
        f"That transaction is not a USDT payment to our deposit address on {token['label']}. "
        f"Make sure you sent USDT on {token['label']} to the exact address shown."
    )


async def get_native_balance(session, address, chain) -> float:
    res = await rpc_race(session, chain, "eth_getBalance", [address, "latest"])
    return int(res, 16) / 1e18


async def get_erc20_balance(
    session, address, chain, contract_address, decimals
) -> float:
    padded = address[2:].rjust(64, "0")
    data_field = "0x70a08231" + padded
    res = await rpc_race(
        session,
        chain,
        "eth_call",
        [{"to": contract_address, "data": data_field}, "latest"],
    )
    if not res or res == "0x":
        return 0.0
    return int(res, 16) / (10**decimals)


async def get_block_timestamp(session, chain, block_number) -> datetime:
    res = await rpc_race(
        session, chain, "eth_getBlockByNumber", [hex(block_number), False], timeout=5
    )
    return datetime.fromtimestamp(int(res["timestamp"], 16), tz=timezone.utc)


async def get_recent_token_transfers(
    session, address, chain, contract_address, decimals, limit=5
):
    padded_topic = "0x" + address[2:].rjust(64, "0")
    latest_hex = await rpc_race(session, chain, "eth_blockNumber", [])
    latest = int(latest_hex, 16)

    logs = None
    used_range = None
    for rng in (5000, 2000, 500):
        from_block = max(0, latest - rng)
        try:
            logs = await rpc_race(
                session,
                chain,
                "eth_getLogs",
                [
                    {
                        "address": contract_address,
                        "fromBlock": hex(from_block),
                        "toBlock": "latest",
                        "topics": [TRANSFER_TOPIC, None, padded_topic],
                    }
                ],
            )
            used_range = rng
            break
        except ApiError:
            continue

    if logs is None:
        raise ApiError("eth_getLogs unavailable")

    transfers = []
    unique_blocks = {int(log["blockNumber"], 16) for log in logs[-limit:]}

    async def _ts(bn):
        try:
            return bn, await get_block_timestamp(session, chain, bn)
        except ApiError:
            return bn, None

    ts_results = dict(await asyncio.gather(*[_ts(bn) for bn in unique_blocks]))

    for log in reversed(logs):
        amount = int(log["data"], 16) / (10**decimals)
        from_addr = "0x" + log["topics"][1][-40:]
        block_num = int(log["blockNumber"], 16)
        transfers.append(
            {
                "amount": amount,
                "from": from_addr,
                "hash": log["transactionHash"],
                "time": ts_results.get(block_num),
            }
        )
        if len(transfers) >= limit:
            break

    window_hours = (used_range * CHAIN_INFO[chain]["block_time"]) / 3600
    return transfers, window_hours


# ════════════════════   ════════════════════════   ═════════════════════════════════
#  PER-CHAIN EVM
# ═══════════════════════════════════════════════════════════════════════════════


async def check_evm_chain(session, addr, chain) -> dict:
    info = CHAIN_INFO[chain]
    result = {
        "chain": chain,
        "name": info["name"],
        "native_sym": info["native"],
        "native": 0.0,
        "tokens": [],
        "errors": [],
    }

    native_task = asyncio.create_task(get_native_balance(session, addr, chain))
    tokens_to_check = EVM_TOKENS[chain]
    token_tasks = [
        asyncio.create_task(
            get_erc20_balance(
                session, addr, chain, token["contract"], token["decimals"]
            )
        )
        for token in tokens_to_check
    ]

    await asyncio.gather(native_task, *token_tasks, return_exceptions=True)

    try:
        result["native"] = native_task.result()
    except Exception as e:
        result["errors"].append(f"{info['native']}: {e}")

    positive_balance_tokens = []
    for token, task in zip(tokens_to_check, token_tasks):
        try:
            bal = task.result()
            if bal > 0.000001:
                positive_balance_tokens.append((token, bal))
        except Exception as e:
            result["errors"].append(f"{token['symbol']}: {e}")

    if positive_balance_tokens:
        transfer_tasks = [
            get_recent_token_transfers(
                session, addr, chain, token["contract"], token["decimals"]
            )
            for token, _ in positive_balance_tokens
        ]
        transfer_results = await asyncio.gather(*transfer_tasks, return_exceptions=True)

        for (token, bal), tx_res in zip(positive_balance_tokens, transfer_results):
            token_entry = {
                "symbol": token["symbol"],
                "balance": bal,
                "price_key": token["price_key"],
                "transfers": [],
                "window_hours": 0.0,
            }
            # Transfer history is optional (needs eth_getLogs, which free public
            # nodes often block). Failing to fetch it must NOT pollute the balance
            # output or trigger the "totals may be incomplete" warning.
            if not isinstance(tx_res, Exception):
                token_entry["transfers"], token_entry["window_hours"] = tx_res
            result["tokens"].append(token_entry)

    return result


# ══════════════════════════════   ════════════════════════════════════════════════
#  FORMAT FUNCTIONS
# ════════════     ═     ═════════════   ══════════════════════════════════════════════════


async def format_evm_wallet(session, raw_address):
    try:
        addr = normalize_evm_address(raw_address)
    except ValueError as e:
        return [f"Skipping '{raw_address}': {e}"], 0.0

    lines = [
        f"Address : {addr}",
        f"Type    : EVM (ETH / BSC / Polygon)",
        f"Checked : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
    ]

    chain_results = await asyncio.gather(
        check_evm_chain(session, addr, "eth"),
        check_evm_chain(session, addr, "bsc"),
        check_evm_chain(session, addr, "polygon"),
    )

    native_key = {"eth": "ETH", "bsc": "BNB", "polygon": "POL"}
    grand_totals = {}
    grand_usd = 0.0
    any_error = False

    for r in chain_results:
        chain = r["chain"]
        np = PRICES.get(native_key[chain], 0.0)
        native_usd = r["native"] * np
        chain_usd = native_usd

        grand_totals[native_key[chain]] = (
            grand_totals.get(native_key[chain], 0.0) + r["native"]
        )

        lines.append("")
        lines.append(f"[{r['name']}]")
        lines.append(
            f"  {r['native_sym']:<10}: {r['native']:.8f}  (~${native_usd:.2f})"
        )

        for t in r["tokens"]:
            tp = PRICES.get(t["price_key"], 0.0)
            t_usd = t["balance"] * tp
            chain_usd += t_usd
            grand_totals[t["symbol"]] = (
                grand_totals.get(t["symbol"], 0.0) + t["balance"]
            )
            lines.append(f"  {t['symbol']:<10}: {t['balance']:.6f}  (~${t_usd:.2f})")

        lines.append(f"  Chain USD : ${chain_usd:.2f}")

        for t in r["tokens"]:
            if t["transfers"]:
                lines.append(
                    f"  Recent incoming {t['symbol']} (last ~{t['window_hours']:.1f}h):"
                )
                for tx in t["transfers"]:
                    when = (
                        tx["time"].strftime("%Y-%m-%d %H:%M UTC") if tx["time"] else "?"
                    )
                    lines.append(
                        f"    +{tx['amount']:.6f} {t['symbol']} from {tx['from']} {when} tx:{tx['hash'][:12]}..."
                    )

        for err in r["errors"]:
            lines.append(f"  ERROR: {err}")
            any_error = True

        grand_usd += chain_usd

    lines.append("")
    lines.append("─" * 38)
    if any_error:
        lines.append("WARNING: some chains had errors — totals may be incomplete.")

    for sym, tot in sorted(grand_totals.items()):
        if tot > 0.000001:
            lines.append(f"TOTAL {sym:<7} : {tot:.6f}")

    lines.append(f"TOTAL USD  : ${grand_usd:.2f}")
    lines.append("─" * 38)
    lines.append("✓ FUNDS FOUND" if grand_usd > 0.01 else "No funds detected")

    return lines, grand_usd


async def format_btc_wallet(session, address):
    lines = [
        f"Address : {address}",
        f"Type    : BTC",
        f"Checked : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]
    balance = None
    balance_usd = 0.0

    async def _bal():
        async with session.get(
            f"https://blockstream.info/api/address/{address}",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            d = await r.json(content_type=None)
        s = d.get("chain_stats", {})
        return (s.get("funded_txo_sum", 0) - s.get("spent_txo_sum", 0)) / 1e8

    async def _txs():
        async with session.get(
            f"https://blockstream.info/api/address/{address}/txs",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            return await r.json(content_type=None)

    bal_task, txs_task = await asyncio.gather(_bal(), _txs(), return_exceptions=True)

    if isinstance(bal_task, Exception):
        lines.append(f"BTC: ERROR ({bal_task})")
    else:
        balance = bal_task
        balance_usd = balance * PRICES.get("BTC", 0)
        lines.append(f"BTC: {balance:.8f}  (~${balance_usd:.2f})")

    if isinstance(txs_task, Exception):
        lines.append(f"Recent txs: ERROR ({txs_task})")
    else:
        txs = txs_task
        if isinstance(txs, list) and txs:
            lines.append("")
            lines.append("Recent transactions:")
            for tx in txs[:5]:
                txid = tx.get("txid", "")
                block_time = tx.get("status", {}).get("block_time")
                when = (
                    datetime.fromtimestamp(block_time, tz=timezone.utc).strftime(
                        "%Y-%m-%d %H:%M UTC"
                    )
                    if block_time
                    else "unconfirmed"
                )
                received = (
                    sum(
                        o["value"]
                        for o in tx.get("vout", [])
                        if o.get("scriptpubkey_address") == address
                    )
                    / 1e8
                )
                lines.append(f"  tx:{txid[:12]}... +{received:.8f} BTC  {when}")
        else:
            lines.append("No recent transactions")

    lines.append("")
    lines.append("─" * 38)
    lines.append(f"TOTAL BTC : {balance or 0:.8f}")
    lines.append(f"TOTAL USD : ${balance_usd:.2f}")
    lines.append("─" * 38)
    lines.append("✓ FUNDS FOUND" if balance and balance > 0 else "No BTC balance")

    return lines, balance_usd


async def format_ltc_wallet(session, address):
    lines = [
        f"Address : {address}",
        f"Type    : LTC",
        f"Checked : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]
    balance = None
    balance_usd = 0.0

    async def _bal():
        async with session.get(
            f"https://api.blockcypher.com/v1/ltc/main/addrs/{address}/balance",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            return (await r.json(content_type=None)).get("balance", 0) / 1e8

    async def _txs():
        async with session.get(
            f"https://api.blockcypher.com/v1/ltc/main/addrs/{address}",
            params={"limit": 5},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            d = await r.json(content_type=None)
        return (d.get("txrefs") or []) + (d.get("unconfirmed_txrefs") or [])

    bal_task, txs_task = await asyncio.gather(_bal(), _txs(), return_exceptions=True)

    if isinstance(bal_task, Exception):
        lines.append(f"LTC: ERROR ({bal_task})")
    else:
        balance = bal_task
        balance_usd = balance * PRICES.get("LTC", 0)
        lines.append(f"LTC: {balance:.8f}  (~${balance_usd:.2f})")

    if isinstance(txs_task, Exception):
        lines.append(f"Recent txs: ERROR ({txs_task})")
    else:
        txrefs = txs_task
        if txrefs:
            lines.append("")
            lines.append("Recent transactions:")
            for tx in txrefs[:5]:
                value = tx.get("value", 0) / 1e8
                direction = "received" if tx.get("tx_input_n", -1) == -1 else "sent"
                confirmed = tx.get("confirmed", "unconfirmed")
                lines.append(
                    f"  tx:{tx.get('tx_hash', '')[:12]}... {direction} {value:.8f} LTC  {confirmed}"
                )
        else:
            lines.append("No recent transactions")

    lines.append("")
    lines.append("─" * 38)
    lines.append(f"TOTAL LTC : {balance or 0:.8f}")
    lines.append(f"TOTAL USD : ${balance_usd:.2f}")
    lines.append("─" * 38)
    lines.append("✓ FUNDS FOUND" if balance and balance > 0 else "No LTC balance")

    return lines, balance_usd


async def format_trx_wallet(session, address):
    lines = [
        f"Address : {address}",
        f"Type    : TRX / Tron",
        f"Checked : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]
    trx_bal = 0.0
    usdt = 0.0
    usdc = 0.0

    async def _account():
        async with session.get(
            f"https://api.trongrid.io/v1/accounts/{address}",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            return await r.json(content_type=None)

    async def _txs(contract_address):
        async with session.get(
            f"https://api.trongrid.io/v1/accounts/{address}/transactions/trc20",
            params={
                "contract_address": contract_address,
                "limit": 5,
                "only_to": "true",
            },
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            return (await r.json(content_type=None)).get("data", [])

    acc_task = asyncio.create_task(_account())
    await asyncio.gather(acc_task, return_exceptions=True)

    try:
        d = acc_task.result()
        accounts = d.get("data", [])
        if accounts:
            acc = accounts[0]
            trx_bal = acc.get("balance", 0) / 1e6
            for token in acc.get("trc20", []):
                if TRX_USDT_CONTRACT in token:
                    usdt = int(token[TRX_USDT_CONTRACT]) / 1e6
                if TRX_USDC_CONTRACT in token:
                    usdc = int(token[TRX_USDC_CONTRACT]) / 1e6
    except Exception as e:
        lines.append(f"ERROR: {e}")

    trx_usd = trx_bal * PRICES.get("TRX", 0.0)
    usdt_usd = usdt * PRICES.get("USDT", 1.0)
    usdc_usd = usdc * PRICES.get("USDC", 1.0)
    total_usd = trx_usd + usdt_usd + usdc_usd

    lines.append(f"TRX        : {trx_bal:.6f}  (~${trx_usd:.2f})")
    if usdt > 0:
        lines.append(f"USDT(TRC20): {usdt:.6f}  (~${usdt_usd:.2f})")
    if usdc > 0:
        lines.append(f"USDC(TRC20): {usdc:.6f}  (~${usdc_usd:.2f})")

    tx_tasks = {}
    if usdt > 0:
        tx_tasks["USDT"] = asyncio.create_task(_txs(TRX_USDT_CONTRACT))
    if usdc > 0:
        tx_tasks["USDC"] = asyncio.create_task(_txs(TRX_USDC_CONTRACT))

    if tx_tasks:
        await asyncio.gather(*tx_tasks.values(), return_exceptions=True)
        for name, task in tx_tasks.items():
            try:
                txs = task.result()
                if txs:
                    lines.append("")
                    lines.append(f"Recent incoming {name}:")
                    for tx in txs:
                        decimals = int(tx.get("token_info", {}).get("decimals", 6))
                        amount = int(tx["value"]) / (10**decimals)
                        ts = datetime.fromtimestamp(
                            tx["block_timestamp"] / 1000, tz=timezone.utc
                        )
                        lines.append(
                            f"  +{amount:.6f} {name} from {tx['from']} "
                            f"{ts.strftime('%Y-%m-%d %H:%M UTC')} tx:{tx['transaction_id'][:12]}..."
                        )
                else:
                    lines.append(f"No recent incoming {name}")
            except Exception as e:
                lines.append(f"Recent {name} txs error: {e}")

    lines.append("")
    lines.append("─" * 38)
    lines.append(f"TOTAL TRX  : {trx_bal:.6f}")
    if usdt > 0:
        lines.append(f"TOTAL USDT : {usdt:.6f}")
    if usdc > 0:
        lines.append(f"TOTAL USDC : {usdc:.6f}")
    lines.append(f"TOTAL USD  : ${total_usd:.2f}")
    lines.append("─" * 38)
    lines.append("✓ FUNDS FOUND" if total_usd > 0.01 else "No funds detected")

    return lines, total_usd


async def format_ton_wallet(session, address):
    try:
        raw_addr = normalize_ton_address(address)
    except Exception as e:
        return [f"Skipping '{address}': {e}"], 0.0

    lines = [
        f"Address : {address}",
        f"Type    : TON",
        f"Checked : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]
    balance = None
    balance_usd = 0.0
    jetton_usd = 0.0
    jetton_lines = []

    async def _bal():
        async with session.get(
            "https://toncenter.com/api/v2/getAddressBalance",
            params={"address": raw_addr},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            return int((await r.json(content_type=None)).get("result", 0)) / 1e9

    async def _txs():
        async with session.get(
            "https://toncenter.com/api/v2/getTransactions",
            params={"address": raw_addr, "limit": 5},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            return (await r.json(content_type=None)).get("result", [])

    async def _jettons():
        async with session.get(
            f"https://tonapi.io/v2/accounts/{raw_addr}/jettons",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status == 200:
                return await r.json(content_type=None)
            return None

    bal_task, txs_task, jettons_task = await asyncio.gather(
        _bal(), _txs(), _jettons(), return_exceptions=True
    )

    if isinstance(bal_task, Exception):
        lines.append(f"TON: ERROR ({bal_task})")
    else:
        balance = bal_task
        balance_usd = balance * PRICES.get("TON", 0.0)
        lines.append(f"TON        : {balance:.9f}  (~${balance_usd:.2f})")

    balances = []
    if not isinstance(jettons_task, Exception) and jettons_task:
        balances = jettons_task.get("balances", [])
        for item in balances:
            bal_raw = int(item.get("balance", "0"))
            if bal_raw > 0:
                jetton_meta = item.get("jetton", {})
                symbol = jetton_meta.get("symbol", "UNKNOWN").upper()
                decimals = int(jetton_meta.get("decimals", 9))
                amount = bal_raw / (10**decimals)

                price = 0.0
                if symbol in ("USDT", "USD", "USDC"):
                    price = 1.0
                else:
                    price = PRICES.get(symbol, 0.0)
                    if price == 0.0:
                        price = float(
                            item.get("price", {}).get("prices", {}).get("USD", 0.0)
                        )

                usd_val = amount * price
                jetton_usd += usd_val

                price_str = f"  (~${usd_val:.2f})" if price > 0 else ""
                jetton_lines.append(f"  {symbol:<10}: {amount:.6f}{price_str}")

    if jetton_lines:
        lines.extend(jetton_lines)

    if isinstance(txs_task, Exception):
        lines.append(f"Recent txs: ERROR ({txs_task})")
    else:
        shown = []
        for tx in txs_task:
            in_msg = tx.get("in_msg", {}) or {}
            value = int(in_msg.get("value", 0) or 0) / 1e9
            src = in_msg.get("source", "") or "?"
            utime = tx.get("utime")
            when = (
                datetime.fromtimestamp(utime, tz=timezone.utc).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
                if utime
                else "?"
            )
            if value > 0:
                shown.append(f"  +{value:.9f} TON from {src}  {when}")
        if shown:
            lines.append("")
            lines.append("Recent transactions:")
            lines.extend(shown)
        else:
            lines.append("No recent incoming transactions")

    total_usd = balance_usd + jetton_usd
    lines.append("")
    lines.append("─" * 38)
    lines.append(f"TOTAL TON  : {balance or 0:.9f}")
    if balances:
        for item in balances:
            bal_raw = int(item.get("balance", "0"))
            if bal_raw > 0:
                symbol = item.get("jetton", {}).get("symbol", "UNKNOWN").upper()
                decimals = int(item.get("jetton", {}).get("decimals", 9))
                amount = bal_raw / (10**decimals)
                lines.append(f"TOTAL {symbol:<5}: {amount:.6f}")

    lines.append(f"TOTAL USD  : ${total_usd:.2f}")
    lines.append("─" * 38)
    lines.append("✓ FUNDS FOUND" if total_usd > 0.01 else "No TON balance")

    return lines, balance_usd


def friendly_to_raw(addr_str: str) -> str:
    normalized = addr_str.replace("-", "+").replace("_", "/")
    padding = "=" * ((4 - len(normalized) % 4) % 4)
    try:
        decoded = base64.b64decode(normalized + padding)
    except Exception:
        raise ValueError("Invalid friendly address encoding")

    if len(decoded) != 36:
        raise ValueError("Invalid friendly address length")

    data = decoded[:34]
    crc_expected = struct.unpack(">H", decoded[34:])[0]

    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc = crc << 1
            crc &= 0xFFFF

    if crc != crc_expected:
        raise ValueError("CRC checksum mismatch")

    workchain = decoded[1]
    workchain_id = workchain if workchain < 128 else workchain - 256
    account_id = decoded[2:34].hex()
    return f"{workchain_id}:{account_id}"


def normalize_ton_address(address: str) -> str:
    addr = address.strip()
    if re.match(r"^-?\d+:[0-9a-fA-F]{64}$", addr):
        return addr
    return friendly_to_raw(addr)


# ════════════════════════════════════════════════════   ════════════════════   ═════
#  ROUTER
# ═════════════════════════════════════════════════  ═══  ═════════════════════════


def detect_address_type(address: str) -> str:
    addr = address.strip()
    if re.match(r"^0x[0-9a-fA-F]{40}$", addr):
        return "evm"
    if re.match(r"^T[1-9A-HJ-NP-Za-km-z]{33}$", addr):
        return "trx"
    if re.match(r"^[A-Za-z0-9_-]{48}$", addr) or re.match(
        r"^-?\d+:[0-9a-fA-F]{64}$", addr
    ):
        return "ton"
    if addr.lower().startswith("bc1") or re.match(
        r"^[13][a-zA-HJ-NP-Z0-9]{25,34}$", addr
    ):
        return "btc"
    if addr.lower().startswith("ltc1") or re.match(
        r"^[LM][a-zA-HJ-NP-Z0-9]{25,34}$", addr
    ):
        return "ltc"
    return "unknown"


async def format_wallet(session, raw_address) -> tuple[list[str], float] | None:
    addr = raw_address.strip()
    addr_type = detect_address_type(addr)
    if addr_type == "evm":
        return await format_evm_wallet(session, addr)
    if addr_type == "trx":
        return await format_trx_wallet(session, addr)
    if addr_type == "ton":
        return await format_ton_wallet(session, addr)
    if addr_type == "btc":
        return await format_btc_wallet(session, addr)
    if addr_type == "ltc":
        return await format_ltc_wallet(session, addr)
    return None


def find_addresses(text: str) -> list[str]:
    found = []
    for token in re.split(r"\s+", text):
        cleaned = token.strip(".,!?;:()[]{}'\"<>")
        if (
            cleaned
            and detect_address_type(cleaned) != "unknown"
            and cleaned not in found
        ):
            found.append(cleaned)
    return found


def chunk_message(text: str, limit: int = 4000) -> list[str]:
    chunks = []
    while len(text) > limit:
        idx = text.rfind("\n", 0, limit)
        if idx == -1:
            idx = limit
        chunks.append(text[:idx])
        text = text[idx:].lstrip("\n")
    chunks.append(text)
    return chunks


# ═════════════   ═════════════════════  ═══════════════════════════════════════════
#  UI HELPERS
# ══════════════════   ════════════════════════════════════════════════════════════


def get_referral_link(user_id: int) -> str:
    return f"https://t.me/{BOT_USERNAME}?start={user_id}"


def build_start_message(user_id: int, credits: int) -> str:
    return (
        "Wallet Balance Checker\n\n"
        f"Credits Remaining: {credits}\n\n"
        "Supported Wallets:\n\n"
        "✓ Ethereum\n"
        "✓ BNB Smart Chain\n"
        "✓ Polygon\n"
        "✓ Bitcoin\n"
        "✓ Litecoin\n"
        "✓ Tron\n"
        "✓ TON\n\n"
        "Just paste any personal wallet address and the bot will check it instantly.\n\n"
        "Do not use exchange deposit addresses.\n"
        "Binance, Bybit, OKX, Bitget, KuCoin, Gate.io, MEXC and BingX addresses will not return correct balances.\n\n"
        "1 Credit = 1 Wallet Check\n\n"
        "/balance   — your credit balance\n"
        "/refer     — your referral link\n"
        "/help      — supported wallets and tips\n"
        "/commands  — all available commands"
    )


def build_no_credits_message(user_id: int) -> str:
    ref_link = get_referral_link(user_id)
    return (
        "No credits remaining.\n\n"
        "Top up instantly by depositing USDT (BSC / Polygon / Tron) — tap 💵 Deposit in the menu,\n"
        "or invite friends to earn free credits (+3 per person who joins your link).\n\n"
        f"{ref_link}\n\n"
        "/menu to open the store."
    )


# ── Store UI ───────────────────────────────────────────────────────────────────

# Users we are currently waiting on to paste a deposit transaction hash.
AWAITING_TX: set[int] = set()
AWAITING_WALLET: set[int] = set()
# Users we are waiting on to type how much they want to deposit.
AWAITING_AMOUNT: set[int] = set()
# The amount a user said they intend to deposit (shown back to them).
PENDING_AMOUNT: dict[int, float] = {}
# The network a user picked before typing their amount (bsc / polygon).
DEPOSIT_CHAIN_CHOICE: dict[int, str] = {}
# Direct "tap and pay" product purchases: user_id → {"pid", "intent_id"}.
# When the matching deposit intent is credited, the product is bought and
# delivered automatically in the same step.
DIRECT_BUY_PENDING: dict[int, dict] = {}
# UPI deposit flow state.
# Users we are waiting on to type how many ₹ they want to add via UPI.
AWAITING_UPI_AMOUNT: set[int] = set()
# Users we are waiting on to paste their UPI reference (UTR).
AWAITING_UPI_UTR: set[int] = set()
# The upi_requests.id a user is currently working on (awaiting_utr row).
PENDING_UPI_REQ: dict[int, int] = {}
# Manual-fulfillment shop flow: users we're waiting on to type their invite email.
AWAITING_EMAIL: set[int] = set()
# The orders.id (awaiting_email row) a user is currently entering an email for.
PENDING_EMAIL_ORDER: dict[int, int] = {}
# Custom purchase quantity: users we're waiting on to type how many items they
# want. user_id -> {"pid": product_id, "cur": "usd"|"inr"}.
AWAITING_QTY: dict[int, dict] = {}
# Admin multi-step input flows. admin_id -> {"action", "step", "data": {...}}.
# Only consulted for user ids in ADMIN_IDS; drives handle_admin_input().
ADMIN_STATE: dict[int, dict] = {}


def clear_admin_state(user_id: int) -> None:
    ADMIN_STATE.pop(user_id, None)


def clear_deposit_state(user_id: int) -> None:
    """Clear all in-memory deposit-flow flags for a user (crypto + UPI), so a
    half-finished flow can't mis-route their next message."""
    AWAITING_TX.discard(user_id)
    AWAITING_WALLET.discard(user_id)
    AWAITING_AMOUNT.discard(user_id)
    PENDING_AMOUNT.pop(user_id, None)
    DEPOSIT_CHAIN_CHOICE.pop(user_id, None)
    DIRECT_BUY_PENDING.pop(user_id, None)
    AWAITING_UPI_AMOUNT.discard(user_id)
    AWAITING_UPI_UTR.discard(user_id)
    PENDING_UPI_REQ.pop(user_id, None)
    AWAITING_EMAIL.discard(user_id)
    PENDING_EMAIL_ORDER.pop(user_id, None)
    AWAITING_QTY.pop(user_id, None)
    SUPPLIER_PANEL.activation_inputs.pop(user_id, None)


# Maps an admin address-editor choice to (setting key, validator kind, label).
ADDR_KINDS = {
    "evm": ("addr_evm", "evm", "EVM (BSC + Polygon, 0x…)"),
    "tron": ("addr_tron", "tron", "Tron / TRC20 (T…)"),
    "solana": ("addr_solana", "solana", "Solana / SPL (base58)"),
    "ton": ("addr_ton", "ton", "GRAM / TON (UQ…/EQ…)"),
    "ltc": ("addr_ltc", "ltc", "Litecoin (ltc1…/L…/M…)"),
    "btc": ("addr_btc", "btc", "Bitcoin (bc1…/1…/3…)"),
}


# Maintenance mode: while True, only admins can use the bot. Everyone else
# gets a maintenance notice. Toggled from the admin panel; persisted in
# settings as "maintenance" so it survives restarts.
MAINTENANCE = False


async def load_maintenance_state():
    global MAINTENANCE
    MAINTENANCE = (await get_setting("maintenance")) == "1"


async def set_maintenance_state(on: bool):
    global MAINTENANCE
    MAINTENANCE = on
    await set_setting("maintenance", "1" if on else "0")


def maintenance_message() -> str:
    return (
        "🚧 **Bot Under Maintenance**\n"
        "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
        "We're making some improvements right now.\n\n"
        "Please check back in a little while — your balance and "
        "orders are safe.\n\n"
        f"Questions? Contact {ADMIN_CONTACT}"
    )




# ── Broadcast engine ─────────────────────────────────────────────────────────────
# Fast, concurrent admin broadcasts with live progress, mid-flight cancel, and
# full post-hoc deletion (every delivered copy is tracked in broadcast_deliveries
# so a broadcast can be wiped from ALL user chats with one tap).

BROADCAST_CONCURRENCY = 25      # parallel sends per wave (~25 msg/s, under TG cap)
BROADCAST_EDIT_INTERVAL = 1.2   # min seconds between live progress-message edits

# broadcast_id -> {"cancel": bool}  (also "del:<id>" keys for delete jobs)
BROADCAST_JOBS: dict = {}
# admin_id -> the Telethon Message object awaiting Confirm & Send
PENDING_BROADCASTS: dict[int, object] = {}


def bc_progress_bar(done: int, total: int, width: int = 12) -> str:
    filled = int(width * done / total) if total else width
    return "▰" * filled + "▱" * (width - filled)


def bc_preview_of(message) -> str:
    """Short text label used to identify a broadcast in history lists."""
    text = (getattr(message, "raw_text", None) or "").strip()
    if not text and getattr(message, "media", None):
        text = "📎 media message"
    text = " ".join(text.split())
    return (text[:60] + "…") if len(text) > 60 else (text or "(empty)")


async def bc_create(admin_id: int, preview: str, total: int) -> int:
    async with get_db() as db:
        cur = await db.execute(
            "INSERT INTO broadcasts (admin_id, preview, status, total, created_at) "
            "VALUES (?, ?, 'sending', ?, ?)",
            (admin_id, preview, total, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        return cur.lastrowid


async def bc_finish(bcast_id: int, status: str, sent: int, failed: int):
    async with get_db() as db:
        await db.execute(
            "UPDATE broadcasts SET status = ?, sent = ?, failed = ?, finished_at = ? "
            "WHERE id = ?",
            (status, sent, failed, datetime.now(timezone.utc).isoformat(), bcast_id),
        )
        await db.commit()


async def bc_record_deliveries(bcast_id: int, rows: list[tuple[int, int]]):
    """rows: [(user_id, message_id), ...] — bulk-saved so Delete Broadcast works."""
    if not rows:
        return
    async with get_db() as db:
        await db.executemany(
            "INSERT OR IGNORE INTO broadcast_deliveries "
            "(broadcast_id, user_id, message_id) VALUES (?, ?, ?)",
            [(bcast_id, uid, mid) for uid, mid in rows],
        )
        await db.commit()


async def bc_get(bcast_id: int):
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM broadcasts WHERE id = ?", (bcast_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


async def bc_list(limit: int = 8) -> list[dict]:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM broadcasts ORDER BY id DESC LIMIT ?", (limit,)
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def bc_deliveries(bcast_id: int) -> list[tuple[int, int]]:
    async with get_db() as db:
        async with db.execute(
            "SELECT user_id, message_id FROM broadcast_deliveries "
            "WHERE broadcast_id = ?",
            (bcast_id,),
        ) as cursor:
            return [(r[0], r[1]) for r in await cursor.fetchall()]


async def bc_clear_deliveries(bcast_id: int):
    async with get_db() as db:
        await db.execute(
            "DELETE FROM broadcast_deliveries WHERE broadcast_id = ?", (bcast_id,)
        )
        await db.commit()


def _bc_is_blocked_err(e: Exception) -> bool:
    return type(e).__name__ in (
        "UserIsBlockedError",
        "InputUserDeactivatedError",
        "UserDeactivatedBanError",
        "PeerIdInvalidError",
    )


def bc_live_text(
    bcast_id: int, done: int, total: int, sent: int,
    blocked: int, failed: int, rate: float,
) -> str:
    pct = int(done * 100 / total) if total else 100
    eta = int((total - done) / rate) if rate > 0 else 0
    eta_line = f"⏳ ~{eta}s left" if total > done else "⏳ finishing…"
    return (
        f"📢  Broadcasting  #{bcast_id}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{bc_progress_bar(done, total)}  {pct}%\n\n"
        f"✅ Sent:      {sent}\n"
        f"🚫 Blocked:   {blocked}\n"
        f"⚠️ Failed:    {failed}\n"
        f"👥 Total:     {total}\n\n"
        f"⚡ {rate:.1f} msg/s  ·  {eta_line}"
    )


async def run_broadcast(
    bcast_id: int, payload, status_chat_id: int, status_msg_id: int
):
    """Send `payload` (a Telethon Message → rich re-send, or a plain str) to every
    user, concurrently, with live progress + cancel + delivery tracking."""
    BROADCAST_JOBS[bcast_id] = {"cancel": False}
    user_ids = await get_all_user_ids()
    total = len(user_ids)
    sent = blocked = failed = 0
    deliveries: list[tuple[int, int]] = []
    started = time.monotonic()
    last_edit = 0.0

    async def edit_status(text: str, buttons):
        nonlocal last_edit
        try:
            await client.edit_message(
                status_chat_id, status_msg_id, text, buttons=buttons
            )
            last_edit = time.monotonic()
        except errors.MessageNotModifiedError:
            last_edit = time.monotonic()
        except Exception:
            pass

    async def send_one(uid: int):
        nonlocal sent, blocked, failed
        try:
            msg = await client.send_message(uid, payload)
            deliveries.append((uid, msg.id))
            sent += 1
        except errors.FloodWaitError as e:
            await asyncio.sleep(min(e.seconds, 60) + 1)
            try:
                msg = await client.send_message(uid, payload)
                deliveries.append((uid, msg.id))
                sent += 1
            except Exception:
                failed += 1
        except Exception as e:
            if _bc_is_blocked_err(e):
                blocked += 1
            else:
                failed += 1

    cancelled = False
    for i in range(0, total, BROADCAST_CONCURRENCY):
        if BROADCAST_JOBS.get(bcast_id, {}).get("cancel"):
            cancelled = True
            break
        wave = user_ids[i : i + BROADCAST_CONCURRENCY]
        await asyncio.gather(*(send_one(uid) for uid in wave))
        # Persist this wave's deliveries so Delete works even after a crash.
        await bc_record_deliveries(bcast_id, deliveries)
        deliveries.clear()
        done = min(i + BROADCAST_CONCURRENCY, total)
        elapsed = max(time.monotonic() - started, 0.001)
        if time.monotonic() - last_edit >= BROADCAST_EDIT_INTERVAL:
            await edit_status(
                bc_live_text(
                    bcast_id, done, total, sent, blocked, failed, done / elapsed
                ),
                [[Button.inline("⛔ Cancel Broadcast", f"adm_bc_stop:{bcast_id}".encode())]],
            )
        await asyncio.sleep(0.05)

    await bc_record_deliveries(bcast_id, deliveries)
    BROADCAST_JOBS.pop(bcast_id, None)
    status = "cancelled" if cancelled else "done"
    await bc_finish(bcast_id, status, sent, failed + blocked)

    elapsed = max(time.monotonic() - started, 0.001)
    head = "⛔  Broadcast Cancelled" if cancelled else "✅  Broadcast Complete"
    await edit_status(
        f"{head}  #{bcast_id}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{bc_progress_bar(1, 1)}  100%\n\n"
        f"✅ Sent:      {sent}\n"
        f"🚫 Blocked:   {blocked}\n"
        f"⚠️ Failed:    {failed}\n"
        f"👥 Total:     {total}\n\n"
        f"⚡ finished in {elapsed:.1f}s ({sent / elapsed:.1f} msg/s)",
        [
            [Button.inline("🗑 Delete This Broadcast", f"adm_bc_del:{bcast_id}".encode())],
            [Button.inline("📢 Broadcast Center", b"adm_broadcast")],
            [Button.inline("🏠 Home to Admin", b"admin")],
        ],
    )


async def run_broadcast_delete(bcast_id: int, status_chat_id: int, status_msg_id: int):
    """Delete every delivered copy of a broadcast from all user chats,
    with live progress + cancel."""
    job_key = f"del:{bcast_id}"
    BROADCAST_JOBS[job_key] = {"cancel": False}
    rows = await bc_deliveries(bcast_id)
    # Group message ids per user so one API call clears each chat.
    per_user: dict[int, list[int]] = {}
    for uid, mid in rows:
        per_user.setdefault(uid, []).append(mid)
    total = len(per_user)
    deleted = failed = done = 0
    last_edit = 0.0

    async def edit_status(text: str, buttons):
        nonlocal last_edit
        try:
            await client.edit_message(
                status_chat_id, status_msg_id, text, buttons=buttons
            )
            last_edit = time.monotonic()
        except errors.MessageNotModifiedError:
            last_edit = time.monotonic()
        except Exception:
            pass

    async def del_one(uid: int, mids: list[int]):
        nonlocal deleted, failed
        try:
            await client.delete_messages(uid, mids)
            deleted += 1
        except errors.FloodWaitError as e:
            await asyncio.sleep(min(e.seconds, 60) + 1)
            try:
                await client.delete_messages(uid, mids)
                deleted += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1

    cancelled = False
    items = list(per_user.items())
    for i in range(0, total, BROADCAST_CONCURRENCY):
        if BROADCAST_JOBS.get(job_key, {}).get("cancel"):
            cancelled = True
            break
        wave = items[i : i + BROADCAST_CONCURRENCY]
        await asyncio.gather(*(del_one(uid, mids) for uid, mids in wave))
        done = min(i + BROADCAST_CONCURRENCY, total)
        if time.monotonic() - last_edit >= BROADCAST_EDIT_INTERVAL:
            pct = int(done * 100 / total) if total else 100
            await edit_status(
                f"🗑  Deleting Broadcast  #{bcast_id}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{bc_progress_bar(done, total)}  {pct}%\n\n"
                f"🗑 Cleared:  {deleted} / {total} chats\n"
                f"⚠️ Failed:   {failed}",
                [[Button.inline("⛔ Stop Deleting", f"adm_bc_stopdel:{bcast_id}".encode())]],
            )
        await asyncio.sleep(0.05)

    BROADCAST_JOBS.pop(job_key, None)
    if not cancelled:
        await bc_clear_deliveries(bcast_id)
        async with get_db() as db:
            await db.execute(
                "UPDATE broadcasts SET status = 'deleted' WHERE id = ?", (bcast_id,)
            )
            await db.commit()
    head = "⛔  Deletion Stopped" if cancelled else "✅  Broadcast Deleted"
    await edit_status(
        f"{head}  #{bcast_id}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🗑 Cleared from {deleted} chat(s).\n"
        f"⚠️ Failed in {failed} chat(s) "
        "(message too old or chat unavailable).",
        [
            [Button.inline("📢 Broadcast Center", b"adm_broadcast")],
            [Button.inline("🏠 Home to Admin", b"admin")],
        ],
    )


def bc_status_emoji(status: str) -> str:
    return {
        "sending": "📡",
        "done": "✅",
        "cancelled": "⛔",
        "deleted": "🗑",
    }.get(status, "•")


async def build_broadcast_center():
    """(text, buttons) for the admin Broadcast Center screen."""
    recent = await bc_list(8)
    lines = [
        "📢  Broadcast Center",
        "━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "🚀 Send a message to ALL users — text, photos,",
        "videos, premium emojis… everything is preserved.",
        "",
        "Live progress, instant cancel, and one-tap",
        "deletion of any past broadcast from every chat.",
    ]
    rows = [[Button.inline("🚀 New Broadcast", b"adm_bc_new")]]
    if recent:
        lines += ["", f"🗂 Recent broadcasts ({len(recent)}):"]
        for b in recent:
            date = (b["created_at"] or "")[:10]
            rows.append(
                [
                    Button.inline(
                        f"{bc_status_emoji(b['status'])} #{b['id']} · {date} · "
                        f"{b['sent']} sent",
                        f"adm_bc_view:{b['id']}".encode(),
                    )
                ]
            )
    rows.append([Button.inline("🏠 Home to Admin", b"admin")])
    return "\n".join(lines), rows


# ── Database backup / extraction ─────────   ───────────────  ────    ────────────────
# How often the DB file is automatically sent to the admins (12 hours).
DB_BACKUP_INTERVAL_SECONDS = 12 * 60 * 60  # 12 hours: critical user wallet data


async def make_db_snapshot() -> str:
    """Export the current D1 data, never the obsolete local database."""
    from d1_backup import snapshot
    return await snapshot(os.path.dirname(DB_PATH))


async def send_db_backup(chat_ids, note: str = "") -> bool:
    """Snapshot the DB and send the file to every chat id given.
    Returns True if at least one send succeeded. Always cleans up the
    temporary snapshot file."""
    snap_path = None
    ok = False
    try:
        snap_path = await make_db_snapshot()
        size_kb = os.path.getsize(snap_path) / 1024
        total_users = await get_total_user_count()
        caption = (
            "🗄 Database Backup\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"👤 Users: {total_users}\n"
            f"📦 Size: {size_kb:.1f} KB"
            + (f"\n\n{note}" if note else "")
        )
        for cid in chat_ids:
            try:
                await client.send_file(
                    cid,
                    snap_path,
                    caption=caption,
                    force_document=True,
                )
                ok = True
            except Exception as e:
                print(f"DB backup: failed to send to {cid}: {e}")
    except Exception as e:
        print(f"DB backup failed: {e}")
    finally:
        if snap_path and os.path.exists(snap_path):
            try:
                os.remove(snap_path)
            except OSError:
                pass
    return ok


async def auto_db_backup_loop():
    """Background task: send the DB file to all admins every 12 hours.

    The last-sent time is PERSISTED in settings, so bot restarts no longer
    reset the timer (the old version slept the full interval before the
    first send — a restart every <interval meant the backup never fired).
    Checks hourly; sends immediately on startup if a backup is overdue."""
    while True:
        try:
            last_raw = await get_setting("last_db_backup")
            last_ts = float(last_raw) if last_raw else 0.0
            now = time.time()
            if now - last_ts >= DB_BACKUP_INTERVAL_SECONDS:
                ok = await send_db_backup(
                    ADMIN_IDS,
                    note="⏰ Automatic backup (every 12 hours). Keep this file safe.",
                )
                if ok:
                    await set_setting("last_db_backup", str(now))
                else:
                    # Send failed — retry sooner (30 min) instead of
                    # stamping the time and silently skipping 2 days.
                    await asyncio.sleep(30 * 60)
                    continue
        except Exception as e:
            print(f"DB backup loop error: {e}")
        await asyncio.sleep(60 * 60)  # re-check hourly


async def build_admin_panel_text() -> str:
    total_users = await get_total_user_count()
    total_deposit = await get_total_deposit_usd()
    return (
        "🛠  Admin Panel\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 Total Users:      {total_users}\n"
        f"💰 Total Deposited:  ${total_deposit:.2f}\n\n"
        "Pick an action below 👇"
    )


def admin_panel_buttons():
    """Minimal top-level admin panel — features grouped into sub-menus."""
    return [
        [Button.inline("👥 User Management", b"adm_users")],
        [Button.inline("💵 Deposit Settings", b"adm_depcfg")],
        [Button.inline("💳 UPI", b"adm_upimenu")],
        [Button.inline("🛒 Shop", b"adm_shop")],
        [Button.inline("🏭 Wholesale Suppliers", b"wa:home:0")],
        [
            Button.inline("📢 Broadcast", b"adm_broadcast"),
            Button.inline("📊 Stats", b"adm_stats"),
        ],
        [Button.inline("🗄 Extract Database", b"adm_extractdb")],
        [
            Button.inline(
                "▶️ Start Bot (now in Maintenance)"
                if MAINTENANCE
                else "⏸ Stop Bot (Maintenance)",
                b"adm_maint",
            )
        ],
        [btn_back()],
    ]


def admin_users_buttons():
    """User Management sub-menu: find, balances, ban/unban."""
    return [
        [Button.inline("🔍 Find User", b"adm_find")],
        [
            Button.inline("➕ Add $ Balance", b"adm_addbal"),
            Button.inline("➖ Remove $ Balance", b"adm_rmbal"),
        ],
        [
            Button.inline("➕ Add ₹ Balance", b"adm_addbalinr"),
            Button.inline("➖ Remove ₹ Balance", b"adm_rmbalinr"),
        ],
        [
            Button.inline("   Ban User", b"adm_ban"),
            Button.inline("✅ Unban User", b"adm_unban"),
        ],
        [Button.inline("🗑 Delete User Data", b"adm_deluser")],
        [Button.inline("🏠 Home to Admin", b"admin")],
    ]


def admin_depcfg_buttons():
    """Deposit Settings sub-menu: addresses, min deposit, recent deposits."""
    return [
        [Button.inline("🏦 Edit Addresses", b"adm_addr")],
        [Button.inline("⚙️ Min Deposit", b"adm_setmin")],
        [Button.inline("🧾 Recent Deposits", b"adm_deposits")],
        [Button.inline("🏠 Home to Admin", b"admin")],
    ]


def admin_upimenu_buttons():
    """UPI sub-menu: settings + pending requests."""
    return [
        [Button.inline("💳 UPI Settings", b"adm_upiset")],
        [Button.inline("🧾 UPI Requests", b"adm_upi")],
        [Button.inline("   Home to Admin", b"admin")],
    ]


def admin_shop_buttons():
    """Shop sub-menu: manage categories/products, add a category, recent orders."""
    return [
        [Button.inline("🗂 Categories & Products", b"adm_products")],
        [Button.inline("➕ Add Category", b"adm_addcat")],
        [Button.inline("🧾 Recent Orders", b"adm_orders")],
        [Button.inline("🏭 Wholesale Suppliers", b"wa:home:0")],
        [Button.inline("⚠️ Low-Stock Alert", b"adm_setlowstock")],
        [Button.inline("🏠 Home to Admin", b"admin")],
    ]


def admin_addr_buttons():
    rows = [
        [Button.inline(f"      {label}", f"adm_setaddr:{k}".encode())]
        for k, (_, _, label) in ADDR_KINDS.items()
    ]
    rows.append([Button.inline("🏠 Home to Admin", b"admin")])
    return rows


def admin_cancel_buttons():
    return [[Button.inline("✖️ Cancel", b"adm_cancel")]]


def banned_message() -> str:
    return (
        "    Access Suspended\n"
        "━━━━━━━━━━━━━━   ━   ━   ━\n\n"
        "Your access to this bot has been suspended.\n\n"
        f"If you think this is a mistake, contact {ADMIN_CONTACT}."
    )


def math_sans_bold(text: str) -> str:
    """Convert A-Z / a-z / 0-9 to Unicode Mathematical Bold (serif)
    (e.g. 'Shop' -> '𝐒𝐡𝐨𝐩'). Telegram buttons can't use custom fonts, but
    these Unicode letterforms render as a bold serif font everywhere."""
    out = []
    for ch in text:
        if "A" <= ch <= "Z":
            out.append(chr(0x1D400 + ord(ch) - ord("A")))
        elif "a" <= ch <= "z":
            out.append(chr(0x1D41A + ord(ch) - ord("a")))
        elif "0" <= ch <= "9":
            out.append(chr(0x1D7CE + ord(ch) - ord("0")))
        else:
            out.append(ch)
    return "".join(out)


def main_menu_buttons(is_admin: bool = False):
    # Home menu ONLY: colored backgrounds + premium-emoji icons (KeyboardButtonStyle).
    # style = 'success' (green) / 'primary' (blue) / 'danger' (red);
    # icon  = custom-emoji document id (falls back to a plain button if unmapped).
    # Labels use the default font in Pascal Case (home menu only — per user request).
    _e = PREMIUM_EMOJI_MAP.get

    def _b(t):
        return " ".join(w[:1].upper() + w[1:].lower() for w in t.split(" "))
    rows = [
        [
            Button.inline(_b("Shop"), b"shop", style="success", icon=_e("🛒")),
            Button.inline(_b("Wallet"), b"balance", style="success", icon=_e("🪙")),
        ],
        [
            Button.inline(_b("Deposit"), b"deposit_menu", style="primary", icon=_e("💲")),
            Button.inline(_b("My Orders"), b"orders", style="primary", icon=_e("🧾")),
        ],
        [
            Button.url(
                _b("Support"),
                f"https://t.me/{ADMIN_CONTACT.lstrip('@')}",
                # style="danger",
                icon=_e("👤"),
            )
        ],
    ]
    if is_admin:
        rows.append([Button.inline(_b("ADMIN"), b"admin", style="danger")])
    return rows


def main_menu_buttons_plain(is_admin: bool = False):
    """Plain (no color / no custom-emoji icon) version of the home menu, used as a
    safe fallback if Telegram rejects the styled buttons for this bot account."""
    _b = math_sans_bold
    rows = [
        [
            Button.inline("🛒 " + _b("SHOP"), b"shop"),
            Button.inline("🪙 " + _b("WALLET"), b"balance"),
        ],
        [
            Button.inline("💲 " + _b("DEPOSIT"), b"deposit_menu", style="primary"),
            Button.inline("   " + _b("MY ORDERS"), b"orders", style="primary"),
        ],
        [Button.url("👤 " + _b("SUPPORT"), f"https://t.me/{ADMIN_CONTACT.lstrip('@')}")],
    ]
    if is_admin:
        rows.append([Button.inline("🛠 " + _b("ADMIN"), b"admin")])
    return rows


# ── Reusable nav buttons for inner/under screens ───────────────────────────   ────
# Premium-emoji icon ONLY, NO background color (colors are reserved for the home
# menu). icon falls back to a plain label when the emoji id isn't mapped.


def btn_deposit(data=b"deposit_menu", label="Deposit"):
    return Button.inline(
        label, data, icon=PREMIUM_EMOJI_MAP.get("💲"), style="primary"
    )


def btn_dep_history(data=b"dep_history", label="Transaction History"):
    return Button.inline(label, data, icon=5256110612395605858, style="primary")


def btn_wallet(data=b"balance", label="Wallet"):
    return Button.inline(label, data, icon=PREMIUM_EMOJI_MAP.get("🪙"))


def btn_shop(data=b"shop", label="Shop"):
    return Button.inline(label, data, icon=PREMIUM_EMOJI_MAP.get("🛒"))


def btn_orders(data=b"orders", label="My Orders"):
    return Button.inline(label, data, icon=PREMIUM_EMOJI_MAP.get("🧾"), style="primary")


def btn_support(label="Support"):
    return Button.url(
        label,
        f"https://t.me/{ADMIN_CONTACT.lstrip('@')}",
        icon=PREMIUM_EMOJI_MAP.get("👤"),
    )


def btn_back(data=b"menu", label="🏠 Home"):
    return Button.inline(label, data)


def back_home_row(back_to: bytes):
    """The shop-style navigation row: a premium-arrow Back button to the
    parent screen plus a Home button. If the parent IS home, a single styled
    Back button is returned instead (avoids two buttons doing the same thing)."""
    back = Button.inline("Back", back_to, icon=PREMIUM_EMOJI_MAP.get("⬅️"))
    if back_to == b"menu":
        return [back]
    return [back, Button.inline("🏠 Home", b"menu")]


async def send_home_screen(event, text: str, is_admin: bool, edit: bool = False):
    """Render the home screen. If the styled (custom-emoji icon) buttons are
    rejected by Telegram, fall back to plain buttons so /start and /menu can
    never break. For the edit path, fall back to a fresh message if the target
    can't be edited at all."""

    async def _send(buttons):
        if edit:
            # Swap the media back to the banner too: a product page may have
            # replaced it with the product's image. Fall back to a caption-only
            # edit if the media swap is rejected.
            PRODUCT_IMAGE_MSGS.discard((event.chat_id, event.message_id))
            SCREEN_BANNER_MSGS.pop((event.chat_id, event.message_id), None)
            banner_path = get_start_banner()
            file = cached_banner_file(banner_path)
            try:
                msg = await event.edit(text, buttons=buttons, file=file)
                remember_banner_upload(banner_path, msg)
                return msg
            except errors.MessageNotModifiedError:
                raise
            except Exception:
                if file is not banner_path:
                    forget_banner_upload(banner_path)
                    try:
                        msg = await event.edit(
                            text, buttons=buttons, file=banner_path
                        )
                        remember_banner_upload(banner_path, msg)
                        return msg
                    except errors.MessageNotModifiedError:
                        raise
                    except Exception:
                        pass
                return await event.edit(text, buttons=buttons)
        # respond (not reply): don't quote the user's /start or /menu message.
        # Fresh home screens carry the banner image on top; if sending the
        # photo fails for any reason, fall back to a text-only home screen.
        banner_path = get_start_banner()
        file = cached_banner_file(banner_path)
        try:
            msg = await event.respond(text, buttons=buttons, file=file)
            remember_banner_upload(banner_path, msg)
            return msg
        except Exception as e:
            if file is not banner_path:
                forget_banner_upload(banner_path)
                try:
                    msg = await event.respond(
                        text, buttons=buttons, file=banner_path
                    )
                    remember_banner_upload(banner_path, msg)
                    return msg
                except Exception:
                    pass
            print(f"Home banner send failed ({type(e).__name__}: {e}); text only.")
            return await event.respond(text, buttons=buttons)

    try:
        return await _send(main_menu_buttons(is_admin))
    except errors.MessageNotModifiedError:
        return  # already showing the right content — nothing to do
    except Exception as e:
        print(
            f"Styled menu send failed ({type(e).__name__}: {e}); using plain buttons."
        )
    plain = main_menu_buttons_plain(is_admin)
    try:
        return await _send(plain)
    except errors.MessageNotModifiedError:
        return
    except Exception:
        return await event.respond(text, buttons=plain)


def deposit_menu_buttons():
    return [
        [Button.inline("CRYPTO [ Instant ]", b"deposit", icon=usdqq, style="primary")],
        [Button.inline("UPI / QR [ Manual ]", b"deposit_upi", icon=INR_ICON_ID, style="primary")],
        back_home_row(b"menu"),
    ]


def deposit_amount_buttons():
    """Preset USD amounts + custom + back for the crypto deposit flow."""
    return [
        [
            Button.inline("$10", b"dep_amt:10", icon=5359469584489390740, style="success"),
            Button.inline("$25", b"dep_amt:25", icon=5359469584489390740, style="success"),
            Button.inline("$50", b"dep_amt:50", icon=5359469584489390740, style="success"),
        ],
        [Button.inline("✏️ Custom amount", b"dep_custom", style="primary")],
        back_home_row(b"deposit"),
    ]


# Banner shown on top of the /start and /menu home screens. A local file next
# to bot.py is preferred (survives even if the URL goes down); the URL is the
# fallback so the banner still works if the file wasn't uploaded to the VPS.
START_BANNER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "start_banner.png")
START_BANNER_URL = "https://hebbkx1anhila5yf.public.blob.vercel-storage.com/image-ctvHCDdEbcAWX5HXCuOq2QFIqQzgFc.png"


def get_start_banner():
    return START_BANNER_FILE if os.path.exists(START_BANNER_FILE) else START_BANNER_URL


# Per-screen banners (same folder as bot.py). If a file is missing, the screen
# just keeps the home banner instead.
SCREEN_BANNER_FILES = {
    "shop": os.path.join(os.path.dirname(os.path.abspath(__file__)), "shop_banner.png"),
    "wallet": os.path.join(os.path.dirname(os.path.abspath(__file__)), "wallet_banner.png"),
    "deposit": os.path.join(os.path.dirname(os.path.abspath(__file__)), "deposit_banner.png"),
    "orders": os.path.join(os.path.dirname(os.path.abspath(__file__)), "orders_banner.png"),
    "crypto": os.path.join(os.path.dirname(os.path.abspath(__file__)), "crypto_banner.png"),
    "upi": os.path.join(os.path.dirname(os.path.abspath(__file__)), "upi_banner.png"),
    "usdt": os.path.join(os.path.dirname(os.path.abspath(__file__)), "usdt_banner.png"),
    "coin": os.path.join(os.path.dirname(os.path.abspath(__file__)), "coin_banner.png"),
    # Per-network deposit QR codes, shown on the pay-address screen. Save each
    # as qr_<chain>.png next to bot.py; missing files fall back to the home
    # banner automatically.
    "qr_bsc": os.path.join(os.path.dirname(os.path.abspath(__file__)), "qr_bsc.png"),
    "qr_polygon": os.path.join(os.path.dirname(os.path.abspath(__file__)), "qr_polygon.png"),
    "qr_tron": os.path.join(os.path.dirname(os.path.abspath(__file__)), "qr_tron.png"),
    "qr_solana": os.path.join(os.path.dirname(os.path.abspath(__file__)), "qr_solana.png"),
    "qr_ton": os.path.join(os.path.dirname(os.path.abspath(__file__)), "qr_ton.png"),
    "qr_ltc": os.path.join(os.path.dirname(os.path.abspath(__file__)), "qr_ltc.png"),
    "qr_btc": os.path.join(os.path.dirname(os.path.abspath(__file__)), "qr_btc.png"),
}


def get_screen_banner(name):
    # `name` is either a registered screen name ("shop", "wallet", …) or a
    # direct image file path (category images). Anything else — including a
    # non-string passed by mistake — falls back to the home banner instead
    # of crashing.
    if not isinstance(name, str) or not name:
        return get_start_banner()
    if os.path.sep in name:
        return name if os.path.exists(name) else get_start_banner()
    path = SCREEN_BANNER_FILES.get(name)
    return path if path and os.path.exists(path) else get_start_banner()


# Messages currently showing a per-screen banner: (chat_id, message_id) -> name.
SCREEN_BANNER_MSGS: dict = {}

# Banner upload cache: local file path -> Telegram Photo object. Banners are
# 0.2–1.1 MB each; without this cache every screen switch re-uploads the PNG
# to Telegram, which makes button taps feel very slow on small VPSes. With it,
# each banner uploads once per bot run and is then reused instantly (like a
# file_id). Keyed by (path, mtime) so replacing a banner file busts the cache.
_BANNER_PHOTO_CACHE: dict = {}


def cached_banner_file(target):
    """Return a cached Telegram photo for a local banner path, or the path
    itself on the first use (Telethon will upload it)."""
    if not isinstance(target, str) or not os.path.isfile(target):
        return target  # URL or missing file — let Telethon handle it
    try:
        key = (target, os.path.getmtime(target))
    except OSError:
        return target
    return _BANNER_PHOTO_CACHE.get(key, target)


def remember_banner_upload(target, msg):
    """After a successful send/edit, remember the uploaded photo for reuse."""
    if not isinstance(target, str) or not os.path.isfile(target):
        return
    photo = getattr(msg, "photo", None)
    if photo is None:
        return
    try:
        key = (target, os.path.getmtime(target))
    except OSError:
        return
    _BANNER_PHOTO_CACHE[key] = photo


def forget_banner_upload(target):
    """Drop a cached photo (e.g. Telegram rejected the stale reference)."""
    if not isinstance(target, str):
        return
    for key in [k for k in _BANNER_PHOTO_CACHE if k[0] == target]:
        _BANNER_PHOTO_CACHE.pop(key, None)


def build_home_text(balance_usd: float, balance_inr: float = 0.0) -> str:
    # Matches the reference screenshot: heading + divider, ONE quote block with
    # the tagline and the three perks, then the footer. All the emojis used
    # here are premium-mapped in _DEFAULT_PREMIUM_EMOJI_MAP.
    return (
        "**👋 Welcome to Hidden Marketplace**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "> **💬 Everything you need in one marketplace, with secure payments "
        "and fast delivery 💰**\n"
        ">\n"
        "> **✅ Automatic Payments**\n"
        "> **⚡️ Instant Delivery**\n"
        "> **2️⃣ Faster Response**\n\n"
        "**🗃 Browse using the options below**"
    )


def build_balance_text(balance_usd: float, balance_inr: float = 0.0) -> str:
    return (
        "**💳  Your Balance**\n"
        "**━━━━━━━━━━━━━━━━          ━━━**\n\n"
        f"**💵  USD: ${balance_usd:.2f}**\n\n"
        f"**🍌  INR: ₹{balance_inr:.2f}**\n\n"
        "> **Use your wallet balance to purchase products instantly ✖️**"
    )


def shop_price_label(p: dict) -> str:
    """Compact dual-price label, e.g. '$5.00 / ₹400.00' (omits unpriced currencies)."""
    parts = []
    if (p.get("price_usd") or 0) > 0:
        parts.append(f"${p['price_usd']:.2f}")
    if (p.get("price_inr") or 0) > 0:
        parts.append(f"₹{p['price_inr']:.2f}")
    return " / ".join(parts) if parts else "—"


def product_is_manual(p: dict) -> bool:
    """True if this product is manually fulfilled (email invite) vs instant pool."""
    return (p.get("fulfillment") or "instant") == "manual"


def product_emoji(p: dict) -> str:
    """Use the exact product button icon, bypassing shared fallback-emoji mappings."""
    char = p.get("emoji_char") or ""
    if p.get("emoji_id"):
        return f"[{char or '📦'}](emoji/{int(p['emoji_id'])}) "
    return f"{char} " if char else ""


def product_footer(p: dict, stock: int) -> str:
    on_demand = product_is_manual(p) or (
        p.get("supplier_id") and p.get("supplier_stock") is None and stock > 0
    )
    stock_label = "On demand" if on_demand else f"{max(0, stock)} available"
    delivery = "⚡ Delivery is automatic after payment confirmation."
    if product_is_manual(p):
        delivery = "📧 Delivery is by email invite after payment confirmation."
    elif p.get("supplier_delivery") == "activation":
        delivery = "⚡ Activation begins after payment confirmation; follow progress in My Orders."
    return (
        f"**💲 Price: {shop_price_label(p) if (p.get('price_usd') or p.get('price_inr')) else '$0.00'}**\n"
        f"**🛡 Warranty: {p.get('warranty_days') or 0} days**\n\n"
        f"**📊 Stock: {stock_label}**\n\n"
        f"{delivery}"
    )


def product_purchasable(p: dict) -> bool:
    """A product can be bought if it's manual (no pool) or has instant stock left."""
    return product_is_manual(p) or (p.get("stock") or 0) > 0


# Messages whose media currently shows a PRODUCT image (not the home banner):
# (chat_id, message_id). The next non-product screen edit swaps the banner back.
PRODUCT_IMAGE_MSGS: set = set()


async def send_product_page(event, user_id: int, p: dict, edit: bool = False):
    """Show the product page WITH its image (when one is set) above the
    description. Falls back to text-only if the image can't be sent."""
    text, rows = await build_product_page(user_id, p)
    img = (p.get("image_file_id") or "").strip()
    # Images are stored as local file paths. Ignore anything else (legacy
    # numeric IDs from the old format) and skip paths whose file vanished.
    if img and not (os.path.sep in img and os.path.exists(img)):
        img = ""
    if img:
        # Reuse the banner upload cache for product images too: upload the
        # file to Telegram once per bot run, then reuse the stored photo on
        # every later view instead of re-uploading from disk on each tap.
        file = cached_banner_file(img)
        attempts = [file, img] if file is not img else [img]
        last_err: Exception | None = None
        for attempt_file in attempts:
            try:
                if edit:
                    # Home/shop screens are media messages (banner photo), so
                    # the media can be swapped in-place to this product image.
                    msg = await event.edit(text, buttons=rows, file=attempt_file)
                    remember_banner_upload(img, msg)
                    PRODUCT_IMAGE_MSGS.add((event.chat_id, event.message_id))
                else:
                    msg = await event.respond(text, buttons=rows, file=attempt_file)
                    if msg is not None:
                        remember_banner_upload(img, msg)
                        PRODUCT_IMAGE_MSGS.add((msg.chat_id, msg.id))
                return
            except errors.MessageNotModifiedError:
                return
            except Exception as e:
                # Stale cached photo reference — drop it and retry from disk.
                forget_banner_upload(img)
                last_err = e
        print(
            f"Product image send failed "
            f"({type(last_err).__name__}: {last_err}); text only."
        )
    if edit:
        try:
            await event.edit(text, buttons=rows)
            return
        except Exception:
            pass
    await event.respond(text, buttons=rows)


async def build_product_page(user_id: int, p: dict):
    """(text, button_rows) for the user-facing single-product page. Used by the
    shop_view callback and by t.me deep links (?start=prod_<id>)."""
    pid = p["id"]
    is_manual = product_is_manual(p)
    stock = await product_stock(pid)
    desc = (p["description"] or "").strip() or "—"
    lines = [desc, "", product_footer(p, stock)]
    rows = []
    if not is_manual and stock <= 0:
        rows = [
            [Button.inline("❌ Out of Stock", f"stock_empty:{pid}".encode(), style="danger")],
            [Button.inline("🔔 Notify me when back in stock", f"stock_notify:{pid}".encode(), style="success")],
            [Button.inline("Back to Store", b"shop")],
        ]
        return "\n".join(lines), rows
    else:
        price_usd = p["price_usd"] or 0
        price_inr = p["price_inr"] or 0
        # 3 main buttons: Pay with Crypto (green), Pay with Wallet (blue),
        # Learn More (blue). Wallet-balance choices live inside Pay with
        # Wallet now — no Buy-for / Deposit buttons on the product page.
        if price_usd > 0:
            rows.append(
                [
                    Button.inline(
                        "Pay with Crypto",
                        f"paycrypto:{pid}".encode(),
                        style="success",
                        icon=5206584567116352967,
                    )
                ]
            )
        if price_usd > 0 or price_inr > 0:
            rows.append(
                [
                    Button.inline(
                        "Pay with Wallet",
                        f"paywallet:{pid}".encode(),
                        style="primary",
                        icon=4994863664633741922,
                    )
                ]
            )
    if product_learn_more(p):
        rows.append(
            [
                Button.inline(
                    "Learn More",
                    f"learnmore:{pid}".encode(),
                    icon=5339399017631591044,
                    style="primary",
                )
            ]
        )
    rows.append(
        [
            Button.inline(
                "Back", b"shop", icon=PREMIUM_EMOJI_MAP.get("⬅️")
            ),
            Button.inline("🏠 Home", b"shop"),
        ]
    )
    return "\n".join(lines), rows


def product_deep_link(product_id: int) -> str:
    """t.me link that opens the bot straight onto this product's page."""
    return f"https://t.me/{BOT_USERNAME}?start=prod_{product_id}"


async def build_shop_rows() -> list:
    """Show all active products directly, including out-of-stock products."""
    products = await list_active_products()
    rows = []
    for p in products:
        label = p["name"]
        if product_is_manual(p):
            style = "success"
        elif (p.get("stock") or 0) <= 0:
            style = "danger"
        else:
            style = "success"
        rows.append(
            [
                Button.inline(
                    label,
                    f"shop_view:{p['id']}".encode(),
                    style=style,
                    icon=p.get("emoji_id"),
                )
            ]
        )
    rows.append([btn_orders()])
    rows.append([btn_back()])
    return rows


async def build_shop_text(user_id: int) -> str:
    balance_usd, balance_inr = await get_balances(user_id)
    return (
              
        f"[👛](emoji/5256186332669035163) **Balance: "
        f"${balance_usd:.2f} / ₹{balance_inr:.2f}**\n\n"
        "**Choose a product**[👇](emoji/5228931982037116397)\n\n"
    )


def build_register_wallet_text() -> str:
    return (
        "🔗  Register Your Wallet (one-time)\n"
        "━━━━━━━━━━━━━━━━  ━━━━━━\n\n"
        "For your security, deposits are locked to YOUR wallet.\n\n"
        "Send me the BEP20 wallet address you will pay FROM\n"
        "(the one in your wallet app, starts with 0x).\n\n"
        "Only payments coming from this exact wallet will be\n"
        "credited to you — so no one else can claim your deposit.\n\n"
        "Tap “🔗 Set My Wallet” below and paste your address."
    )


def register_wallet_buttons():
    return [
        [Button.inline("🔗 Set My Wallet", b"bind_wallet")],
        [Button.inline("🏠 Home", b"menu")],
    ]


def build_deposit_text(intent: dict | None = None) -> str:
    if not DEPOSIT_ADDRESS:
        return (
            "💵  Deposit — Temporarily Unavailable\n"
            "━━━━━━━━━━━━━━━━━━━   \n\n"
            "Deposits are not set up yet. Please check back soon."
        )
    if intent is None:
        # Shouldn't normally happen — no reservation to show.
        return (
            "💵  Deposit USDT\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Tap 💵 Deposit and enter an amount to get your payment details."
        )
    mins = DEPOSIT_INTENT_TTL_MINUTES
    chain = intent.get("chain", "bsc")
    token = DEPOSIT_TOKENS.get(chain, DEPOSIT_TOKENS["bsc"])
    label = token["label"]
    symbol = token["symbol"]
    dep_addr = deposit_address_for(chain)
    # Coin-specific header emoji: TON / LTC / BTC show their own coin icons,
    # while the USDT networks keep the tether icon.
    _header_emoji = {
        "ton": "[💎](emoji/5078343973303485905)",
        "ltc": "[🪙](emoji/5217877586620729050)",
        "btc": "[🟠](emoji/5231005931550030290)",
    }.get(token.get("kind"), "[💎](emoji/5832577678300943429)")
    return (
        f"{_header_emoji} **Deposit {symbol} — {label}**\n"
        "**━━━━   ━━━━━━━━━━━━━━━━━━**\n\n"
        f"[➡️](emoji/5215720576735255650) **Send EXACTLY this Amount: "
        f"`{format_intent_amount(chain, intent['amount_milli'])}` {symbol}"
        + (f" (≈ ${intent['usd_amount']:.2f} USD)" if intent.get("usd_amount") else "")
        + "**\n\n"
        f"**To this Address:** `{dep_addr}`\n\n"
        f"[⚠️](emoji/5447644880824181073) **Send the exact amount via "
        f"{label} only.**\n\n"
        f"[🕐](emoji/5902050947567194830) **Expires in {mins} minutes.**\n\n"
        "**After payment, tap \"✅ I've Paid\" and paste your TxID.**\n\n"
        "**Amount will be credited automatically after instant on-chain "
        "verification.**\n\n"
        f"[👥](emoji/5301218742645051855) **Issues? Support:** {ADMIN_CONTACT}"
    )


def deposit_buttons(back_to: bytes = b"deposit"):
    rows = []
    if DEPOSIT_ADDRESS:
        rows.append([Button.inline("✅ I’ve Paid — Submit TxID", b"submit_tx")])
    rows.append(back_home_row(back_to))
    return rows


def build_credit_shop_text(balance_usd: float) -> str:  # legacy dead code
    lines = [
        "🛒  Credit Shop",
        "━━━━━━━━━━━   ━━━━━━━━━━━",
        "",
        f"Your Balance:  ${balance_usd:.2f}",
        "",
        "Pick a pack to buy with your balance:",
        "",
    ]
    for credits, price in CREDIT_PACKS:
        lines.append(f"• {credits} credits — ${price:.2f}")
    lines.append("")
    lines.append("Need more balance? Tap 💵 Deposit.")
    return "\n".join(lines)


def shop_buttons():
    rows = []
    for i, (credits, price) in enumerate(CREDIT_PACKS):
        rows.append(
            [Button.inline(f"{credits} credits — ${price:.2f}", f"buy:{i}".encode())]
        )
    rows.append(
        [Button.inline("Deposit", b"deposit", icon=5445353829304387411), Button.inline("🏠 Home", b"menu")]
    )
    return rows


async def notify_deposit_credited(user_id: int, amount: float, new_balance: float):
    await notify_user(
        user_id,
        "✅ Deposit Received\n"
        "━━━━━━━━━━━━━━━━━━━  \n\n"
        f"+${amount:.2f} USDT added to your balance.\n"
        f"New Balance: ${new_balance:.2f}\n\n"
        "Open 🛒 Shop to buy credits.",
    )


# ═══════════════  ═══  ════════════════════════════════════════════════  ══════════
#  ANIMATION  (edit-in-place — all responses use a single message)
# ═══════  ══════════  ══════════════════════  ═════════════════════════════════════


async def send_typing_text(
    event, full_text: str, words_per_step: int = 14, step_delay: float = 0.03
):
    """Send a plain-text message and animate it by editing in place."""
    word_ends = [m.end() for m in re.finditer(r"\S+", full_text)]
    if not word_ends:
        return await event.reply(full_text)
    sent = await event.reply("▍")
    indices = list(range(words_per_step - 1, len(word_ends), words_per_step))
    if not indices or indices[-1] != len(word_ends) - 1:
        indices.append(len(word_ends) - 1)
    for step, wi in enumerate(indices):
        pos = word_ends[wi]
        is_last = step == len(indices) - 1
        display = full_text[:pos] if is_last else full_text[:pos] + " ▍"
        try:
            await sent.edit(display)
        except Exception:
            pass
        if not is_last:
            await asyncio.sleep(step_delay)
    return sent


async def send_typing_code(
    event, full_text: str, words_per_step: int = 12, step_delay: float = 0.04
):
    """Send a monospace code block and animate it by editing in place."""
    word_ends = [m.end() for m in re.finditer(r"\S+", full_text)]
    if not word_ends:
        return await event.reply(f"```\n{full_text}\n```")
    sent = await event.reply("`...`")
    indices = list(range(words_per_step - 1, len(word_ends), words_per_step))
    if not indices or indices[-1] != len(word_ends) - 1:
        indices.append(len(word_ends) - 1)
    for step, wi in enumerate(indices):
        pos = word_ends[wi]
        is_last = step == len(indices) - 1
        cursor = "" if is_last else "    "
        display = f"```\n{full_text[:pos]}{cursor}\n```"
        try:
            await sent.edit(display)
        except Exception:
            pass
        if not is_last:
            await asyncio.sleep(step_delay)
    return sent


# ════════════════════════════════════════════════════════════════════════════   ══
#  TELEGRAM BOT
# ══════════════════════════════════════════════════════════════════════  ════════

client = TelegramClient("premium_bot", API_ID, API_HASH)
_health_runner = None


async def start_health_server():
    global _health_runner
    port = int(os.environ.get("PORT", "10000"))
    app = web.Application()
    app.router.add_get("/healthz", lambda request: web.json_response({"status": "ok"}))
    _health_runner = web.AppRunner(app)
    await _health_runner.setup()
    site = web.TCPSite(_health_runner, "0.0.0.0", port)
    await site.start()
    print(f"Health server listening on port {port}.")


# ── Premium (custom) emoji parse mode ──────────────────────────────────────────
# A thin wrapper around Telethon's default markdown parser: after normal markdown
# parsing, any standard emoji present in PREMIUM_EMOJI_MAP gets a MessageEntityCustomEmoji
# attached so it renders as the premium version. Setting this on the client means EVERY
# outgoing message/caption is upgraded automatically — no per-call changes needed.
from telethon.extensions import markdown as _tl_markdown
from telethon.helpers import add_surrogate, del_surrogate
from telethon.tl.functions.messages import GetCustomEmojiDocumentsRequest
from telethon.tl.types import (
    MessageEntityCustomEmoji,
    DocumentAttributeCustomEmoji,
    MessageEntityCode,
    MessageEntityPre,
    MessageEntityBlockquote,
    MessageEntityBold,
    MessageEntityItalic,
    MessageEntityTextUrl,
)


class PremiumEmojiParseMode:
    @staticmethod
    def parse(text):
        # Telethon's markdown.parse ends with strip_text(), which removes
        # leading/trailing whitespace (incl. blank lines) from the message.
        # Our quote spans are computed from LINE INDICES, so if we let that
        # strip happen after we've recorded indices, every line shifts and
        # quotes/premium emojis land on the wrong text (this is exactly what
        # broke the shop product page, which starts with a blank line).
        # Pre-strip here so Telethon's strip is a no-op.
        text = text.strip()

        # Pre-process blockquotes: strip leading "> " and remember which line
        # indices are quotes. Offsets are computed AFTER markdown parsing so
        # that removed ** markers don't shift our positions.
        lines = text.split("\n")
        quote_line_indices = set()
        expandable_line_indices = set()  # ">> " lines → collapsed/expandable quote
        new_lines = []
        for i, line in enumerate(lines):
            if line.startswith(">> "):
                quote_line_indices.add(i)
                expandable_line_indices.add(i)
                new_lines.append(line[3:])
            elif line.startswith(">>"):
                quote_line_indices.add(i)
                expandable_line_indices.add(i)
                new_lines.append(line[2:])
            elif line.startswith("> "):
                quote_line_indices.add(i)
                new_lines.append(line[2:])
            elif line.startswith(">"):
                # Tolerate ">" without a following space too (incl. a lone ">"
                # which marks an intentionally EMPTY line inside a quote).
                quote_line_indices.add(i)
                new_lines.append(line[1:])
            else:
                new_lines.append(line)

        # Removing quote prefixes can re-introduce leading/trailing blank
        # lines (a quote that starts or ends with a lone ">"). Telethon's
        # strip would remove those lines and shift our indices again, so
        # drop them ourselves and shift the recorded indices to match.
        while new_lines and not new_lines[0].strip():
            new_lines.pop(0)
            quote_line_indices = {i - 1 for i in quote_line_indices if i > 0}
            expandable_line_indices = {
                i - 1 for i in expandable_line_indices if i > 0
            }
        while new_lines and not new_lines[-1].strip():
            last = len(new_lines) - 1
            new_lines.pop()
            quote_line_indices.discard(last)
            expandable_line_indices.discard(last)

        text = "\n".join(new_lines)

        text, entities = _tl_markdown.parse(text)

        # Inline premium emojis: [🎁](emoji/5440502878491930005) parses as a
        # text-url entity — convert those into real custom-emoji entities so
        # a specific emoji ID can be used per line (overriding the global map).
        inline_emoji_spans = []
        for i, e in enumerate(entities):
            if isinstance(e, MessageEntityTextUrl) and e.url.startswith("emoji/"):
                try:
                    doc_id = int(e.url.split("/", 1)[1])
                except ValueError:
                    continue
                entities[i] = MessageEntityCustomEmoji(
                    offset=e.offset, length=e.length, document_id=doc_id
                )
                inline_emoji_spans.append((e.offset, e.offset + e.length))

        # Now compute spans on the final parsed text (** markers already gone).
        if quote_line_indices:
            # Merge consecutive quoted lines into ONE blockquote entity so a
            # multi-line quote renders as a single block, not stacked ones.
            # IMPORTANT: empty quoted lines (a lone ">") must NOT break the
            # run — a 3-line quote with a blank middle line is still ONE quote.
            parsed_lines = text.split("\n")
            char_offset = 0
            run_start = None  # utf-16 offset where the current quote run began
            run_end = None
            run_expandable = False

            def _make_quote(offset, length, collapsed):
                if collapsed:
                    try:
                        return MessageEntityBlockquote(
                            offset=offset, length=length, collapsed=True
                        )
                    except TypeError:
                        pass  # older Telethon without expandable-quote support
                return MessageEntityBlockquote(offset=offset, length=length)

            def _flush():
                nonlocal run_start, run_end, run_expandable
                if run_start is not None and run_end is not None and run_end > run_start:
                    entities.append(
                        _make_quote(run_start, run_end - run_start, run_expandable)
                    )
                run_start = None
                run_end = None
                run_expandable = False

            for i, line in enumerate(parsed_lines):
                line_len = len(line.encode("utf-16-le")) // 2
                if i in quote_line_indices:
                    if run_start is None:
                        run_start = char_offset
                    if line_len > 0:
                        run_end = char_offset + line_len
                    if i in expandable_line_indices:
                        run_expandable = True
                else:
                    _flush()
                char_offset += line_len + 1  # +1 for the \n
            _flush()

        if PREMIUM_EMOJI_MAP:
            # Never place a custom emoji inside a code/pre span (Telegram rejects the
            # overlap), and never let two custom-emoji spans overlap each other.
            protected = [
                (e.offset, e.offset + e.length)
                for e in entities
                if isinstance(e, (MessageEntityCode, MessageEntityPre))
            ]
            claimed = list(inline_emoji_spans)

            def _intersects(a, b, ranges):
                return any(a < y and x < b for (x, y) in ranges)

            # Longer emoji first so multi-codepoint sequences win over their pieces.
            for emoji in sorted(PREMIUM_EMOJI_MAP, key=len, reverse=True):
                doc_id = PREMIUM_EMOJI_MAP[emoji]
                start = 0
                while True:
                    idx = text.find(emoji, start)
                    if idx == -1:
                        break
                    offset = len(text[:idx].encode("utf-16-le")) // 2
                    length = len(emoji.encode("utf-16-le")) // 2
                    if not _intersects(
                        offset, offset + length, protected
                    ) and not _intersects(offset, offset + length, claimed):
                        entities.append(
                            MessageEntityCustomEmoji(
                                offset=offset, length=length, document_id=doc_id
                            )
                        )
                        claimed.append((offset, offset + length))
                    start = idx + len(emoji)
            entities.sort(key=lambda e: e.offset)
        return text, entities

    @staticmethod
    def unparse(text, entities):
        # Drop custom-emoji entities before unparsing (markdown can't represent them).
        ents = [
            e for e in (entities or []) if not isinstance(e, MessageEntityCustomEmoji)
        ]
        return _tl_markdown.unparse(text, ents)


client.parse_mode = PremiumEmojiParseMode()


async def load_premium_emoji_pack():
    """If PREMIUM_EMOJI_PACK is set, pull every custom emoji from that pack and map
    its `alt` (the standard emoji it stands in for)     its document id. Explicit
    PREMIUM_EMOJI_MAP entries win over the pack. Safe no-op on any failure."""
    if not PREMIUM_EMOJI_PACK:
        return
    try:
        from telethon.tl.functions.messages import GetStickerSetRequest
        from telethon.tl.types import InputStickerSetShortName

        result = await client(
            GetStickerSetRequest(
                stickerset=InputStickerSetShortName(short_name=PREMIUM_EMOJI_PACK),
                hash=0,
            )
        )
        added = 0
        for doc in result.documents:
            alt = None
            for attr in doc.attributes:
                if isinstance(attr, DocumentAttributeCustomEmoji):
                    alt = attr.alt
                    break
            if alt and alt not in PREMIUM_EMOJI_MAP:
                PREMIUM_EMOJI_MAP[alt] = doc.id
                added += 1
        print(
            f"Premium emoji pack '{PREMIUM_EMOJI_PACK}': mapped {added} emoji "
            f"({len(PREMIUM_EMOJI_MAP)} total)."
        )
    except Exception as e:
        print(f"Could not load premium emoji pack '{PREMIUM_EMOJI_PACK}': {e}")


# ── Category premium-emoji helpers ───────    ───────────────   ────────────────────


def register_category_emoji(emoji_char: str | None, emoji_id: int | None) -> None:
    """Add a category's premium emoji to the live map so the parse mode
    upgrades it in message text."""
    if emoji_char and emoji_id:
        PREMIUM_EMOJI_MAP[emoji_char] = emoji_id


async def unregister_category_emoji(old_char: str | None) -> None:
    """Drop a replaced category/product emoji from the live map, unless it's a
    default mapping or still used by another category or product."""
    if not old_char or old_char in _DEFAULT_PREMIUM_EMOJI_MAP:
        return
    try:
        for c in await list_categories():
            if c.get("emoji_char") == old_char:
                return
        for p in await list_all_products():
            if p.get("emoji_char") == old_char:
                return
    except Exception:
        return
    PREMIUM_EMOJI_MAP.pop(old_char, None)


async def load_category_emojis():
    """On startup, register every stored category/product emoji into the
    premium map, plus any extra mappings saved from description formatting."""
    try:
        for c in await list_categories():
            register_category_emoji(c.get("emoji_char"), c.get("emoji_id"))
        for p in await list_all_products():
            register_category_emoji(p.get("emoji_char"), p.get("emoji_id"))
        async with get_db() as db:
            async with db.execute("SELECT char, doc_id FROM emoji_map") as cur:
                for ch, did in await cur.fetchall():
                    register_category_emoji(ch, did)
    except Exception as e:
        print(f"Could not load category emojis: {e}")


async def save_emoji_mappings(pairs) -> None:
    """Persist (emoji_char, document_id) pairs and register them live, so
    premium emojis used in descriptions keep rendering after restarts."""
    pairs = [(ch, did) for ch, did in pairs if ch and did]
    if not pairs:
        return
    async with get_db() as db:
        await db.executemany(
            "INSERT OR REPLACE INTO emoji_map (char, doc_id) VALUES (?, ?)", pairs
        )
        await db.commit()
    for ch, did in pairs:
        register_category_emoji(ch, did)


def serialize_message_markdown(msg):
    """Convert a Telegram message (raw text + formatting entities) back into
    this bot's markdown dialect (**bold**, __italic__, `code`, "> " quote
    lines), and collect (emoji_char, document_id) pairs for every premium
    emoji in it. Rendering the returned text through the bot's parse mode
    reproduces the original formatting."""
    raw = msg.message or ""
    ents = msg.entities or []
    s = add_surrogate(raw)
    # Each insert is (surrogate_pos, order_key, marker). order_key controls
    # left-to-right ordering when several markers land on the same position:
    # closers first (inner spans close before outer), then "> " quote
    # prefixes, then openers (outer spans open before inner). This keeps
    # nesting valid (e.g. "> **__text__**", never "**> __text**__").
    inserts = []
    pairs = []
    quote_positions = set()  # line-start positions already given a quote prefix
    markers = {
        MessageEntityBold: (0, "**"),
        MessageEntityItalic: (1, "__"),
        MessageEntityCode: (2, "`"),
    }
    spans = {}  # (rank, marker) -> list of [start, end) spans
    for e in ents:
        start, end = e.offset, e.offset + e.length
        if isinstance(e, MessageEntityCustomEmoji):
            pairs.append((del_surrogate(s[start:end]), e.document_id))
            continue
        rank_marker = markers.get(type(e))
        if rank_marker is not None:
            spans.setdefault(rank_marker, []).append([start, end])
        elif isinstance(e, MessageEntityBlockquote):
            # Expandable ("show more") quotes round-trip via a ">> " prefix.
            q_marker = ">> " if getattr(e, "collapsed", False) else "> "
            # Snap to the start of the line the quote begins on — Telegram can
            # report the entity offset mid-line, which would otherwise put the
            # "> " prefix in the middle of a line and lose that line on re-parse.
            line_start = s.rfind("\n", 0, start) + 1
            pos = line_start
            for line in s[line_start:end].split("\n"):
                # Prefix EVERY line the quote covers (blank ones included, so a
                # 3-line quote never collapses to 2), and never prefix the same
                # line twice when Telegram sends stacked quote entities.
                if pos not in quote_positions:
                    quote_positions.add(pos)
                    inserts.append((pos, (1, 0), q_marker))
                pos += len(line) + 1
    for (rank, marker), sp in spans.items():
        # Merge touching/overlapping same-type spans (Telegram often splits a
        # bold run around emojis), then split every span at newlines so markers
        # never cross a line break — that would put e.g. "**" before the "> "
        # of the next quoted line and break quote detection.
        sp.sort()
        merged = []
        for st, en in sp:
            if merged and st <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], en)
            else:
                merged.append([st, en])
        for st, en in merged:
            seg_start = st
            for i in range(st, en + 1):
                if i == en or s[i] == "\n":
                    # Trim trailing spaces so "** **"-style markers around
                    # whitespace-only tails don't appear.
                    seg_end = i
                    while seg_end > seg_start and s[seg_end - 1] == " ":
                        seg_end -= 1
                    if seg_end > seg_start:
                        # closer: inner first; opener: outer first. Mirrored
                        # rank tie-break keeps identical spans nested.
                        inserts.append((seg_end, (0, -seg_start, -rank), marker))
                        inserts.append(
                            (seg_start, (2, -(seg_end - seg_start), rank), marker)
                        )
                    seg_start = i + 1
    # Insert from the end so earlier positions stay valid; at equal positions
    # the marker meant to appear leftmost is inserted last.
    for pos, _order, marker in sorted(
        inserts, key=lambda t: (t[0], t[1]), reverse=True
    ):
        s = s[:pos] + marker + s[pos:]
    return del_surrogate(s), pairs


async def resolve_custom_emoji(emoji_id: int) -> str | None:
    """Look up a custom-emoji document id and return its standard-emoji
    fallback (`alt`), or None if the id is invalid."""
    try:
        docs = await client(GetCustomEmojiDocumentsRequest(document_id=[emoji_id]))
        for doc in docs:
            for attr in doc.attributes:
                if isinstance(attr, DocumentAttributeCustomEmoji):
                    return attr.alt
    except Exception as e:
        print(f"Could not resolve custom emoji {emoji_id}: {e}")
    return None


async def parse_category_input(
    event, text: str, label: str = "category", maxlen: int = 60
):
    """Parse an admin's category/product-name message, supporting an optional
    premium emoji. Returns (name, emoji_char, emoji_id, error_message).

    Accepted forms:
      • "<premium emoji> Name"  (emoji sent inline — captured from entities)
      • "Name | 5368324170671202286"  (explicit custom-emoji document id)
      • "Name"  (no emoji)
    """
    name = text.strip()
    emoji_char = None
    emoji_id = None

    msg = event.message
    ents = [e for e in (msg.entities or []) if isinstance(e, MessageEntityCustomEmoji)]
    if ents:
        e = ents[0]
        s = add_surrogate(msg.message)
        emoji_char = del_surrogate(s[e.offset : e.offset + e.length])
        emoji_id = e.document_id
        # Remove the emoji itself from the stored name.
        name = del_surrogate(s[: e.offset] + s[e.offset + e.length :]).strip()
    else:
        # "<emoji_id> Name..." (id first) or "Name | <emoji_id>"
        m = re.match(r"^(\d{8,})\s+(.+)$", name)
        if m:
            emoji_id = int(m.group(1))
            name = m.group(2).strip()
        else:
            m = re.match(r"^(.*?)\s*\|\s*(\d{8,})\s*$", name)
            if m:
                name = m.group(1).strip()
                emoji_id = int(m.group(2))
        if emoji_id is not None:
            emoji_char = await resolve_custom_emoji(emoji_id)
            if not emoji_char:
                return (
                    None,
                    None,
                    None,
                    "❌ Couldn't find a premium emoji with that ID.\n"
                    "Double-check the emoji ID, or just send the premium "
                    "emoji itself followed by the name.",
                )

    if not name or len(name) > maxlen:
        return (None, None, None, f"Send a {label} name (up to {maxlen} chars).")
    return name, emoji_char, emoji_id, None


# ── Notification Helpers ─────────────────────────────────────────────  ─────────


async def notify_user(user_id: int, message: str, buttons=None):
    """Send a notification to a user; silently ignore if unreachable."""
    try:
        await client.send_message(user_id, message, buttons=buttons)
    except Exception:
        pass


async def _notify_admins_bg(message: str, buttons=None):
    for aid in ADMIN_IDS:
        try:
            await client.send_message(aid, message, buttons=buttons)
        except Exception:
            pass


async def notify_admins(message: str, buttons=None):
    """Queue a notification to every admin WITHOUT blocking the caller.
    Admin sends are network round-trips (one per admin) that used to run
    inline inside purchase/deposit handlers, delaying the user's response.
    Now they run in a background task; callers may still `await` this
    (it returns immediately)."""
    asyncio.create_task(_notify_admins_bg(message, buttons))


# ── Log group ────────────────────────────────────────────────────────────────
# All top-ups and purchases are also posted to this Telegram group.
# The bot must be a member of the group. Different clients show group IDs
# in different formats, so we try the common variants once and cache the
# one that works.
LOG_GROUP_ID = -5319859343
_LOG_GROUP_RESOLVED: list = []


def ist_now_str() -> str:
    """Current time as '11-Jul-2026 01:00 PM IST'."""
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%d-%b-%Y %I:%M %p IST")


async def _log_to_group_bg(message: str) -> None:
    candidates = _LOG_GROUP_RESOLVED or [
        LOG_GROUP_ID,
        int(f"-100{abs(LOG_GROUP_ID)}"),
        abs(LOG_GROUP_ID),
    ]
    for cid in candidates:
        try:
            await client.send_message(cid, message)
            if not _LOG_GROUP_RESOLVED:
                _LOG_GROUP_RESOLVED.append(cid)
            return
        except Exception:
            continue
    print(f"[log group] could not deliver log: {message[:60]}...")


async def log_to_group(message: str) -> None:
    """Post a log line to the log group in the background; never raises and
    never delays the calling handler (group sends can take seconds when the
    ID needs resolving or Telegram is slow)."""
    asyncio.create_task(_log_to_group_bg(message))


async def subscribe_product_stock(product_id: int, user_id: int) -> None:
    """Persist one subscription per buyer/product, including across restarts."""
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO product_stock_subscriptions (product_id, user_id) VALUES (?, ?)",
            (product_id, user_id),
        )
        await db.commit()


async def notify_product_restock() -> None:
    async with get_db() as db:
        # Discard subscriptions to deleted products; hidden products remain pending.
        await db.execute(
            "DELETE FROM product_stock_subscriptions WHERE product_id NOT IN (SELECT id FROM products)"
        )
        products = await (await db.execute(
            "SELECT DISTINCT product_id FROM product_stock_subscriptions"
        )).fetchall()
    for row in products:
        pid = row[0]
        try:
            p = await get_product(pid)
            if not p or not p["active"]:
                continue
            stock = await product_stock(pid)
            if not product_is_manual(p) and stock <= 0:
                continue
            async with get_db() as db:
                subscribers = await (await db.execute(
                    "SELECT user_id FROM product_stock_subscriptions WHERE product_id = ? LIMIT 100",
                    (pid,),
                )).fetchall()
            for subscriber in subscribers:
                uid = subscriber[0]
                try:
                    if not await ensure_user_not_banned(uid):
                        await client.send_message(
                            uid,
                            f"🔔 Back in stock!\n\n{product_emoji(p)}{p['name']}\n\n"
                            "Open the product to check current availability and purchase.",
                            buttons=[[Button.inline("View Product", f"shop_view:{pid}".encode())]],
                        )
                except (errors.UserIsBlockedError, errors.InputUserDeactivatedError):
                    pass  # Unreachable buyers should not be retried forever.
                except errors.FloodWaitError as exc:
                    # Leave subscriptions pending and retry after Telegram's cooldown.
                    await asyncio.sleep(exc.seconds)
                    return
                except Exception:
                    continue  # Temporary send failure: preserve this subscription.
                async with get_db() as db:
                    await db.execute(
                        "DELETE FROM product_stock_subscriptions WHERE product_id = ? AND user_id = ?",
                        (pid, uid),
                    )
                    await db.commit()
        except Exception as exc:
            print(f"Restock notification check failed for product {pid}: {type(exc).__name__}")


async def product_stock_notification_loop() -> None:
    while True:
        try:
            await notify_product_restock()
        except Exception as exc:
            print(f"Restock notification loop failed: {type(exc).__name__}")
        await asyncio.sleep(30)


async def maybe_alert_low_stock(product_id: int, product_name: str) -> None:
    """After a purchase, alert admins if this sale just pushed the product's
    available stock down to the low-stock threshold (or to zero / sold out).

    Fires only on the crossing sale (remaining <= threshold but was above it
    before this purchase) and again when it hits exactly 0, so admins get at
    most one 'running low' ping plus one 'sold out' ping — never a message on
    every purchase below the threshold."""
    remaining = await product_stock(product_id)
    threshold = await get_low_stock_threshold()
    # Each purchase removes exactly one item, so `remaining == threshold` means
    # THIS sale is the one that crossed the line (it was threshold+1 before).
    crossed_threshold = remaining == threshold
    sold_out = remaining == 0
    if not (crossed_threshold or sold_out):
        return
    if sold_out:
        msg = (
            "🚨 Out of Stock\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📦 {product_name} just SOLD OUT — 0 deliverables left.\n"
            "Users can no longer buy it until you add stock."
        )
    else:
        left = "1 deliverable" if remaining == 1 else f"{remaining} deliverables"
        msg = (
            "⚠️ Low Stock\n"
            "━━━━━━━━━━   ━━━━━━━━━\n\n"
            f"📦 {product_name} is running low — only {left} left.\n"
            "Add stock soon so it doesn't sell out."
        )
    await notify_admins(
        msg,
        [[Button.inline("📦 Products", b"adm_products")]],
    )


async def notify_referral_reward(
    referrer_id: int, referred_user_id: int, new_balance: int
):
    """Notify the referrer they earned credits; tell the new user they were referred."""
    await notify_user(
        referrer_id,
        f"Referral Reward\n\n"
        f"A new user joined through your referral link.\n\n"
        f"+{REFERRAL_REWARD_CREDITS} Credits Added\n"
        f"New Balance: {new_balance} Credits\n\n"
        f"Keep sharing your link to earn more.",
    )
    await notify_user(
        referred_user_id,
        f"Welcome!\n\n"
        f"You joined through a referral link and your account is ready.\n"
        f"Starting Credits: {STARTING_CREDITS}\n\n"
        f"Paste any personal wallet address to run your first check.\n"
        f"Use /refer to get your own referral link.",
    )


async def notify_credit_added(user_id: int, amount: int, new_balance: int):
    await notify_user(
        user_id,
        f"Credits Added\n\n"
        f"+{amount} Credits have been added to your account.\n"
        f"New Balance: {new_balance} Credits",
    )


async def notify_credit_removed(user_id: int, amount: int, new_balance: int):
    await notify_user(
        user_id,
        f"Credits Updated\n\n"
        f"{amount} Credits have been removed from your account.\n"
        f"New Balance: {new_balance} Credits",
    )


# ── DB helpers for admin stats ─────────────────────────────────────────────  ───


async def get_total_user_count() -> int:
    async with get_db() as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            row = await cursor.fetchone()
        return row[0] if row else 0


async def get_total_referral_count() -> int:
    async with get_db() as db:
        async with db.execute("SELECT COUNT(*) FROM referrals") as cursor:
            row = await cursor.fetchone()
        return row[0] if row else 0


async def get_all_user_ids() -> list[int]:
    async with get_db() as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            rows = await cursor.fetchall()
        return [r[0] for r in rows]


async def get_full_admin_stats() -> dict:
    """All numbers for the admin Stats page in one DB pass."""
    stats: dict = {}
    async with get_db() as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            stats["users"] = (await c.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(usd_value), 0) FROM deposits"
        ) as c:
            row = await c.fetchone()
            stats["dep_count"], stats["dep_usd"] = row[0], row[1]
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount_inr), 0) FROM upi_requests "
            "WHERE status = 'approved'"
        ) as c:
            row = await c.fetchone()
            stats["upi_count"], stats["upi_inr"] = row[0], row[1]
        async with db.execute(
            "SELECT COUNT(*), "
            "COALESCE(SUM(CASE WHEN currency = 'usd' THEN amount END), 0), "
            "COALESCE(SUM(CASE WHEN currency = 'inr' THEN amount END), 0) "
            "FROM orders WHERE status != 'refunded'"
        ) as c:
            row = await c.fetchone()
            stats["orders"], stats["sales_usd"], stats["sales_inr"] = row
        async with db.execute(
            "SELECT COUNT(*) FROM orders WHERE status = 'refunded'"
        ) as c:
            stats["refunds"] = (await c.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM orders"
        ) as c:
            stats["buyers"] = (await c.fetchone())[0]
        async with db.execute(
            "SELECT COALESCE(SUM(balance_usd), 0), COALESCE(SUM(balance_inr), 0) "
            "FROM users"
        ) as c:
            row = await c.fetchone()
            stats["held_usd"], stats["held_inr"] = row[0], row[1]
        async with db.execute(
            "SELECT COUNT(*) FROM product_items WHERE status = 'available'"
        ) as c:
            stats["stock"] = (await c.fetchone())[0]
        async with db.execute(
            "SELECT user_id, COUNT(*), "
            "COALESCE(SUM(CASE WHEN currency = 'usd' THEN amount END), 0), "
            "COALESCE(SUM(CASE WHEN currency = 'inr' THEN amount END), 0) "
            "FROM orders WHERE status != 'refunded' "
            "GROUP BY user_id "
            "ORDER BY SUM(CASE WHEN currency = 'usd' THEN amount ELSE 0 END) "
            "+ SUM(CASE WHEN currency = 'inr' THEN amount ELSE 0 END) / 90.0 DESC "
            "LIMIT 5"
        ) as c:
            stats["top_buyers"] = await c.fetchall()
    return stats


# Username labels are cosmetic (stats page etc.), so cache them for a few
# minutes — get_entity is a Telegram API round-trip per user otherwise.
_USER_LABEL_CACHE: dict[int, tuple[float, str]] = {}
_USER_LABEL_TTL = 300.0  # seconds


async def format_user_label(uid: int) -> str:
    """`@username` when available, otherwise the numeric ID. Cached 5 min."""
    cached = _USER_LABEL_CACHE.get(uid)
    if cached and (time.monotonic() - cached[0]) < _USER_LABEL_TTL:
        return cached[1]
    try:
        entity = await client.get_entity(uid)
        if getattr(entity, "username", None):
            label = f"@{entity.username}"
        else:
            name = " ".join(
                x for x in [getattr(entity, "first_name", None)] if x
            ).strip()
            label = f"{name} (`{uid}`)" if name else f"`{uid}`"
    except Exception:
        # Don't cache failures for the full TTL — the entity may simply not
        # be resolvable yet; retry on the next request.
        return f"`{uid}`"
    _USER_LABEL_CACHE[uid] = (time.monotonic(), label)
    if len(_USER_LABEL_CACHE) > 2000:
        _USER_LABEL_CACHE.clear()
    return label


# ── /start ───────────────────────────────────   ─────────────────────────────────


# ── Maintenance gate ─────────────────────────────────────────────────────────
# Registered BEFORE every other handler so it runs first. While maintenance
# mode is on, non-admin messages and button taps are answered with a notice
# and StopPropagation prevents any other handler from running.


@client.on(events.NewMessage(incoming=True))
async def maintenance_gate_msg(event):
    # Only gate PRIVATE chats — never reply in groups (e.g. the log group),
    # which would spam every group message during maintenance.
    if MAINTENANCE and event.is_private and event.sender_id not in ADMIN_IDS:
        await event.reply(maintenance_message())
        raise events.StopPropagation


@client.on(events.CallbackQuery)
async def maintenance_gate_cb(event):
    if MAINTENANCE and event.sender_id not in ADMIN_IDS:
        await event.answer(
            "🚧 Bot is under maintenance. Please try again later.", alert=True
        )
        raise events.StopPropagation


@client.on(events.NewMessage(pattern=r"^/start(?:\s+(.+))?$"))
async def start_handler(event):
    user_id = event.sender_id
    user = await get_or_create_user(user_id)
    if user.get("_is_new"):
        await notify_admins(
            f"👤 New User Joined\n   ━━━━━━━━━━━━━━━━━━━━━━\n\nUser ID: `{user_id}`"
        )
    if user_id not in ADMIN_IDS and await is_banned(user_id):
        await event.respond(banned_message())
        return

    # Deep link: /start prod_<id> → open that product's page directly.
    payload = (event.pattern_match.group(1) or "").strip()
    if payload.startswith("prod_"):
        try:
            pid = int(payload[5:])
        except ValueError:
            pid = None
        if pid:
            p = await get_product(pid)
            if p and p["active"]:
                await send_product_page(event, user_id, p)
                return
        await event.respond("😔 That product is no longer available.")

    balance_usd, balance_inr = await get_balances(user_id)
    await send_home_screen(
        event,
        build_home_text(balance_usd, balance_inr),
        user_id in ADMIN_IDS,
    )


# ─    /menu — open the store home ────────────────────────────────────────────────


@client.on(events.NewMessage(pattern=r"^/menu$"))
async def cmd_menu(event):
    user_id = event.sender_id
    if await ensure_user_not_banned(user_id) and user_id not in ADMIN_IDS:
        await event.respond(banned_message())
        return
    clear_deposit_state(user_id)
    balance_usd, balance_inr = await get_balances(user_id)
    await send_home_screen(
        event,
        build_home_text(balance_usd, balance_inr),
        user_id in ADMIN_IDS,
    )


# ── Inline button (callback) handler ─────────────────  ─────────────────────────

# Telegram callback queries expire after ~10s (or when answered twice, e.g. a
# double-tap). Answering an expired query raises QueryIdInvalidError inside a
# fire-and-forget task, spamming "Task exception was never retrieved" in the
# logs. Patch answer() once so expired/duplicate answers are silently ignored.
_orig_cb_answer = events.CallbackQuery.Event.answer


async def _safe_cb_answer(self, *args, **kwargs):
    # An explicit answer (e.g. an alert popup) must win over the automatic
    # spinner-clearing task scheduled in on_callback — cancel it first.
    spinner = getattr(self, "_spinner_task", None)
    if spinner is not None and spinner is not asyncio.current_task():
        spinner.cancel()
    try:
        return await _orig_cb_answer(self, *args, **kwargs)
    except errors.rpcerrorlist.QueryIdInvalidError:
        return None


events.CallbackQuery.Event.answer = _safe_cb_answer


@client.on(events.CallbackQuery)
async def on_callback(event):
    user_id = event.sender_id
    data = event.data.decode()

    # Ensure the user exists + banned check in one cached step (admins are
    # never banned). This runs on every tap, so it must be fast.
    if await ensure_user_not_banned(user_id) and user_id not in ADMIN_IDS:
        await event.answer("Your access has been suspended.", alert=True)
        return

    # Clear the button loading spinner quickly so taps feel instant. Without
    # this, the spinner runs until the full handler (DB reads + banner edit)
    # finishes, which makes the bot feel slow. A short grace period lets
    # handlers that pop an alert (event.answer(..., alert=True)) win the race
    # and still display their popup; the patched answer() cancels this task.
    async def _clear_spinner():
        await asyncio.sleep(0.15)
        await event.answer()

    event._spinner_task = asyncio.create_task(_clear_spinner())

    async def show(text, buttons, banner: str | None = None):
        # Media handling: product pages show product images, screens that
        # pass a banner show that banner, and everything else is TEXT-ONLY
        # (the brand image belongs to the /start home screen only).
        key = (event.chat_id, event.message_id)
        current = "product" if key in PRODUCT_IMAGE_MSGS else SCREEN_BANNER_MSGS.get(key)
        if banner is None:
            if current == "__text__":
                # Already a text-only message — plain edit below.
                pass
            else:
                # Message currently has media (product image, screen banner,
                # or the start banner). Telegram can't edit media away.
                # First try a plain edit — if the message is actually
                # text-only (e.g. trackers were emptied by a restart), this
                # succeeds with NO flicker at all.
                try:
                    await event.edit(text, buttons=buttons)
                    SCREEN_BANNER_MSGS[key] = "__text__"
                    PRODUCT_IMAGE_MSGS.discard(key)
                    return
                except errors.MessageNotModifiedError:
                    SCREEN_BANNER_MSGS[key] = "__text__"
                    return
                except Exception:
                    pass
                # Really has media → send the NEW message FIRST, then delete
                # the old one, so the user never sees an empty gap while the
                # screen switches (the old "delete then send" order made the
                # message visibly disappear before the next one arrived).
                msg = await event.respond(text, buttons=buttons)
                SCREEN_BANNER_MSGS[(event.chat_id, msg.id)] = "__text__"
                PRODUCT_IMAGE_MSGS.discard(key)
                SCREEN_BANNER_MSGS.pop(key, None)
                try:
                    await event.delete()
                except Exception:
                    pass
                return
            try:
                await event.edit(text, buttons=buttons)
            except errors.MessageNotModifiedError:
                pass
            except Exception:
                msg = await event.respond(text, buttons=buttons)
                SCREEN_BANNER_MSGS[(event.chat_id, msg.id)] = "__text__"
            return
        if current != banner:
            target = get_screen_banner(banner)
            file = cached_banner_file(target)
            try:
                msg = await event.edit(text, buttons=buttons, file=file)
                remember_banner_upload(target, msg)
                PRODUCT_IMAGE_MSGS.discard(key)
                if banner:
                    SCREEN_BANNER_MSGS[key] = banner
                else:
                    SCREEN_BANNER_MSGS.pop(key, None)
                return
            except errors.MessageNotModifiedError:
                return
            except Exception:
                # A stale cached photo reference may have been rejected —
                # drop it and retry once with a fresh upload from disk.
                if file is not target:
                    forget_banner_upload(target)
                    try:
                        msg = await event.edit(text, buttons=buttons, file=target)
                        remember_banner_upload(target, msg)
                        PRODUCT_IMAGE_MSGS.discard(key)
                        if banner:
                            SCREEN_BANNER_MSGS[key] = banner
                        else:
                            SCREEN_BANNER_MSGS.pop(key, None)
                        return
                    except Exception:
                        pass
                # fall through to a normal caption-only edit
        try:
            await event.edit(text, buttons=buttons)
            PRODUCT_IMAGE_MSGS.discard(key)
        except Exception:
            await event.respond(text, buttons=buttons)

    supplier_token = None
    if data.startswith(("wc:choose:", "wc:quote:", "wc:quantity:")):
        parts = data.split(":")
        prefix = {"choose": "shop_buy", "quote": "shop_qty", "quantity": "shop_qtyc"}[parts[1]]
        data = ":".join([prefix, *parts[2:]])
    if data.startswith("wc:confirm:"):
        if not event.is_private:
            await event.answer("Open a private chat with the bot.", alert=True)
            return
        supplier_token = data.split(":", 2)[2]
        checkout = await SUPPLIERS.one(
            "SELECT * FROM wholesale_checkouts WHERE token=? AND user_id=?",
            (supplier_token, user_id))
        if not checkout:
            await event.answer("This checkout is unavailable. Open the product again.", alert=True)
            return
        data = f"shop_ok:{checkout['product_id']}:{checkout['currency']}:{checkout['quantity']}"
    if data.startswith(("wa:", "wc:")):
        await SUPPLIER_PANEL.callback(event, data)
        return
    SUPPLIER_PANEL.activation_inputs.pop(user_id, None)

    if data == "menu":
        balance_usd, balance_inr = await get_balances(user_id)
        clear_deposit_state(user_id)
        await send_home_screen(
            event,
            build_home_text(balance_usd, balance_inr),
            user_id in ADMIN_IDS,
            edit=True,
        )

    elif data == "balance":
        balance_usd, balance_inr = await get_balances(user_id)
        await show(
            build_balance_text(balance_usd, balance_inr),
            [
                [btn_deposit()],
                [btn_dep_history()],
                back_home_row(b"menu"),
            ],
            banner="wallet",
        )

    elif data == "shop" or data.startswith("shop_cat:"):
        clear_deposit_state(user_id)
        products = await list_active_products()
        if not products:
            await show(
                "🛒  Shop\n"
                "━━━━━━━━━━━━━━━━━━━━━━   \n\n"
                "**No products are available right now. Check back soon!**",
                [
                    [btn_orders()],
                    [btn_back()],
                ],
            )
        else:
            await show(
                await build_shop_text(user_id),
                await build_shop_rows(),
            )

    elif data.startswith(("stock_empty:", "stock_notify:")):
        if not event.is_private:
            await event.answer("Open a private chat with the bot.", alert=True)
            return
        try:
            pid = int(data.split(":", 1)[1])
        except ValueError:
            await event.answer("Invalid product.", alert=True)
            return
        p = await get_product(pid)
        if not p or not p["active"]:
            await event.answer("That product is no longer available.", alert=True)
            return
        if product_is_manual(p) or await product_stock(pid) > 0:
            await send_product_page(event, user_id, p, edit=True)
            await event.answer("This product is back in stock.", alert=True)
            return
        if data.startswith("stock_empty:"):
            await event.answer("This product is currently out of stock.", alert=True)
            return
        await subscribe_product_stock(pid, user_id)
        await send_product_page(event, user_id, p, edit=True)
        await event.respond(
            "✅ Done! I'll notify you as soon as this product is back in stock."
        )

    elif data.startswith("shop_view:"):
        # Leaving any half-finished flow (incl. the manual-email prompt this is the
        # cancel target for) so a later message isn't misrouted as an email.
        clear_deposit_state(user_id)
        try:
            pid = int(data.split(":", 1)[1])
        except ValueError:
            await event.answer("Invalid product.", alert=True)
            return
        p = await get_product(pid)
        if not p or not p["active"]:
            await event.answer("That product is no longer available.", alert=True)
            await show(await build_shop_text(user_id), await build_shop_rows())
            return
        await send_product_page(event, user_id, p, edit=True)

    elif data.startswith("shop_buy:") or data.startswith("shop_qty:"):
        # shop_buy:{pid}:{cur}        → quantity picker (instant, stock > 1)
        #                               or straight to confirm (manual / 1 left)
        # shop_qty:{pid}:{cur}:{qty}  → confirm screen for that quantity
        parts = data.split(":")
        if len(parts) not in (3, 4) or parts[2] not in ("usd", "inr"):
            await event.answer("Invalid selection.", alert=True)
            return
        try:
            pid = int(parts[1])
            qty = int(parts[3]) if len(parts) == 4 else 1
        except ValueError:
            await event.answer("Invalid product.", alert=True)
            return
        if qty < 1:
            await event.answer("Invalid quantity.", alert=True)
            return
        cur = parts[2]
        p = await get_product(pid)
        if not p or not p["active"]:
            await event.answer("That product is no longer available.", alert=True)
            return
        price = p["price_usd"] if cur == "usd" else p["price_inr"]
        if not price or price <= 0:
            await event.answer("Not sold in that currency.", alert=True)
            return
        sym = "$" if cur == "usd" else "₹"
        is_manual = product_is_manual(p)
        stock = 1 if is_manual else await product_stock(pid)

        if data.startswith("shop_buy:") and not is_manual and stock > 1:
            # Quantity picker: how many does the buyer want?
            # Text-only screen (no banner) — replace the media message with a
            # fresh plain-text message, since Telegram can't edit media away.
            AWAITING_QTY.pop(user_id, None)
            qty_row = [
                Button.inline(
                    str(q),
                    f"shop_qty:{pid}:{cur}:{q}".encode(),
                    style="primary",
                )
                for q in (1, 5, 10)
            ]
            qty_text = (
                "**➕  Custom Quantity**\n"
                "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
                f"{product_emoji(p)}**Product: {p['name']}**\n\n"
                f"**Price each: {sym}{price:.2f}**\n\n"
                f"**🙌 Available Stocks: {stock}**\n\n"
                "**Type how many you want ( 1–20 )**"
            )
            qty_buttons = [
                qty_row,
                [
                    Button.inline(
                        "✏️ Custom Quantity",
                        f"shop_qtyc:{pid}:{cur}".encode(),
                        style="primary",
                    )
                ],
                [
                    Button.inline(
                        "Cancel",
                        f"shop_view:{pid}".encode(),
                        icon=PREMIUM_EMOJI_MAP.get("⬅️"),
                    )
                ],
            ]
            try:
                # Send the new screen FIRST, then delete the old message —
                # the reverse order made the old message visibly vanish
                # before the new one arrived (ugly flicker on Buy).
                await event.respond(qty_text, buttons=qty_buttons)
                PRODUCT_IMAGE_MSGS.discard((event.chat_id, event.message_id))
                SCREEN_BANNER_MSGS.pop((event.chat_id, event.message_id), None)
                await event.delete()
            except Exception:
                await show(qty_text, qty_buttons)
            return

        # Confirm screen (manual products and explicit shop_qty picks).
        if not is_manual and qty > stock:
            await event.answer(f"Only {stock} left in stock.", alert=True)
            return
        total = round(price * qty, 6 if cur == "usd" else 2)
        try:
            confirm_data = await purchase_confirmation_data(user_id, p, cur, qty)
        except (ValueError, SupplierError) as exc:
            await event.answer(str(exc), alert=True)
            return
        if is_manual:
            deliver_line = (
                f"This will be deducted from your {sym} balance. You'll enter "
                "your email next, and the invite arrives within 1 hour."
            )
        else:
            items_word = "items" if qty > 1 else "item"
            deliver_line = (
                f"This will be deducted from your {sym} balance and the "
                f"{items_word} will be delivered instantly."
            )
        if p.get("supplier_id"):
            deliver_line = (
                f"This will be deducted from your {sym} balance. "
                "Delivery or activation progress will appear in My Orders."
            )
        await show(
            "[🧾](emoji/5444856076954520455) **Confirm Purchase** "
            "[✅](emoji/5206607081334906820)\n"
            "**━━━━━━━━━━━━━  ━━━━━━━━━**\n\n"
            f"{product_emoji(p)}**Product: {p['name']}**\n"
            f"[➕](emoji/6025873488911274008) **Quantity: {qty} × {sym}{price:.2f}**\n\n"
            f"[💸](emoji/5350658940117986785) **Total Price: {sym}{total:.2f}**\n\n"
            f"[🖊](emoji/5985774024968379294) **Note: {deliver_line}**\n\n"
            "**Click Confirm to Continue** [👇](emoji/5228931982037116397)",
            [
                [
                    Button.inline(
                        f"✅ Confirm — pay {sym}{total:.2f}",
                        confirm_data,
                    )
                ],
                [
                    Button.inline(
                        "Cancel",
                        f"shop_view:{pid}".encode(),
                        icon=PREMIUM_EMOJI_MAP.get("⬅️"),
                    )
                ],
            ],
        )

    elif data.startswith("shop_qtyc:"):
        # Green "Custom Quantity" button: ask the buyer to type a number.
        parts = data.split(":")
        if len(parts) != 3 or parts[2] not in ("usd", "inr"):
            await event.answer("Invalid selection.", alert=True)
            return
        try:
            pid = int(parts[1])
        except ValueError:
            await event.answer("Invalid product.", alert=True)
            return
        cur = parts[2]
        p = await get_product(pid)
        if not p or not p["active"]:
            await event.answer("That product is no longer available.", alert=True)
            return
        stock = await product_stock(pid)
        if stock <= 0:
            await event.answer("Out of stock.", alert=True)
            return
        AWAITING_QTY[user_id] = {"pid": pid, "cur": cur}
        max_qty = min(stock, 99)
        await show(
            "✏️  Custom Quantity\n"
            "━━━━  ━━━━━━━━━━━━━━━━━━\n\n"
            f"{product_emoji(p)}Product: {p['name']}\n"
            f"In stock: {stock}\n\n"
            f"Type how many you want (1–{max_qty}).\n"
            "Example: 7",
            [
                [
                    Button.inline(
                        "Cancel",
                        f"shop_view:{pid}".encode(),
                        icon=PREMIUM_EMOJI_MAP.get("⬅️"),
                    )
                ],
            ],
        )

    elif data.startswith("shop_ok:"):
        parts = data.split(":")
        if len(parts) not in (3, 4) or parts[2] not in ("usd", "inr"):
            await event.answer("Invalid selection.", alert=True)
            return
        try:
            pid = int(parts[1])
            qty = int(parts[3]) if len(parts) == 4 else 1
        except ValueError:
            await event.answer("Invalid product.", alert=True)
            return
        if qty < 1:
            await event.answer("Invalid quantity.", alert=True)
            return
        cur = parts[2]
        sym = "$" if cur == "usd" else "₹"
        p = await get_product(pid)
        if p and p.get("supplier_id") and supplier_token is None:
            await send_product_page(event, user_id, p, edit=True)
            return
        status, payload = await buy_product(user_id, pid, cur, qty, supplier_token=supplier_token)
        if status == "supplier_order":
            await show(*(await SUPPLIER_PANEL.order_view(payload, user_id)))
            return
        if status == "supplier_error":
            await show(payload, [[btn_deposit()], back_home_row(f"shop_view:{pid}".encode())])
            return
        if status == "ok":
            rows = [
                [Button.inline("🧾 My Orders", b"orders")],
                [Button.inline("🏠 Home to Shop", b"shop")],
            ]
            howto_line = ""
            if await product_howto_by_id(pid):
                # Guide button goes on top — with the premium 📖 emoji icon.
                rows.insert(
                    0,
                    [
                        Button.inline(
                            "How to Use",
                            f"howto:{pid}".encode(),
                            icon=PREMIUM_EMOJI_MAP.get("📖"),
                        )
                    ],
                )
                howto_line = "📖 New to this product? Tap How to Use below.\n"
            bought_qty = payload.get("qty", 1)
            item_word = "Your items:" if bought_qty > 1 else "Your item:"
            saved_word = "They're" if bought_qty > 1 else "It's"
            qty_line = (
                f"[➕](emoji/6025873488911274008) **Quantity: {bought_qty}**\n\n"
                if bought_qty > 1
                else ""
            )
            await show(
                "[✅](emoji/5181903361456210670) **PURCHASE COMPLETE**\n"
                "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
                f"[📦](emoji/5884479287171485878) **Product: {payload['name']}**\n\n"
                f"{qty_line}"
                f"[✔️](emoji/5278411813468269386) **Paid: {sym}{payload['amount']:.2f}**\n"
                "\n"
                f"[💳](emoji/6143398710551252398) **New Balance: "
                f"{sym}{payload['new_balance']:.2f}**\n"
                "**━━━━━━━━━━━━━━━━━━━━━━━**\n"
                f"[🛍](emoji/5431597233738883049) **{item_word}**\n\n"
                f"`{payload['content']}`\n\n"
                f"{howto_line}"
                f"**{saved_word} also saved under** "
                f"[🧾](emoji/5444856076954520455) **My Orders.**",
                rows,
            )
            await maybe_alert_low_stock(pid, payload["name"])
            await notify_admins(
                "🛒 New Purchase (balance)\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"User: `{user_id}`\n"
                f"Product: {payload['name']}\n"
                f"Quantity: {payload.get('qty', 1)}\n"
                f"Paid: {sym}{payload['amount']:.2f}\n"
                f"User's new balance: {sym}{payload['new_balance']:.2f}"
            )
            await log_to_group(
                "🛒 New Purchase\n"
                f"User ID: {user_id}\n"
                f"Product: {payload['name']}\n"
                "Payment: Wallet Balance\n"
                f"Amount / Quantity: {sym}{payload['amount']:.2f} / "
                f"{payload.get('qty', 1)}\n"
                f"Time: {ist_now_str()}"
            )
        elif status == "ok_manual":
            # Manual product: funds already deducted, order awaiting the buyer's email.
            AWAITING_EMAIL.add(user_id)
            PENDING_EMAIL_ORDER[user_id] = payload["order_id"]
            await show(
                "✅  Payment Received\n"
                "━━━  ━━━━━━━━━━━━━━━━━━━\n\n"
                f"Product: {payload['name']}\n"
                f"Paid: {sym}{payload['amount']:.2f}\n"
                f"New {sym} balance: {sym}{payload['new_balance']:.2f}\n\n"
                "📧 Now send the **email address** where you want the invite "
                "delivered.\n\nType it below 👇",
                [[Button.inline("✖️ Cancel", f"shop_view:{pid}".encode())]],
            )
            await maybe_alert_low_stock(pid, payload["name"])
            await notify_admins(
                "🛒 New Purchase (manual delivery — awaiting buyer email)\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"User: `{user_id}`\n"
                f"Product: {payload['name']}\n"
                f"Paid: {sym}{payload['amount']:.2f}\n"
                f"Order: #{payload['order_id']}\n\n"
                "You'll get another alert once they submit their email."
            )
            await log_to_group(
                "🛒 New Purchase\n"
                f"User ID: {user_id}\n"
                f"Product: {payload['name']}\n"
                "Payment: Wallet Balance (manual delivery)\n"
                f"Amount / Quantity: {sym}{payload['amount']:.2f} / 1\n"
                f"Time: {ist_now_str()}"
            )
        elif status == "insufficient":
            need = payload
            await show(
                "❌  Not Enough Balance\n"
                "━━  ━━━   ━━━━━━━━━━━━━\n\n"
                f"This item costs {sym}{need:.2f} but your {sym} balance is too low.\n\n"
                "Top up and try again.",
                [
                    [Button.inline("Deposit USDT", b"deposit", icon=5445353829304387411)],
                    [Button.inline("🇮🇳 Deposit UPI (  )", b"deposit_upi")],
                    [Button.inline("🏠 Home to Shop", b"shop")],
                ],
            )
        elif status == "out_of_stock":
            left = payload if isinstance(payload, int) else 0
            if qty > 1 and left > 0:
                msg = (
                    f"Sorry, only {left} left in stock — not enough for your "
                    f"quantity of {qty}. No charge was made."
                )
            else:
                msg = "Sorry, this product just sold out. No charge was made."
            await show(
                "😔  Sold Out\n"
                "━━━━━━━━━━━━━━━━━━━   \n\n" + msg,
                [
                    [Button.inline("🛒 View Product", f"shop_view:{pid}".encode())],
                    [Button.inline("🏠 Home to Shop", b"shop")],
                ],
            )
        elif status == "no_price":
            await event.answer("Not sold in that currency.", alert=True)
        else:  # gone
            await event.answer("That product is no longer available.", alert=True)
            await show(await build_shop_text(user_id), await build_shop_rows())

    elif data == "orders":
        orders = await get_user_orders(user_id, 20)
        if not orders:
            await show(
                "🧾  My Orders\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "You haven't bought anything yet.\n"
                "Browse the shop to grab your first item!    ",
                [
                    [btn_dep_history()],
                    [btn_back()],
                ],
                banner="orders",
            )
        else:
            # Group orders by product category — same model as the shop:
            # categories first, then the orders inside the tapped category.
            by_cat: dict[int, list] = {}
            for o in orders:
                by_cat.setdefault(o.get("category_id") or 0, []).append(o)
            n = len(orders)
            lines = [
                "🧾  My Orders",
                "━━━━━━━━━━━━━━━━━━━━━━━",
                "",
                f"You have {n} order{'s' if n != 1 else ''}. "
                "Pick a category to view orders 👇",
            ]
            rows = []
            for c in await list_categories():
                group = by_cat.get(c["id"])
                if not group:
                    continue
                rows.append(
                    [
                        Button.inline(
                            (
                                f"{c['name']} ({len(group)})"
                                if c.get("emoji_id")
                                else f"{cat_emoji(c)} {c['name']} ({len(group)})"
                            ),
                            f"ord_cat:{c['id']}".encode(),
                            style="primary",
                            icon=c.get("emoji_id"),
                        )
                    ]
                )
            other = by_cat.get(0)
            if other:
                rows.append(
                    [
                        Button.inline(
                            f"🗂 Other ({len(other)})",
                            b"ord_cat:0",
                            style="primary",
                        )
                    ]
                )
            rows.append([btn_dep_history()])
            rows.append([btn_back()])
            await show("\n".join(lines), rows, banner="orders")

    elif data.startswith("ord_cat:"):
        # Orders inside one category (0 = uncategorized / removed products).
        try:
            cid = int(data.split(":", 1)[1])
        except ValueError:
            await event.answer("Invalid category.", alert=True)
            return
        if cid == 0:
            cat_label = "🗂 Other"
        else:
            cat = await get_category(cid)
            cat_label = f"{cat_emoji(cat)} {cat['name']}" if cat else "🗂 Other"
        orders = await get_user_orders(user_id, 20)
        group = [o for o in orders if (o.get("category_id") or 0) == cid]
        if not group:
            await event.answer("No orders in this category.", alert=True)
            return

        def _order_icon(o):
            if o["status"] == "refunded":
                return "↩️"
            if o["status"] in ("supplier_pending", "supplier_activation"):
                return "🕐"
            if (o.get("fulfillment") or "instant") == "manual":
                return {
                    "awaiting_email": "✉️",
                    "processing": "🕐",
                    "delivered": "📬",
                    "completed": "✅",
                }.get(o["status"], "🕐")
            return "  "

        n = len(group)
        lines = [
            f"      My Orders — {cat_label}",
            "━━━━━━━━  ━━━━━━━━━━━━━━",
            "",
            f"{n} order{'s' if n != 1 else ''} in this category. "
            "Tap one to view details 👇",
        ]
        rows = []
        for o in group:
            sym = "$" if o["currency"] == "usd" else "₹"
            name = (o.get("product_name") or "(removed)")[:22]
            rows.append(
                [
                    Button.inline(
                        f"{_order_icon(o)} {name} · {sym}{o['amount']:.2f}",
                        f"myord:{o['id']}".encode(),
                    )
                ]
            )
        rows.append([Button.inline("↩️ All Categories", b"orders")])
        rows.append([btn_back()])
        await show("\n".join(lines), rows, banner="orders")

    elif data == "dep_history":
        await show(
            "🍃  **Transaction History**\n"
            "━━━━━━━   ━━━━━━━━━━━━━━━\n\n"
            "Choose what you'd like to view 👇\n\n"
            "**Top-Ups** — funds added to your wallet.\n"
            "**Purchases** — products bought from the shop.",
            [
                [
                    Button.inline(
                        "Top-Ups",
                        b"txn_topups",
                        icon=5397916757333654639,
                        style="primary",
                    )
                ],
                [
                    Button.inline(
                        "Purchases",
                        b"txn_purchases",
                        icon=5224289685030839993,
                        style="primary",
                    )
                ],
                [btn_orders()],
                [btn_back()],
            ],
        )

    elif data == "txn_topups":
        deps = await get_user_deposits(user_id, 20)
        if not deps:
            await show(
                "🍃  **Top-Ups**\n"
                "━━━━━━━━━━━━━━━━━  ━━━━   \n\n"
                "You haven't made any deposits yet.\n"
                "Top up your wallet to get started! 💰",
                [
                    [btn_deposit()],
                    [Button.inline("↩️ Transaction History", b"dep_history")],
                    [btn_back()],
                ],
            )
        else:
            n = len(deps)
            lines = [
                "🍃  **Top-Ups**",
                "━━━━━━━━━━━━━━━━━━━━━━━",
                "",
                f"Your last {n} deposit{'s' if n != 1 else ''} 👇",
                "",
            ]
            for d in deps:
                sym = "$" if d["currency"] == "usd" else "₹"
                ts = (d["created_at"] or "")[:16].replace("T", " ")
                token = (d["token"] or "").upper()
                lines.append(f"✅ {token} · {sym}{d['amount']:.2f}  —  {ts}")
            await show(
                "\n".join(lines),
                [
                    [btn_deposit()],
                    [Button.inline("↩️ Transaction History", b"dep_history")],
                    [btn_back()],
                ],
            )

    elif data == "txn_purchases":
        purchases = await get_user_orders(user_id, 20)
        if not purchases:
            await show(
                "🍃  **Purchases**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "You haven't bought anything yet.\n"
                "Browse the shop to grab your first item! 🛍",
                [
                    [Button.inline("↩️ Transaction History", b"dep_history")],
                    [btn_back()],
                ],
            )
        else:
            n = len(purchases)
            lines = [
                "🍃  **Purchases**",
                "━━━━━━━━━━━━━━━━━━━━━━━",
                "",
                f"Your last {n} purchase{'s' if n != 1 else ''} 👇",
                "",
            ]
            for o in purchases:
                sym = "$" if o["currency"] == "usd" else "₹"
                ts = (o.get("created_at") or "")[:16].replace("T", " ")
                name = (o.get("product_name") or "(removed)")[:22]
                icon = "↩️" if o["status"] == "refunded" else "✅"
                lines.append(f"{icon} {name} · {sym}{o['amount']:.2f}  —  {ts}")
            await show(
                "\n".join(lines),
                [
                    [btn_orders()],
                    [Button.inline("↩️ Transaction History", b"dep_history")],
                    [btn_back()],
                ],
            )

    elif data == "deposit_menu":
        clear_deposit_state(user_id)
        await show(
            "**😜 Deposit Funds**\n"
            "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
            "**Choose how you'd like to add funds to your wallet:**\n\n"
            "**🙊 Crypto - USD balance.**\n\n"
            "**🫥 UPI - INR balance.**",
            deposit_menu_buttons(),
            banner="deposit",
        )

    elif data == "deposit":
        # CRYPTO screen: lists supported coins. USDT is the only coin for now,
        # shown with its tether premium-emoji icon.
        clear_deposit_state(user_id)
        if not DEPOSIT_ADDRESS:
            await show(build_deposit_text(), deposit_buttons())
        else:
            await show(
                "**   Crypto Deposit**\n"
                "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
                "**Choose the coin you want to pay with:**",
                [
                    [
                        Button.inline(
                            "USDT",
                            b"dep_usdt",
                            icon=USDT_ICON_ID,
                            style="primary",
                        )
                    ],
                    [
                        Button.inline(
                            "GRAM",
                            b"dep_chain:ton",
                            icon=GRAM_ICON_ID,
                            style="primary",
                        )
                    ],
                    [
                        Button.inline(
                            "LTC",
                            b"dep_chain:ltc",
                            icon=LTC_ICON_ID,
                            style="primary",
                        )
                    ],
                    [
                        Button.inline(
                            "BTC",
                            b"dep_chain:btc",
                            icon=BTC_ICON_ID,
                            style="primary",
                        )
                    ],
                    back_home_row(b"deposit_menu"),
                ],
                banner="crypto",
            )

    elif data == "dep_usdt":
        # USDT screen: the admin-configured deposit addresses, one button per
        # network. Tapping a network continues the normal deposit flow.
        clear_deposit_state(user_id)
        # Per-network premium emoji icons (blue buttons).
        network_icons = {
            "bsc": 5814447916969890525,
            "polygon": 5377753552326254071,
            "tron": 6213214271531126888,
        }
        rows = [
            [
                Button.inline(
                    DEPOSIT_TOKENS[c]["label"],
                    f"dep_chain:{c}".encode(),
                    icon=network_icons.get(c),
                    style="primary",
                )
            ]
            for c in DEPOSIT_CHAINS
        ]
        rows.append(back_home_row(b"deposit"))
        await show(
            "**💵  USDT — Choose Network**\n"
            "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
            "**Choose the network you'll use to send your USDT payment.**\n\n"
            "**Need assistance?\n Contact Support before making your payment 👇\n\n**"
            f"**❓ Need help? Contact {ADMIN_CONTACT}.**",
            rows,
            banner="usdt",
        )

    elif data.startswith("dep_chain:"):
        chain = data.split(":", 1)[1]
        if chain not in DEPOSIT_TOKENS:
            await event.answer("Unknown network.", alert=True)
            return
        AWAITING_TX.discard(user_id)
        AWAITING_AMOUNT.discard(user_id)
        PENDING_AMOUNT.pop(user_id, None)
        DEPOSIT_CHAIN_CHOICE[user_id] = chain
        label = DEPOSIT_TOKENS[chain]["label"]
        # Coin name + its premium emoji (same icons as the coin list buttons).
        _coin_icons = {
            "ton": ("GRAM", GRAM_ICON_ID),
            "ltc": ("LTC", LTC_ICON_ID),
            "btc": ("BTC", BTC_ICON_ID),
        }
        coin_name, coin_icon = _coin_icons.get(chain, ("USDT", USDT_ICON_ID))
        await show(
            f"[💎](emoji/{coin_icon}) **{coin_name}**\n\n"
            "**Make a Deposit to your wallet**\n\n"
            "**Choose an amount (USD):**\n\n"
            f"**Network: {label}**",
            deposit_amount_buttons(),
            banner="coin",
        )

    elif data.startswith("dep_amt:"):
        try:
            amount = float(data.split(":", 1)[1])
        except ValueError:
            await event.answer("Bad amount.", alert=True)
            return
        if amount + 1e-9 < MIN_DEPOSIT_USD:
            await event.answer(
                f"Minimum deposit is ${MIN_DEPOSIT_USD:.2f}.", alert=True
            )
            return
        AWAITING_AMOUNT.discard(user_id)
        chain = DEPOSIT_CHAIN_CHOICE.get(user_id, DEPOSIT_CHAINS[0])
        # Native coins (TON, LTC): the user picks a USD amount but pays in the
        # coin — convert at the live price so the reserved amount is in-coin.
        _kind = (DEPOSIT_TOKENS.get(chain) or {}).get("kind")
        usd_amt = amount if _kind in ("ton", "ltc", "btc") else None
        if _kind in ("ton", "ltc", "btc"):
            try:
                price = await get_coin_price_usd(_kind)
            except DepositError as e:
                await event.answer(str(e), alert=True)
                return
            amount = amount / price
        intent = await create_deposit_intent(user_id, amount, chain, usd_amount=usd_amt)
        if intent is None:
            await show(
                "⚠️ Too many deposits are in progress right now. Please try again in "
                "a few minutes.",
                [
                    [Button.inline("💵 Try Again", b"deposit")],
                    [btn_back()],
                ],
            )
            return
        await show(
            build_deposit_text(intent), deposit_buttons(), banner=f"qr_{chain}"
        )

    elif data.startswith("paywallet:"):
        # "Pay with Wallet": choose which wallet balance to pay with.
        try:
            pid = int(data.split(":", 1)[1])
        except ValueError:
            await event.answer("Bad product.", alert=True)
            return
        p = await get_product(pid)
        if not p or not p.get("active"):
            await event.answer("This product is no longer available.", alert=True)
            return
        if not product_is_manual(p) and await product_stock(pid) <= 0:
            await event.answer("This product is out of stock.", alert=True)
            return
        price_usd = p["price_usd"] or 0
        price_inr = p["price_inr"] or 0
        balance_usd, balance_inr = await get_balances(user_id)
        can_usd = price_usd > 0 and balance_usd >= price_usd
        can_inr = price_inr > 0 and balance_inr >= price_inr
        rows = []
        if can_usd:
            rows.append(
                [
                    Button.inline(
                        "USD Balance",
                        f"shop_buy:{pid}:usd".encode(),
                        style="success",
                        icon=5409048419211682843,
                    )
                ]
            )
        if can_inr:
            rows.append(
                [
                    Button.inline(
                        "INR Balance",
                        f"shop_buy:{pid}:inr".encode(),
                        style="success",
                        icon=6138853050309150669,
                    )
                ]
            )
        rows.append(
            [
                Button.inline(
                    "Deposit into Wallet",
                    b"deposit_menu",
                    style="primary",
                    icon=4994863664633741922,
                )
            ]
        )
        rows.append(back_home_row(f"shop_view:{pid}".encode()))
        await show(
            "**Choose which wallet balance you'd like to use for payment:**\n\n"
            f"[💵](emoji/5190866142159199158) **USD Balance: "
            f"${balance_usd:.2f}**\n\n"
            f"[🇮🇳](emoji/6147530606528959460) **INR Balance: "
            f"₹{balance_inr:.2f}**",
            rows,
        )

    elif data.startswith("paycrypto:"):
        # paycrypto:{pid}       → quantity picker (instant, stock > 1)
        #                         or straight to the coin picker (qty=1)
        # paycrypto:{pid}:{qty} → coin picker for that quantity
        parts = data.split(":")
        try:
            pid = int(parts[1])
            qty = int(parts[2]) if len(parts) == 3 else 0
        except (ValueError, IndexError):
            await event.answer("Bad product.", alert=True)
            return
        p = await get_product(pid)
        if not p or not p.get("active"):
            await event.answer("This product is no longer available.", alert=True)
            return
        is_manual = product_is_manual(p)
        stock = 1 if is_manual else await product_stock(pid)
        price_usd = p["price_usd"] or 0
        if qty <= 0:
            if not is_manual and stock > 1:
                # Quantity picker — same as the normal shop flow.
                AWAITING_QTY.pop(user_id, None)
                qty_row = [
                    Button.inline(
                        str(q),
                        f"paycrypto:{pid}:{q}".encode(),
                        style="primary",
                    )
                    for q in (1, 5, 10)
                ]
                qty_text = (
                    "**➕  Custom Quantity**\n"
                    "**━━━━━━━━━━━   ━━━━━━━━━━━**\n\n"
                    f"{product_emoji(p)}**Product: {p['name']}**\n\n"
                    f"**Price each: ${price_usd:.2f}**\n\n"
                    f"**🙌 Available Stocks: {stock}**\n\n"
                    "**How many do you want?**"
                )
                qty_buttons = [
                    qty_row,
                    [
                        Button.inline(
                            "✏️ Custom Quantity",
                            f"paycqtyc:{pid}".encode(),
                            style="primary",
                        )
                    ],
                    [
                        Button.inline(
                            "Cancel",
                            f"shop_view:{pid}".encode(),
                            icon=PREMIUM_EMOJI_MAP.get("⬅️"),
                        )
                    ],
                ]
                try:
                    # New message first, delete after — no visible gap.
                    await event.respond(qty_text, buttons=qty_buttons)
                    PRODUCT_IMAGE_MSGS.discard((event.chat_id, event.message_id))
                    SCREEN_BANNER_MSGS.pop((event.chat_id, event.message_id), None)
                    await event.delete()
                except Exception:
                    await show(qty_text, qty_buttons)
                return
            qty = 1
        if not is_manual and qty > stock:
            await event.answer(f"Only {stock} left in stock.", alert=True)
            return
        total_usd = round(price_usd * qty, 2)
        qty_line = (
            f"**Quantity: {qty} × ${price_usd:.2f} = ${total_usd:.2f}**\n"
            if qty > 1
            else ""
        )
        rows = [
            [
                Button.inline(
                    "USDT",
                    f"buypaynet:{pid}:{qty}".encode(),
                    style="primary",
                    icon=USDT_ICON_ID,
                ),
                Button.inline(
                    "GRAM",
                    f"buypay:{pid}:ton:{qty}".encode(),
                    style="primary",
                    icon=GRAM_ICON_ID,
                ),
            ],
            [
                Button.inline(
                    "LTC",
                    f"buypay:{pid}:ltc:{qty}".encode(),
                    style="primary",
                    icon=LTC_ICON_ID,
                ),
                Button.inline(
                    "BTC",
                    f"buypay:{pid}:btc:{qty}".encode(),
                    style="primary",
                    icon=BTC_ICON_ID,
                ),
            ],
            back_home_row(f"shop_view:{pid}".encode()),
        ]
        await show(
            "**💸  Pay with Crypto — Choose Coin**\n"
            "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
            f"{qty_line}"
            "**Choose the coin you'll use to pay for this product.**\n"
            "Delivery is automatic once your payment confirms.",
            rows,
        )

    elif data.startswith("paycqtyc:"):
        # "Custom Quantity" in the crypto flow: ask the buyer to type a number.
        try:
            pid = int(data.split(":", 1)[1])
        except ValueError:
            await event.answer("Invalid product.", alert=True)
            return
        p = await get_product(pid)
        if not p or not p.get("active"):
            await event.answer("That product is no longer available.", alert=True)
            return
        stock = await product_stock(pid)
        if stock <= 0:
            await event.answer("Out of stock.", alert=True)
            return
        AWAITING_QTY[user_id] = {"pid": pid, "cur": "crypto"}
        max_qty = min(stock, 99)
        await show(
            "✏️  Custom Quantity\n"
            "━━━━  ━━━━━━━━━━━━━━━━━━\n\n"
            f"{product_emoji(p)}Product: {p['name']}\n"
            f"In stock: {stock}\n\n"
            f"Type how many you want (1–{max_qty}).\n"
            "Example: 7",
            [
                [
                    Button.inline(
                        "Cancel",
                        f"shop_view:{pid}".encode(),
                        icon=PREMIUM_EMOJI_MAP.get("⬅️"),
                    )
                ],
            ],
        )

    elif data.startswith("learnmore:"):
        # Buyer opens the product's admin-written Learn More text.
        try:
            pid = int(data.split(":", 1)[1])
        except ValueError:
            await event.answer("Invalid product.", alert=True)
            return
        p = await get_product(pid)
        info = product_learn_more(p)
        if not info:
            await event.answer(
                "No extra info has been added for this product yet.", alert=True
            )
            return
        await show(
            f"{info}",
            [
                [
                    Button.inline(
                        "Back",
                        f"shop_view:{pid}".encode(),
                        icon=PREMIUM_EMOJI_MAP.get("⬅️"),
                    ),
                    Button.inline("🏠 Home", b"shop"),
                ],
            ],
        )

    elif data.startswith("buypaynet:"):
        # Direct pay with USDT: pick which network to send on.
        parts = data.split(":")
        try:
            pid = int(parts[1])
            qty = int(parts[2]) if len(parts) == 3 else 1
        except (ValueError, IndexError):
            await event.answer("Bad product.", alert=True)
            return
        qty = max(1, qty)
        rows = [
            [
                Button.inline(
                    DEPOSIT_TOKENS[c]["label"],
                    f"buypay:{pid}:{c}:{qty}".encode(),
                    style="primary",
                )
            ]
            for c in DEPOSIT_CHAINS
        ]
        rows.append(back_home_row(f"paycrypto:{pid}:{qty}".encode()))
        await show(
            "**💵  Pay with USDT — Choose Network**\n"
            "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
            "**Choose the network you'll use to send your USDT payment.**",
            rows,
            banner="usdt",
        )

    elif data.startswith("buypay:"):
        # Direct "tap and pay": reserve a deposit for the product's USD price
        # (minus any existing USD balance) and auto-deliver once it confirms.
        parts = data.split(":")
        try:
            pid = int(parts[1])
            chain = parts[2]
            qty = int(parts[3]) if len(parts) == 4 else 1
        except (ValueError, IndexError):
            await event.answer("Bad request.", alert=True)
            return
        qty = max(1, min(qty, 99))
        if chain not in DEPOSIT_TOKENS:
            await event.answer("Unknown network.", alert=True)
            return
        p = await get_product(pid)
        if not p or not p.get("active"):
            await event.answer("This product is no longer available.", alert=True)
            return
        unit_usd = p["price_usd"] or 0
        if unit_usd <= 0:
            await event.answer(
                "This product can't be paid in crypto (no USD price).", alert=True
            )
            return
        if product_is_manual(p):
            qty = 1
        elif await product_stock(pid) < qty:
            await event.answer(
                f"Only {await product_stock(pid)} left in stock.", alert=True
            )
            return
        price_usd = round(unit_usd * qty, 2)
        if p.get("supplier_id"):
            try:
                await SUPPLIERS.validate_quote(p, "usd", qty, price_usd)
            except (ValueError, SupplierError) as exc:
                await event.answer(str(exc), alert=True)
                return
        balance_usd = await get_balance_usd(user_id)
        if balance_usd >= price_usd:
            await event.answer(
                "You already have enough balance — use the Buy button!", alert=True
            )
            return
        clear_deposit_state(user_id)
        DEPOSIT_CHAIN_CHOICE[user_id] = chain
        amount = max(price_usd - balance_usd, MIN_DEPOSIT_USD)
        _kind = (DEPOSIT_TOKENS.get(chain) or {}).get("kind")
        _usd_amt = amount if _kind in ("ton", "ltc", "btc") else None
        if _kind in ("ton", "ltc", "btc"):
            try:
                coin_price = await get_coin_price_usd(_kind)
            except DepositError as e:
                await event.answer(str(e), alert=True)
                return
            amount = amount / coin_price
        intent = await create_deposit_intent(user_id, amount, chain, usd_amount=_usd_amt)
        if intent is None:
            await show(
                "⚠️ Too many deposits are in progress right now. Please try again in "
                "a few minutes.",
                [back_home_row(f"shop_view:{pid}".encode())],
            )
            return
        DIRECT_BUY_PENDING[user_id] = {
            "pid": pid,
            "intent_id": intent["id"],
            "qty": qty,
        }
        qty_note = f" × {qty}" if qty > 1 else ""
        await show(
            build_deposit_text(intent)
            + f"\n\n⚡ **{p['name']}{qty_note}** will be delivered automatically "
            "as soon as your payment is confirmed.",
            deposit_buttons(back_to=f"paycrypto:{pid}:{qty}".encode()),
            banner=f"qr_{chain}",
        )

    elif data == "dep_custom":
        chain = DEPOSIT_CHAIN_CHOICE.get(user_id, DEPOSIT_CHAINS[0])
        AWAITING_TX.discard(user_id)
        AWAITING_AMOUNT.add(user_id)
        label = DEPOSIT_TOKENS[chain]["label"]
        await show(
            f"💵  How much do you want to deposit?\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Network: {label}\n\n"
            f"Type an amount in USDT (minimum ${MIN_DEPOSIT_USD:.2f}).\n"
            "Example: 10\n\n"
            "I'll give you an exact amount to send that's reserved just for you.",
            [back_home_row(f"dep_chain:{chain}".encode())],
        )

    elif data == "submit_tx":
        AWAITING_AMOUNT.discard(user_id)
        AWAITING_TX.add(user_id)
        tx_text = (
            "[🖊](emoji/5985774024968379294) **Paste your transaction hash "
            "(TxID) below.**\n\n"
            "It looks like: 0x followed by 64 characters.\n\n"
            "I'll verify it on-chain and credit your balance.\n\n"
            f"[⁉️](emoji/5294404476382233385) **Contact {ADMIN_CONTACT} "
            "for help.**"
        )
        tx_buttons = [
            [
                Button.inline(
                    "Cancel",
                    b"deposit",
                    icon=5397586104981403273,
                    style="primary",
                )
            ]
        ]
        # No brand image on this page — send the text-only message FIRST,
        # then delete the photo message (no visible gap).
        try:
            await event.respond(tx_text, buttons=tx_buttons)
            await event.delete()
        except Exception:
            await show(tx_text, tx_buttons)

    elif data == "deposit_upi":
        clear_deposit_state(user_id)
        if not upi_ready():
            await show(
                "🇮🇳  UPI Deposit — Temporarily Unavailable\n"
                "━━━━━━━━━━━━━━━━━━━━  ━━\n\n"
                "UPI deposits aren't set up yet. Please check back soon.\n\n"
                f"❓ Need help? Contact {ADMIN_CONTACT}.",
                [back_home_row(b"deposit_menu")],
                banner="upi",
            )
        else:
            AWAITING_UPI_AMOUNT.add(user_id)
            await show(
                "**💯  Add Funds via UPI**\n"
                "**━━━━━   ━   ━━━━━━━━━━━**\n\n"
                f"**Enter the amount you want to add in ₹ (INR).**\n"
                "**Minimum: ₹10**\n\n"
                "**After you enter the amount, I'll generate your UPI ID and QR code for payment.**",
                [
                    [
                        Button.inline(
                            f"₹{amt}",
                            f"upi_amt:{amt}".encode(),
                            icon=5792091409603368458,
                            style="success",
                        )
                        for amt in (100, 500, 1500)
                    ],
                    [
                        Button.inline(
                            "✏️ Custom Amount",
                            b"upi_custom",
                            style="primary",
                        )
                    ],
                    back_home_row(b"deposit_menu"),
                ],
                banner="upi",
            )

    elif data == "upi_custom":
        # Green "Custom Amount" button: ask the user to type any amount.
        if not upi_ready():
            await event.answer("UPI deposits aren't available right now.", alert=True)
            return
        AWAITING_UPI_AMOUNT.add(user_id)
        await show(
            "**✏️  Custom Amount**\n"
            "**━━━━━━━━━━━━━━━━━━━**\n\n"
            f"**Type the amount you want to add in ₹ (INR).**\n"
            f"**Minimum: ₹{MIN_UPI_INR:.0f}**\n\n"
            "**Example: 750**",
            [back_home_row(b"deposit_upi")],
            banner="upi",
        )

    elif data.startswith("upi_amt:"):
        # Quick-amount buttons on the UPI deposit screen (₹100 / ₹500 / ₹1500).
        if not upi_ready():
            await event.answer("UPI deposits aren't available right now.", alert=True)
            return
        try:
            amt = int(data.split(":", 1)[1])
        except ValueError:
            await event.answer("Invalid amount.", alert=True)
            return
        await event.answer()
        await handle_upi_amount_entry(event, user_id, str(amt))

    elif data.startswith("upi_submit:"):
        try:
            req_id = int(data.split(":", 1)[1])
        except ValueError:
            await event.answer("Invalid request.", alert=True)
            return
        req = await get_upi_request(req_id)
        if not req or req["user_id"] != user_id or req["status"] != "awaiting_utr":
            await event.answer("This deposit request is no longer active.", alert=True)
            return
        AWAITING_UPI_AMOUNT.discard(user_id)
        AWAITING_UPI_UTR.add(user_id)
        PENDING_UPI_REQ[user_id] = req_id
        utr_text = (
            "🖊 Paste your UPI reference number (UTR) below.\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"⏩ You're adding ₹{req['amount_inr']:.2f}.\n\n"
            "After paying, open your UPI app, copy the 12-digit UTR /\n"
            "transaction reference number, and paste it here.\n\n"
            "An admin will verify your payment and credit your ₹ balance.\n\n"
            f"⁉️ Stuck? Contact {ADMIN_CONTACT} for help."
        )
        utr_buttons = [
            [
                Button.inline(
                    "Cancel",
                    b"menu",
                    icon=5397586104981403273,
                    style="primary",
                )
            ]
        ]
        # No QR on this page — send the text-only message FIRST, then
        # delete the photo message (no visible gap).
        try:
            await event.respond(utr_text, buttons=utr_buttons)
            await event.delete()
        except Exception:
            await show(utr_text, utr_buttons)

    elif data.startswith("upi_ok:") or data.startswith("upi_no:"):
        if user_id not in ADMIN_IDS:
            await event.answer("Not authorized.", alert=True)
            return
        approve = data.startswith("upi_ok:")
        try:
            req_id = int(data.split(":", 1)[1])
        except ValueError:
            await event.answer("Invalid request.", alert=True)
            return
        req = await get_upi_request(req_id)
        if not req:
            await event.answer("Request not found.", alert=True)
            return
        if req["status"] != "pending":
            await event.answer(f"Already {req['status']}.", alert=True)
            await show(
                f"UPI request #{req_id} is already {req['status']}.",
                [
                    [Button.inline("🧾 UPI Requests", b"adm_upi")],
                    [Button.inline("🏠 Home to Admin", b"admin")],
                ],
            )
            return
        if approve:
            res = await approve_upi_request(req_id, user_id)
            if res is None:
                await event.answer("Could not approve (already decided).", alert=True)
                return
            target_user, amount_inr, new_balance = res
            await log_admin_action(
                user_id,
                target_user,
                "upi_approve",
                amount_inr,
                f"req#{req_id} utr={req.get('utr')}",
            )
            await log_to_group(
                "💰 New Wallet Top-Up\n"
                f"User ID: {target_user}\n"
                "Payment: UPI\n"
                f"Amount: ₹{amount_inr:.2f}\n"
                f"Time: {ist_now_str()}"
            )
            await show(
                f"✅ Approved UPI request #{req_id}.\n\n"
                f"User `{target_user}` credited ₹{amount_inr:.2f}.\n"
                f"Their new ₹ balance: ₹{new_balance:.2f}",
                [
                    [Button.inline("🧾 UPI Requests", b"adm_upi")],
                    [Button.inline("🏠 Home to Admin", b"admin")],
                ],
            )
            await notify_user(
                target_user,
                "✅ UPI Deposit Approved\n"
                "━  ━━━━━━━━━━━━   ━━━━━━━━\n\n"
                f"₹{amount_inr:.2f} has been added to your INR balance.\n"
                f"New ₹ Balance: ₹{new_balance:.2f}",
                buttons=[
                    [
                        Button.inline(
                            "Check Balance",
                            b"balance",
                            icon=6186229881068590162,
                            style="primary",
                        )
                    ]
                ],
            )
        else:
            res = await reject_upi_request(req_id, user_id)
            if res is None:
                await event.answer("Could not reject (already decided).", alert=True)
                return
            target_user, amount_inr = res
            await log_admin_action(
                user_id,
                target_user,
                "upi_reject",
                amount_inr,
                f"req#{req_id} utr={req.get('utr')}",
            )
            await show(
                f"🚫 Rejected UPI request #{req_id} "
                f"(  {amount_inr:.2f}, user `{target_user}`).",
                [
                    [Button.inline("🧾 UPI Requests", b"adm_upi")],
                    [Button.inline("🏠 Home to Admin", b"admin")],
                ],
            )
            await notify_user(
                target_user,
                "     UPI Deposit Not Approved\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Your UPI deposit of ₹{amount_inr:.2f} could not be verified.\n\n"
                f"If you did pay, contact {ADMIN_CONTACT} with your UTR.",
            )

    elif data == "help":
        await show(
            # "ℹ️  Help\n"
            # "━━━━━   ━━━━━━━━━━      ━━\n\n"
            # "1. Tap 💵 Deposit and pick your network (BNB Smart Chain, Polygon, or Tron).\n"
            # "2. Enter how much USDT you want to deposit.\n"
            # "3. I'll give you an EXACT amount to send (e.g. 10.017) — send that\n"
            # "   exact amount to the address shown, on that network, within 15 minutes.\n"
            # "4. Paste your TxID and your balance is credited automatically.\n\n"
            # "The extra digits identify your payment, so the amount must match\n"
            # "exactly. Tap 💰 My Balance any time to see your balance.\n\n"
            f"❓ Need help? Contact {ADMIN_CONTACT}.",
            [[btn_back()]],
        )

    elif data == "admin":
        if user_id not in ADMIN_IDS:
            await event.answer("Not authorized.", alert=True)
            return
        clear_admin_state(user_id)
        await show(await build_admin_panel_text(), admin_panel_buttons())

    elif data.startswith("omail_sent:"):
        # Admin marks a manual order's invite as sent → notify the buyer.
        if user_id not in ADMIN_IDS:
            await event.answer("Not authorized.", alert=True)
            return
        try:
            oid = int(data.split(":", 1)[1])
        except ValueError:
            await event.answer("Invalid order.", alert=True)
            return
        order = await mark_order_mailed(oid)
        if not order:
            await event.answer(
                "That order isn't awaiting an invite (already sent or refunded).",
                alert=True,
            )
            return
        await log_admin_action(
            user_id, order["user_id"], "order_mailed", None, f"order={oid}"
        )
        await event.answer("Marked as sent. User notified.")
        name = order.get("product_name") or "your product"
        await notify_user(
            order["user_id"],
            "📬 Your Invite Was Sent\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Product: {name}\n"
            f"Email: `{order.get('email') or '—'}`\n\n"
            "Please check your inbox (and spam folder) for the invite.\n"
            "Tap ✅ below once you've received it.",
            buttons=[
                [Button.inline("✅ Got it!", f"ogot:{oid}".encode())],
                [Button.inline("🆘 Didn't get it", f"osupport:{oid}".encode())],
            ],
        )
        # Refresh the admin's order view if they're on it.
        try:
            await show(*await build_admin_order_view(oid))
        except Exception:
            pass

    elif data.startswith("myord:"):
        # Buyer opens the detail view for one of their own orders.
        try:
            oid = int(data.split(":", 1)[1])
        except ValueError:
            await event.answer("Invalid order.", alert=True)
            return
        view = await build_user_order_view(oid, user_id)
        if not view:
            await event.answer("Order not found.", alert=True)
            return
        await show(*view)

    elif data.startswith("howto:"):
        # Buyer opens the product's How-to-Use guide (from a purchase
        # confirmation or an order detail screen).
        try:
            pid = int(data.split(":", 1)[1])
        except ValueError:
            await event.answer("Invalid product.", alert=True)
            return
        p = await get_product(pid)
        guide = product_howto(p)
        if not guide:
            await event.answer(
                "No usage guide has been added for this product yet.", alert=True
            )
            return
        await show(
            f"**📖  How to Use — {p['name']}**\n"
            "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
            f"{guide}",
            [
                [btn_support()],
                [Button.inline("🧾 My Orders", b"orders")],
                [btn_back()],
            ],
        )

    elif data.startswith("ogot:"):
        # Buyer confirms they received their manual invite.
        try:
            oid = int(data.split(":", 1)[1])
        except ValueError:
            await event.answer("Invalid order.", alert=True)
            return
        order = await mark_order_received(oid, user_id)
        if not order:
            await event.answer(
                "This order isn't awaiting your confirmation.", alert=True
            )
            return
        name = order.get("product_name") or "your product"
        await show(
            "🎉  Thanks for Confirming\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Glad your {name} invite arrived. Enjoy!\n\n"
            "Your order is now marked complete.",
            [
                [Button.inline("🛒 Shop", b"shop")],
                [btn_back()],
            ],
        )

    elif data.startswith("osupport:"):
        # Buyer says the invite didn't arrive → show support contact.
        try:
            oid = int(data.split(":", 1)[1])
        except ValueError:
            await event.answer("Invalid order.", alert=True)
            return
        order = await get_order(oid)
        if not order or order["user_id"] != user_id:
            await event.answer("Order not found.", alert=True)
            return
        await show(
            "🆘  Need Help?\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Sorry your invite hasn't arrived yet. It can take up to 1 hour.\n\n"
            f"If it's been longer, contact {ADMIN_CONTACT} with your order "
            f"number (#{oid}) and we'll sort it out.",
            [
                [Button.inline("🧾 My Orders", b"orders")],
                [btn_back()],
            ],
        )

    elif data.startswith("adm_"):
        if user_id not in ADMIN_IDS:
            await event.answer("Not authorized.", alert=True)
            return
        action = data

        if action == "adm_cancel":
            clear_admin_state(user_id)
            await show(await build_admin_panel_text(), admin_panel_buttons())

        elif action == "adm_users":
            clear_admin_state(user_id)
            await show(
                "👥  User Management\n"
                "━━━━━━━   ━━━━━━━━━━━━━━━\n\n"
                "Find a user, adjust their $ / ₹ balance, or ban/unban them.",
                admin_users_buttons(),
            )

        elif action == "adm_depcfg":
            clear_admin_state(user_id)
            await show(
                "💵  Deposit Settings\n"
                "━━━━━━━━━━━   ━━━━━━━━━━━\n\n"
                "Edit deposit addresses, set the minimum deposit, or view "
                "recent deposits.",
                admin_depcfg_buttons(),
            )

        elif action == "adm_upimenu":
            clear_admin_state(user_id)
            await show(
                "💳  UPI\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Configure your UPI ID / QR / minimum, or review pending "
                "UPI deposit requests.",
                admin_upimenu_buttons(),
            )

        elif action == "adm_stats":
            s = await get_full_admin_stats()
            top_lines = []
            for i, (uid, cnt, spent_usd, spent_inr) in enumerate(
                s["top_buyers"], start=1
            ):
                label = await format_user_label(uid)
                spend_parts = []
                if spent_usd:
                    spend_parts.append(f"${spent_usd:.2f}")
                if spent_inr:
                    spend_parts.append(f"₹{spent_inr:.2f}")
                spend = " + ".join(spend_parts) or "$0.00"
                top_lines.append(f"{i}. {label} — {cnt} order(s), {spend}")
            top_block = "\n".join(top_lines) if top_lines else "No purchases yet."
            await show(
                "📊  Bot Statistics\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "👥 Users\n"
                f"• Total users: {s['users']}\n"
                f"• Buyers (bought ≥1): {s['buyers']}\n\n"
                "💰 Deposits\n"
                f"• Crypto: ${s['dep_usd']:.2f} ({s['dep_count']} tx)\n"
                f"• UPI approved: ₹{s['upi_inr']:.2f} ({s['upi_count']} req)\n\n"
                "🛒 Sales\n"
                f"• Orders: {s['orders']}  (refunded: {s['refunds']})\n"
                f"• Revenue: ${s['sales_usd']:.2f} + ₹{s['sales_inr']:.2f}\n"
                f"• Stock available: {s['stock']} item(s)\n\n"
                "👛 User Balances Held\n"
                f"• ${s['held_usd']:.2f} / ₹{s['held_inr']:.2f}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n"
                "🏆 Top Buyers\n"
                f"{top_block}",
                [
                    [Button.inline("🔄 Refresh", b"adm_stats")],
                    [Button.inline("🏠 Home to Admin", b"admin")],
                ],
            )

        elif action == "adm_maint":
            await set_maintenance_state(not MAINTENANCE)
            await log_admin_action(
                user_id, None, "maintenance", None, "on" if MAINTENANCE else "off"
            )
            if MAINTENANCE:
                body = (
                    "🚧  Bot — MAINTENANCE MODE\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    "The bot is now STOPPED for regular users.\n"
                    "They'll see a maintenance notice on every message "
                    "and button tap.\n\n"
                    "Admins (you) can keep using everything normally.\n"
                    "Tap ▶️ Start Bot in the admin panel to go live again."
                )
            else:
                body = (
                    "✅  Bot — LIVE\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    "Maintenance mode is OFF.\n"
                    "All users can use the bot again."
                )
            await show(
                body,
                [[Button.inline("🏠 Home to Admin", b"admin")]],
            )

        elif action == "adm_extractdb":
            await event.answer("Preparing database file…")
            sent = await send_db_backup(
                [user_id], note="📥 Manual extraction from the admin panel."
            )
            if sent:
                await show(
                    "🗄  Extract Database\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    "✅ Database file sent to you above.\n\n"
                    "Keep it safe — you can restore all user data "
                    "from this file if you ever lose VPS access.\n\n"
                    "⏰ It is also sent to all admins automatically "
                    "every 5 days.",
                    [[Button.inline("🏠 Home to Admin", b"admin")]],
                )
            else:
                await show(
                    "🗄  Extract Database\n"
                    "━━━━━━━━━━━━━━━━━  ━━━━━\n\n"
                    "❌ Failed to create or send the backup. "
                    "Check the server logs and try again.",
                    [[Button.inline("🏠 Home to Admin", b"admin")]],
                )

        elif action == "adm_deposits":
            deps = await get_recent_deposits(10)
            if not deps:
                body = "No deposits yet."
            else:
                lines = []
                for d in deps:
                    ts = (d["created_at"] or "")[:16].replace("T", " ")
                    lines.append(
                        f"• `{d['user_id']}` — ${d['usd_value']:.2f} "
                        f"({d['token']}) {ts}"
                    )
                body = "\n".join(lines)
            await show(
                "🧾  Recent Deposits (last 10)\n━━━━━━━━━━━━━━━━━━━━━━━\n\n" + body,
                [[Button.inline("🏠 Home to Admin", b"admin")]],
            )

        elif action == "adm_addr":
            lines = []
            for k, (_skey, _kind, label) in ADDR_KINDS.items():
                if k == "evm":
                    cur = DEPOSIT_ADDRESS or "(not set)"
                elif k == "tron":
                    cur = DEPOSIT_ADDRESS_TRON or "(not set)"
                else:
                    cur = DEPOSIT_ADDRESS_SOLANA or "(not set)"
                lines.append(f"• {label}:\n  `{cur}`")
            await show(
                "🏦  Deposit Addresses\n"
                "━━━━━━━━━━━━━━━━━━  ━━━━\n\n"
                + "\n".join(lines)
                + "\n\nTap one to change it.",
                admin_addr_buttons(),
            )

        elif action.startswith("adm_setaddr:"):
            kind = action.split(":", 1)[1]
            if kind not in ADDR_KINDS:
                await event.answer("Unknown address type.", alert=True)
                return
            _skey, _kind, label = ADDR_KINDS[kind]
            ADMIN_STATE[user_id] = {
                "action": "setaddr",
                "step": "value",
                "data": {"kind": kind},
            }
            await show(
                f"🏦  Set address — {label}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Send the new receiving address now.\n"
                "I'll validate it before saving.",
                admin_cancel_buttons(),
            )

        elif action in ("adm_addbal", "adm_rmbal", "adm_addbalinr", "adm_rmbalinr"):
            is_add = action in ("adm_addbal", "adm_addbalinr")
            is_inr = action in ("adm_addbalinr", "adm_rmbalinr")
            state_action = (
                ("addbalinr" if is_add else "rmbalinr")
                if is_inr
                else ("addbal" if is_add else "rmbal")
            )
            ADMIN_STATE[user_id] = {
                "action": state_action,
                "step": "user_id",
                "data": {},
            }
            cur = "₹ (INR)" if is_inr else "$ (USD)"
            await show(
                f"{'➕  Add' if is_add else '➖  Remove'} {cur} Balance\n"
                "━━━━━━━━━━━━━━━━━  ━━━━━\n\n"
                f"Which user? Send their numeric Telegram user ID to "
                f"{'add to' if is_add else 'remove from'} their {cur} balance.",
                admin_cancel_buttons(),
            )

        elif action == "adm_find":
            ADMIN_STATE[user_id] = {"action": "find", "step": "user_id", "data": {}}
            await show(
                "🔍  Find User\n"
                "━━  ━━━━━━━━━━━━━━━━━━━━\n\n"
                "Send the numeric Telegram user ID to look up.",
                admin_cancel_buttons(),
            )

        elif action == "adm_deluser":
            ADMIN_STATE[user_id] = {
                "action": "deluser",
                "step": "user_id",
                "data": {},
            }
            await show(
                "🗑  Delete User Data\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Send the numeric Telegram user ID to PERMANENTLY erase.\n\n"
                "This wipes their profile, balances, orders, deposits,\n"
                "UPI requests and referrals. Cannot be undone.",
                admin_cancel_buttons(),
            )

        elif action.startswith("adm_deluser_go:"):
            try:
                target = int(action.split(":", 1)[1])
            except ValueError:
                await event.answer("Bad user ID.", alert=True)
                return
            if target in ADMIN_IDS:
                await event.answer("You can't delete an admin.", alert=True)
                return
            try:
                counts = await delete_user_data(target)
            except ValueError as exc:
                await event.answer(str(exc), alert=True)
                return
            await log_admin_action(
                user_id, target, "delete_user_data", None, json.dumps(counts)
            )
            summary = "\n".join(
                f"• {k}: {v}" for k, v in counts.items() if v
            ) or "• nothing found for this ID"
            await show(
                "🗑  User Data Deleted\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"User `{target}` has been permanently erased.\n\n"
                f"Removed rows:\n{summary}",
                [[Button.inline("🏠 Home to Admin", b"admin")]],
            )

        elif action in ("adm_ban", "adm_unban"):
            is_ban = action == "adm_ban"
            ADMIN_STATE[user_id] = {
                "action": "ban" if is_ban else "unban",
                "step": "user_id",
                "data": {},
            }
            await show(
                f"{'🚫  Ban' if is_ban else '✅  Unban'} User\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Send the numeric Telegram user ID to {'ban' if is_ban else 'unban'}.",
                admin_cancel_buttons(),
            )

        elif action == "adm_setmin":
            ADMIN_STATE[user_id] = {"action": "setmin", "step": "value", "data": {}}
            await show(
                "⚙️  Minimum Deposit\n"
                "━  ━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Current minimum: ${MIN_DEPOSIT_USD:.2f}\n\n"
                "Send the new minimum deposit amount in USD (e.g. 5).",
                admin_cancel_buttons(),
            )

        elif action == "adm_setlowstock":
            ADMIN_STATE[user_id] = {
                "action": "setlowstock",
                "step": "value",
                "data": {},
            }
            current = await get_low_stock_threshold()
            await show(
                "⚠️  Low-Stock Alert Threshold\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Current: alert when a product drops to {current} or fewer left.\n\n"
                "Send the new threshold as a whole number (e.g. 3). "
                "Send 0 to only alert when a product sells out.",
                admin_cancel_buttons(),
            )

        elif action == "adm_broadcast":
            clear_admin_state(user_id)
            PENDING_BROADCASTS.pop(user_id, None)
            text, rows = await build_broadcast_center()
            await show(text, rows)

        elif action == "adm_bc_new":
            ADMIN_STATE[user_id] = {
                "action": "broadcast",
                "step": "message",
                "data": {},
            }
            await show(
                "🚀  New Broadcast\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Send the message you want to broadcast to ALL users.\n\n"
                "✨ Everything is preserved exactly: premium emojis,\n"
                "photos, videos, formatting, buttons-free rich text.\n\n"
                "You'll get a preview + confirm step before anything\n"
                "is sent.",
                admin_cancel_buttons(),
            )

        elif action == "adm_bc_send":
            payload = PENDING_BROADCASTS.pop(user_id, None)
            if payload is None:
                await event.answer("Nothing pending — start a new broadcast.", alert=True)
                text, rows = await build_broadcast_center()
                await show(text, rows)
                return
            total = len(await get_all_user_ids())
            bcast_id = await bc_create(user_id, bc_preview_of(payload), total)
            try:
                await event.edit(
                    f"📢  Broadcasting  #{bcast_id}\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"{bc_progress_bar(0, total)}  0%\n\n"
                    f"👥 Total:     {total}\n\n"
                    "⚡ warming up…",
                    buttons=[
                        [Button.inline("⛔ Cancel Broadcast", f"adm_bc_stop:{bcast_id}".encode())]
                    ],
                )
            except Exception:
                pass
            client.loop.create_task(
                run_broadcast(bcast_id, payload, event.chat_id, event.message_id)
            )

        elif action == "adm_bc_discard":
            PENDING_BROADCASTS.pop(user_id, None)
            clear_admin_state(user_id)
            text, rows = await build_broadcast_center()
            await show(text, rows)

        elif action.startswith("adm_bc_stop:"):
            bid = int(action.split(":", 1)[1])
            job = BROADCAST_JOBS.get(bid)
            if job:
                job["cancel"] = True
                await event.answer("Stopping broadcast…")
            else:
                await event.answer("This broadcast already finished.", alert=True)

        elif action.startswith("adm_bc_stopdel:"):
            bid = int(action.split(":", 1)[1])
            job = BROADCAST_JOBS.get(f"del:{bid}")
            if job:
                job["cancel"] = True
                await event.answer("Stopping deletion…")
            else:
                await event.answer("Deletion already finished.", alert=True)

        elif action.startswith("adm_bc_view:"):
            bid = int(action.split(":", 1)[1])
            b = await bc_get(bid)
            if not b:
                await event.answer("Broadcast not found.", alert=True)
                return
            date = (b["created_at"] or "").replace("T", " ")[:16]
            remaining = len(await bc_deliveries(bid))
            rows = []
            if remaining and b["status"] != "deleted":
                rows.append(
                    [Button.inline("🗑 Delete for Everyone", f"adm_bc_del:{bid}".encode())]
                )
            rows.append([Button.inline("📢 Broadcast Center", b"adm_broadcast")])
            rows.append([Button.inline("🏠 Home to Admin", b"admin")])
            await show(
                f"{bc_status_emoji(b['status'])}  Broadcast #{bid}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📝 {b['preview']}\n\n"
                f"📅 {date} UTC\n"
                f"📊 Status: {b['status'].upper()}\n"
                f"✅ Sent: {b['sent']}   ⚠️ Failed: {b['failed']}   "
                f"👥 Total: {b['total']}\n"
                f"💬 Still live in {remaining} chat(s)",
                rows,
            )

        elif action.startswith("adm_bc_del:"):
            bid = int(action.split(":", 1)[1])
            b = await bc_get(bid)
            if not b:
                await event.answer("Broadcast not found.", alert=True)
                return
            remaining = len(await bc_deliveries(bid))
            if not remaining:
                await event.answer("Nothing left to delete for this broadcast.", alert=True)
                return
            await show(
                f"🗑  Delete Broadcast #{bid}?\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📝 {b['preview']}\n\n"
                f"This will remove it from {remaining} user chat(s).\n"
                "⚠️ This cannot be undone.",
                [
                    [Button.inline("✅ Yes, Delete Everywhere", f"adm_bc_delgo:{bid}".encode())],
                    [Button.inline("✖️ Keep It", f"adm_bc_view:{bid}".encode())],
                ],
            )

        elif action.startswith("adm_bc_delgo:"):
            bid = int(action.split(":", 1)[1])
            if f"del:{bid}" in BROADCAST_JOBS:
                await event.answer("Deletion is already running.", alert=True)
                return
            try:
                await event.edit(
                    f"🗑  Deleting Broadcast  #{bid}\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    "⚡ starting…",
                    buttons=[
                        [Button.inline("⛔ Stop Deleting", f"adm_bc_stopdel:{bid}".encode())]
                    ],
                )
            except Exception:
                pass
            client.loop.create_task(
                run_broadcast_delete(bid, event.chat_id, event.message_id)
            )

        elif action == "adm_upiset":
            has_qr = "✅ set" if os.path.exists(UPI_QR_PATH) else "❌ not set"
            await show(
                "💳  UPI Settings\n"
                "  ━━━━  ━━━━━━━   ━━━━━━\n\n"
                f"UPI ID: `{UPI_ID or '(not set)'}`\n"
                f"QR image: {has_qr}\n"
                f"Min UPI deposit: ₹{MIN_UPI_INR:.2f}\n\n"
                f"Status: {'🟢 UPI deposits ENABLED' if upi_ready() else '🔴 UPI deposits DISABLED (set UPI ID + QR)'}\n\n"
                "Tap below to change a setting.",
                [
                    [Button.inline("✏️ Set UPI ID", b"adm_setupiid")],
                    [Button.inline("🖼 Set UPI QR", b"adm_setupiqr")],
                    [Button.inline("⚙️ Min UPI Deposit", b"adm_setminupi")],
                    [Button.inline("🏠 Home to Admin", b"admin")],
                ],
            )

        elif action == "adm_setupiid":
            ADMIN_STATE[user_id] = {"action": "setupiid", "step": "value", "data": {}}
            await show(
                "✏️  Set UPI ID\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Current: `{UPI_ID or '(not set)'}`\n\n"
                "Send the new UPI ID (VPA), e.g. `name@bank`.",
                admin_cancel_buttons(),
            )

        elif action == "adm_setupiqr":
            ADMIN_STATE[user_id] = {"action": "setupiqr", "step": "photo", "data": {}}
            await show(
                "🖼  Set UPI QR\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Send the UPI QR code image now (as a photo).\n"
                "It's shown to users on the UPI deposit screen.",
                admin_cancel_buttons(),
            )

        elif action == "adm_setminupi":
            ADMIN_STATE[user_id] = {"action": "setminupi", "step": "value", "data": {}}
            await show(
                "⚙e    Minimum UPI Deposit\n"
                "━━━━━━━━   ━━━━     ━━━━   ━\n\n"
                f"Current minimum: ₹{MIN_UPI_INR:.2f}\n\n"
                "Send the new minimum UPI deposit amount in ₹ (e.g. 100).",
                admin_cancel_buttons(),
            )

        elif action == "adm_upi":
            reqs = await get_pending_upi_requests(10)
            if not reqs:
                await show(
                    "🧾  Pending UPI Requests\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    "No pending UPI deposits.     ",
                    [[Button.inline("🏠 Home to Admin", b"admin")]],
                )
            else:
                rows = []
                lines = []
                for r in reqs:
                    ts = (r["created_at"] or "")[:16].replace("T", " ")
                    lines.append(
                        f"• #{r['id']} — `{r['user_id']}` — ₹{r['amount_inr']:.2f}\n"
                        f"  UTR: `{r.get('utr') or '—'}`  ({ts})"
                    )
                    rows.append(
                        [
                            Button.inline(
                                f"✅ Approve #{r['id']}", f"upi_ok:{r['id']}".encode()
                            ),
                            Button.inline(
                                f"🚫 Reject #{r['id']}", f"upi_no:{r['id']}".encode()
                            ),
                        ]
                    )
                rows.append([Button.inline("🔄 Refresh", b"adm_upi")])
                rows.append([Button.inline("🏠 Home to Admin", b"admin")])
                await show(
                    "🧾  Pending UPI Requests (oldest first)\n"
                    "   ━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n" + "\n".join(lines),
                    rows,
                )

        elif action == "adm_shop":
            clear_admin_state(user_id)
            await show(
                "🛒  Shop\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Manage products, prices and stock, or review recent orders.",
                admin_shop_buttons(),
            )

        elif action == "adm_products":
            clear_admin_state(user_id)
            cats = await list_categories()
            uncat = await count_uncategorized_products()
            rows = []
            for c in cats:
                rows.append(
                    [
                        Button.inline(
                            (
                                f"{c['name']} ({c['product_count']} products)"
                                if c.get("emoji_id")
                                else f"{cat_emoji(c)} {c['name']} ({c['product_count']} products)"
                            ),
                            f"adm_cat:{c['id']}".encode(),
                            icon=c.get("emoji_id"),
                        )
                    ]
                )
            if uncat > 0:
                rows.append(
                    [Button.inline(f"🗂 Other / uncategorized ({uncat})", b"adm_cat:0")]
                )
            rows.append([Button.inline("➕ Add Category", b"adm_addcat")])
            rows.append(
                [
                    Button.inline("Back", b"adm_shop", icon=PREMIUM_EMOJI_MAP.get("⬅️")),
                    Button.inline("🏠 Home", b"admin"),
                ]
            )
            if not cats and not uncat:
                body = (
                    "No categories yet. Add a category first —\n"
                    "products are created inside a category."
                )
            else:
                body = "Tap a category to manage its products."
            await show(
                "🗂  Categories\n━━━━━━━━━━━━━━━━━━━━━━━\n\n" + body,
                rows,
            )

        elif action.startswith("adm_cat:"):
            clear_admin_state(user_id)
            try:
                cid = int(action.split(":", 1)[1])
            except ValueError:
                await event.answer("Invalid category.", alert=True)
                return
            if cid == 0:
                cat_name = "Other / uncategorized"
                cat_icon = "  "
            else:
                cat = await get_category(cid)
                if not cat:
                    await event.answer("Category not found.", alert=True)
                    return
                cat_name = cat["name"]
                cat_icon = cat_emoji(cat)
            prods = await list_all_products(cid)
            threshold = await get_low_stock_threshold()
            rows = []
            for p in prods:
                flag = "🟢" if p["active"] else "🔴"
                stock = p["stock"]
                if stock <= 0:
                    stock_label = "🚨 OUT"
                elif stock <= threshold:
                    stock_label = f"⚠️ {stock} left"
                else:
                    stock_label = f"stock {stock}"
                rows.append(
                    [
                        Button.inline(
                            f"{flag} {p['name']} — {shop_price_label(p)} "
                            f"({stock_label})",
                            f"adm_prod:{p['id']}".encode(),
                            icon=p.get("emoji_id"),
                        )
                    ]
                )
            rows.append(
                [Button.inline("➕ Add Product", f"adm_addprod:{cid}".encode())]
            )
            if cid != 0:
                rows.append(
                    [
                        Button.inline("✏️ Rename", f"adm_renamecat:{cid}".encode()),
                        Button.inline("🗑 Delete", f"adm_delcat:{cid}".encode()),
                    ]
                )
                rows.append(
                    [Button.inline("🖼️ Category Image", f"adm_recatimg:{cid}".encode())]
                )
            rows.append(
                [
                    Button.inline(
                        "Back", b"adm_products", icon=PREMIUM_EMOJI_MAP.get("⬅️")
                    ),
                    Button.inline("     Home", b"adm_shop"),
                ]
            )
            body = (
                "🟢 active · 🔴 hidden · ⚠️ low · 🚨 out. Tap one to manage."
                if prods
                else "No products in this category yet. Tap ➕ Add Product."
            )
            await show(
                f"{cat_icon}  {cat_name}\n━━━━━━━━━━━━━━━━━━━━━━━\n\n" + body,
                rows,
            )

        elif action.startswith("adm_setcat:"):
            clear_admin_state(user_id)
            try:
                pid = int(action.split(":", 1)[1])
            except ValueError:
                await event.answer("Invalid product.", alert=True)
                return
            p = await get_product(pid)
            if not p:
                await event.answer("Product not found.", alert=True)
                return
            cats = await list_categories()
            rows = []
            for c in cats:
                mark = "✅ " if p.get("category_id") == c["id"] else ""
                rows.append(
                    [
                        Button.inline(
                            (
                                f"{mark}{c['name']}"
                                if c.get("emoji_id")
                                else f"{mark}{cat_emoji(c)} {c['name']}"
                            ),
                            f"adm_setcat_ok:{pid}:{c['id']}".encode(),
                            icon=c.get("emoji_id"),
                        )
                    ]
                )
            mark = "   " if not p.get("category_id") else ""
            rows.append(
                [
                    Button.inline(
                        f"{mark}🗂 Other / uncategorized",
                        f"adm_setcat_ok:{pid}:0".encode(),
                    )
                ]
            )
            rows.append(
                [
                    Button.inline(
                        "Back",
                        f"adm_prod:{pid}".encode(),
                        icon=PREMIUM_EMOJI_MAP.get("⬅️"),
                    )
                ]
            )
            await show(
                f"🗂  Move “{p['name']}” to which category?\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Tap the new category for this product.",
                rows,
            )

        elif action.startswith("adm_setcat_ok:"):
            parts = action.split(":")
            try:
                pid, cid = int(parts[1]), int(parts[2])
            except (IndexError, ValueError):
                await event.answer("Invalid selection.", alert=True)
                return
            p = await get_product(pid)
            if not p:
                await event.answer("Product not found.", alert=True)
                return
            if cid != 0 and not await get_category(cid):
                await event.answer("Category not found.", alert=True)
                return
            await set_product_category(pid, cid if cid != 0 else None)
            await log_admin_action(
                user_id, None, "shop_setcat", None, f"pid={pid} cid={cid}"
            )
            await event.answer("Category updated.")
            text, rows = await build_admin_product_view(pid)
            if text is not None:
                await show(text, rows)

        elif action == "adm_addcat":
            ADMIN_STATE[user_id] = {
                "action": "addcat",
                "step": "name",
                "data": {},
            }
            await show(
                "➕  Add Category — Name\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Send the category name. You can attach a premium emoji:\n\n"
                "• `5368324170671202286 AI Tools` — emoji ID, then name\n"
                "• `😎 Gaming` — premium emoji + name in one message\n"
                "• `Gaming` — plain name, no emoji\n\n"
                "ℹ   The premium emoji shows animated in shop texts.\n"
                "Buttons show its standard version (Telegram limit).",
                admin_cancel_buttons(),
            )

        elif action.startswith("adm_renamecat:"):
            try:
                cid = int(action.split(":", 1)[1])
            except ValueError:
                await event.answer("Invalid category.", alert=True)
                return
            cat = await get_category(cid)
            if not cat:
                await event.answer("Category not found.", alert=True)
                return
            ADMIN_STATE[user_id] = {
                "action": "renamecat",
                "step": "name",
                "data": {"cid": cid},
            }
            await show(
                f"✏️  Rename Category — {cat['name']}\n"
                "━━━━━━━━━━━━━━   ━  ━  ━\n\n"
                "Send the new category name.\n\n"
                "You can also attach a new premium emoji\n"
                "(`emoji_id Name` or `😎 Name`) — otherwise\n"
                "the current emoji is kept.",
                admin_cancel_buttons(),
            )

        elif action.startswith("adm_delcat:"):
            clear_admin_state(user_id)
            try:
                cid = int(action.split(":", 1)[1])
            except ValueError:
                await event.answer("Invalid category.", alert=True)
                return
            cat = await get_category(cid)
            if not cat:
                await event.answer("Category not found.", alert=True)
                return
            prods = await list_all_products(cid)
            await show(
                f"🗑  Delete category “{cat['name']}”?\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"It contains {len(prods)} product(s). Products are NOT deleted —\n"
                "they move to “Other / uncategorized”. This can't be undone.",
                [
                    [Button.inline("🗑 Yes, delete", f"adm_delcat_ok:{cid}".encode())],
                    [
                        Button.inline(
                            "Cancel",
                            f"adm_cat:{cid}".encode(),
                            icon=PREMIUM_EMOJI_MAP.get("⬅️"),
                        )
                    ],
                ],
            )

        elif action.startswith("adm_delcat_ok:"):
            try:
                cid = int(action.split(":", 1)[1])
            except ValueError:
                await event.answer("Invalid category.", alert=True)
                return
            cat = await get_category(cid)
            if not cat:
                await event.answer("Category not found.", alert=True)
                return
            await delete_category(cid)
            await unregister_category_emoji(cat.get("emoji_char"))
            await log_admin_action(
                user_id, None, "shop_delcat", None, f"cid={cid} name={cat['name']}"
            )
            await event.answer("Category deleted.")
            cats = await list_categories()
            uncat = await count_uncategorized_products()
            rows = []
            for c in cats:
                rows.append(
                    [
                        Button.inline(
                            (
                                f"{c['name']} ({c['product_count']} products)"
                                if c.get("emoji_id")
                                else f"{cat_emoji(c)} {c['name']} ({c['product_count']} products)"
                            ),
                            f"adm_cat:{c['id']}".encode(),
                            icon=c.get("emoji_id"),
                        )
                    ]
                )
            if uncat > 0:
                rows.append(
                    [Button.inline(f"🗂 Other / uncategorized ({uncat})", b"adm_cat:0")]
                )
            rows.append([Button.inline("➕ Add Category", b"adm_addcat")])
            rows.append(
                [
                    Button.inline("Back", b"adm_shop", icon=PREMIUM_EMOJI_MAP.get("⬅️")),
                    Button.inline("🏠 Home", b"admin"),
                ]
            )
            await show(
                "🗂  Categories\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Category deleted. Its products (if any) moved to "
                "“Other / uncategorized”.",
                rows,
            )

        elif action.startswith("adm_prod:"):
            clear_admin_state(user_id)
            try:
                pid = int(action.split(":", 1)[1])
            except ValueError:
                await event.answer("Invalid product.", alert=True)
                return
            text, rows = await build_admin_product_view(pid)
            if text is None:
                await event.answer("Product not found.", alert=True)
                return
            await show(text, rows)

        elif action.startswith("adm_reimg:"):
            # Image upload flow: admin sends a photo, we store its file_id.
            try:
                pid = int(action.split(":", 1)[1])
            except ValueError:
                await event.answer("Invalid product.", alert=True)
                return
            p = await get_product(pid)
            if not p:
                await event.answer("Product not found.", alert=True)
                return
            has_img = "📷 (currently set)" if p.get("image_file_id") else "(no image yet)"
            ADMIN_STATE[user_id] = {
                "action": "editimg",
                "step": "photo",
                "data": {"pid": pid},
            }
            await show(
                f"🖼️  Product Image     {p['name']}\n"
                "━   ━━   ━━━━━━━━━━━━━━━\n\n"
                f"Current: {has_img}\n\n"
                "Send a photo to set it, or send `-` to remove the current image.",
                admin_cancel_buttons(),
            )

        elif action.startswith("adm_recatimg:"):
            # Category image upload flow: admin sends a photo, saved to disk.
            try:
                cid = int(action.split(":", 1)[1])
            except ValueError:
                await event.answer("Invalid category.", alert=True)
                return
            cat = await get_category(cid)
            if not cat:
                await event.answer("Category not found.", alert=True)
                return
            has_img = (
                "📷 (currently set)" if cat.get("image_file_id") else "(no image yet)"
            )
            ADMIN_STATE[user_id] = {
                "action": "editcatimg",
                "step": "photo",
                "data": {"cid": cid},
            }
            await show(
                f"🖼️  Category Image — {cat['name']}\n"
                "━━━━   ━━━━━━━━━━━━━━━\n\n"
                f"Current: {has_img}\n\n"
                "Buyers see this image when they open the category in the shop.\n\n"
                "Send a photo to set it, or send `-` to remove the current image.",
                admin_cancel_buttons(),
            )

        elif (
            action.startswith("adm_rename:")
            or action.startswith("adm_redesc:")
            or action.startswith("adm_rehowto:")
            or action.startswith("adm_relearn:")
            or action.startswith("adm_warranty:")
        ):
            if action.startswith("adm_rename:"):
                field = "name"
            elif action.startswith("adm_redesc:"):
                field = "description"
            elif action.startswith("adm_relearn:"):
                field = "learn_more"
            elif action.startswith("adm_warranty:"):
                field = "warranty_days"
            else:
                field = "howto"
            try:
                pid = int(action.split(":", 1)[1])
            except ValueError:
                await event.answer("Invalid product.", alert=True)
                return
            p = await get_product(pid)
            if not p:
                await event.answer("Product not found.", alert=True)
                return
            ADMIN_STATE[user_id] = {
                "action": "editmeta",
                "step": field,
                "data": {"pid": pid},
            }
            if field == "name":
                await show(
                    f"✏️  Rename — {p['name']}\n"
                    "━━━━━━━━   ━━━━━━━━━━━━━━\n\n"
                    "Send the new product name (up to 120 chars).\n\n"
                    "Optional premium emoji — start with one:\n"
                    "• Send the premium emoji followed by the name\n"
                    "• Or `Name | emoji_id`",
                    admin_cancel_buttons(),
                )
            elif field == "warranty_days":
                await show(
                    f"🛡 Edit Warranty — {p['name']}\n\n"
                    f"Current: {p.get('warranty_days') or 0} days\n\n"
                    "Send the warranty in whole days (0 for no warranty).\n"
                    "The product post updates automatically. Stock is calculated from inventory.",
                    admin_cancel_buttons(),
                )
            elif field == "learn_more":
                cur = product_learn_more(p) or "—"
                await show(
                    f"ℹ️  Learn More — {p['name']}\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"Current:\n{cur}\n\n"
                    "Send the Learn More text buyers will see when they tap "
                    "the Learn More button on the product page "
                    "(or send `-` to clear).\n\n"
                    "Formatting is kept: bold, multi-line quotes, expandable "
                    "quotes and premium emojis show exactly how you send them.",
                    admin_cancel_buttons(),
                )
            elif field == "howto":
                cur = product_howto(p) or "—"
                await show(
                    f"📖  How to Use — {p['name']}\n"
                    "━━━━━━━━━━━━━━━━━━━━━━  \n\n"
                    f"Current:\n{cur}\n\n"
                    "Send the usage instructions buyers will see after "
                    "purchase (or send `-` to clear).\n\n"
                    "Formatting is kept: bold, multi-line quotes, expandable "
                    "quotes and premium emojis show exactly how you send them.",
                    admin_cancel_buttons(),
                )
            else:
                cur = (p["description"] or "").strip() or "—"
                await show(
                    f"📝  Edit Description — {p['name']}\n"
                    "━━━━   ━━━━━━━━━━━━━━━━━━\n\n"
                    f"Current: {cur}\n\n"
                    "Send the new description (or send `-` to clear it).",
                    admin_cancel_buttons(),
                )

        elif action.startswith("adm_stock:"):
            try:
                pid = int(action.split(":", 1)[1])
            except ValueError:
                await event.answer("Invalid product.", alert=True)
                return
            p = await get_product(pid)
            if not p:
                await event.answer("Product not found.", alert=True)
                return
            ADMIN_STATE[user_id] = {
                "action": "addstock",
                "step": "items",
                "data": {"pid": pid},
            }
            await show(
                f"    Add Stock — {p['name']}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Send the deliverables now — ONE item per MESSAGE.\n"
                "Each message becomes one unit sold to exactly one buyer\n"
                "(links with extra text are fine — the whole message is "
                "delivered as-is).\n\n"
                "Keep sending items one by one, then tap Cancel/Done.",
                admin_cancel_buttons(),
            )

        elif action.startswith("adm_viewstock:"):
            # View available stock items with per-item delete buttons.
            try:
                pid = int(action.split(":", 1)[1])
            except ValueError:
                await event.answer("Invalid product.", alert=True)
                return
            result = await build_admin_stock_view(pid)
            if result is None:
                await event.answer("Product not found.", alert=True)
                return
            await show(result[0], result[1])

        elif action.startswith("adm_delitem:"):
            # Delete ONE particular stock item (only if still available).
            try:
                _, pid_s, iid_s = action.split(":")
                pid, iid = int(pid_s), int(iid_s)
            except ValueError:
                await event.answer("Invalid item.", alert=True)
                return
            async with get_db() as db:
                cur = await db.execute(
                    "DELETE FROM product_items "
                    "WHERE id = ? AND product_id = ? AND status = 'available'",
                    (iid, pid),
                )
                await db.commit()
                deleted = cur.rowcount
            invalidate_shop_cache()
            if deleted:
                await log_admin_action(
                    user_id, None, "shop_delitem", None, f"pid={pid} item={iid}"
                )
                await event.answer(f"Item #{iid} deleted.")
            else:
                await event.answer("Item already sold or removed.", alert=True)
            # Refresh the stock view.
            result = await build_admin_stock_view(pid)
            if result is not None:
                await show(result[0], result[1])

        elif action.startswith("adm_setprice:"):
            try:
                pid = int(action.split(":", 1)[1])
            except ValueError:
                await event.answer("Invalid product.", alert=True)
                return
            p = await get_product(pid)
            if not p:
                await event.answer("Product not found.", alert=True)
                return
            ADMIN_STATE[user_id] = {
                "action": "setprice",
                "step": "price_usd",
                "data": {"pid": pid},
            }
            await show(
                f"💲  Edit Prices — {p['name']}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Current: {shop_price_label(p)}\n\n"
                "Send the USD price (send 0 if it can't be bought with $).",
                admin_cancel_buttons(),
            )

        elif action.startswith("adm_toggle:"):
            try:
                pid = int(action.split(":", 1)[1])
            except ValueError:
                await event.answer("Invalid product.", alert=True)
                return
            p = await get_product(pid)
            if not p:
                await event.answer("Product not found.", alert=True)
                return
            new_active = not p["active"]
            await set_product_active(pid, new_active)
            await log_admin_action(
                user_id, None, "shop_toggle", None, f"pid={pid} active={new_active}"
            )
            await event.answer(
                "Product is now visible." if new_active else "Product hidden."
            )
            text, rows = await build_admin_product_view(pid)
            if text is None:
                await event.answer("Product not found.", alert=True)
                return
            await show(text, rows)

        elif action.startswith("adm_ftype:"):
            try:
                pid = int(action.split(":", 1)[1])
            except ValueError:
                await event.answer("Invalid product.", alert=True)
                return
            p = await get_product(pid)
            if not p:
                await event.answer("Product not found.", alert=True)
                return
            new_ft = "instant" if product_is_manual(p) else "manual"
            await set_product_fulfillment(pid, new_ft)
            await log_admin_action(
                user_id, None, "shop_fulfillment", None, f"pid={pid} type={new_ft}"
            )
            await event.answer(
                "Now manual (email invite)."
                if new_ft == "manual"
                else "Now instant delivery."
            )
            text, rows = await build_admin_product_view(pid)
            if text is None:
                await event.answer("Product not found.", alert=True)
                return
            await show(text, rows)

        elif action.startswith("adm_delprod:"):
            try:
                pid = int(action.split(":", 1)[1])
            except ValueError:
                await event.answer("Invalid product.", alert=True)
                return
            p = await get_product(pid)
            if not p:
                await event.answer("Product not found.", alert=True)
                return
            await show(
                f"🗑  Delete {p['name']}?\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "This removes the product and its UNSOLD stock. Past orders and\n"
                "already-delivered items are kept. This can't be undone.",
                [
                    [Button.inline("🗑 Yes, delete", f"adm_delok:{pid}".encode())],
                    [
                        Button.inline(
                            "Cancel",
                            f"adm_prod:{pid}".encode(),
                            icon=PREMIUM_EMOJI_MAP.get("⬅️"),
                        )
                    ],
                ],
            )

        elif action.startswith("adm_delok:"):
            try:
                pid = int(action.split(":", 1)[1])
            except ValueError:
                await event.answer("Invalid product.", alert=True)
                return
            p = await get_product(pid)
            if not p:
                await event.answer("Product not found.", alert=True)
                return
            await delete_product(pid)
            await unregister_category_emoji(p.get("emoji_char"))
            await log_admin_action(
                user_id, None, "shop_delete", None, f"pid={pid} name={p['name']}"
            )
            await event.answer("Product deleted.")
            cat_id = p.get("category_id") or 0
            prods = await list_all_products(cat_id)
            rows = []
            for pr in prods:
                flag = "🟢" if pr["active"] else "  "
                rows.append(
                    [
                        Button.inline(
                            f"{flag} {pr['name']} — {shop_price_label(pr)} "
                            f"(stock {pr['stock']})",
                            f"adm_prod:{pr['id']}".encode(),
                            icon=pr.get("emoji_id"),
                        )
                    ]
                )
            rows.append(
                [Button.inline("➕ Add Product", f"adm_addprod:{cat_id}".encode())]
            )
            rows.append(
                [
                    Button.inline(
                        "Back", b"adm_products", icon=PREMIUM_EMOJI_MAP.get("⬅️")
                    ),
                    Button.inline("🏠 Home", b"adm_shop"),
                ]
            )
            await show(
                "📦  Products\n"
                "━━━━━━━━━━━━  ━━━━━━━━━━\n\n"
                + ("Deleted. " if prods else "No products left here. ")
                + ("Tap one to manage." if prods else "Add one to get started."),
                rows,
            )

        elif action == "adm_addprod" or action.startswith("adm_addprod:"):
            # Products are always created inside a category. The bare
            # "adm_addprod" (legacy) sends the admin to pick/create a category.
            cid = None
            if ":" in action:
                try:
                    cid = int(action.split(":", 1)[1])
                except ValueError:
                    await event.answer("Invalid category.", alert=True)
                    return
            if cid is None:
                await event.answer("Pick a category first.", alert=True)
                await show(
                    "🗂  Pick a category\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    "Products live inside a category. Open one (or add one) "
                    "and tap ➕ Add Product there.",
                    [
                        [Button.inline("🗂 Categories", b"adm_products")],
                        [Button.inline("🏠 Home to Shop", b"adm_shop")],
                    ],
                )
                return
            if cid != 0:
                cat = await get_category(cid)
                if not cat:
                    await event.answer("Category not found.", alert=True)
                    return
                cat_label = cat["name"]
            else:
                cat_label = "Other / uncategorized"
            ADMIN_STATE[user_id] = {
                "action": "addprod",
                "step": "name",
                "data": {"category_id": cid},
            }
            await show(
                f"➕  Add Product     Name  (in 🗂 {cat_label})\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Send the product name (e.g. `Gemini Pro    1 month`).\n\n"
                "Optional premium emoji — start with one:\n"
                "• Send the premium emoji followed by the name\n"
                "• Or `Name | emoji_id`",
                admin_cancel_buttons(),
            )

        elif action == "adm_orders":
            clear_admin_state(user_id)
            orders = await get_recent_orders(10)
            rows = []
            if not orders:
                body = "No orders yet."
            else:
                body = "Tap an order to refund or re-deliver it."
                for o in orders:
                    sym = "$" if o["currency"] == "usd" else "₹"
                    name = o.get("product_name") or "(removed)"
                    flag = "↩️" if o.get("status") == "refunded" else "🧾"
                    rows.append(
                        [
                            Button.inline(
                                f"{flag} #{o['id']} {name} — {sym}{o['amount']:.2f} "
                                f"· {o['user_id']}",
                                f"adm_ordv:{o['id']}".encode(),
                            )
                        ]
                    )
            rows.append([Button.inline("🔄 Refresh", b"adm_orders")])
            rows.append([Button.inline("🏠 Home to Shop", b"adm_shop")])
            await show(
                "🧾  Recent Orders\n━━━━━━━━━━━━━━━━━━━━━━━\n\n" + body,
                rows,
            )

        elif action.startswith("adm_ordv:"):
            clear_admin_state(user_id)
            try:
                oid = int(action.split(":", 1)[1])
            except ValueError:
                await event.answer("Invalid order.", alert=True)
                return
            await show(*await build_admin_order_view(oid))

        elif action.startswith("adm_refund:"):
            try:
                oid = int(action.split(":", 1)[1])
            except ValueError:
                await event.answer("Invalid order.", alert=True)
                return
            o = await get_order(oid)
            if not o:
                await event.answer("Order not found.", alert=True)
                return
            if o["status"] == "refunded":
                await event.answer("Already refunded.", alert=True)
                await show(*await build_admin_order_view(oid))
                return
            sym = "$" if o["currency"] == "usd" else "₹"
            await show(
                f"↩️  Refund Order #{oid}?\n"
                "━━   ━━━━━━━━━━━━━━━━━\n\n"
                f"This credits {sym}{o['amount']:.2f} back to user "
                f"`{o['user_id']}`'s {sym} balance and marks the order refunded.\n"
                "The delivered item stays with the user. This can't be undone.",
                [
                    [Button.inline("  ️ Yes, refund", f"adm_refundok:{oid}".encode())],
                    [
                        Button.inline(
                            "Cancel",
                            f"adm_ordv:{oid}".encode(),
                            icon=PREMIUM_EMOJI_MAP.get("⬅️"),
                        )
                    ],
                ],
            )

        elif action.startswith("adm_refundok:"):
            try:
                oid = int(action.split(":", 1)[1])
            except ValueError:
                await event.answer("Invalid order.", alert=True)
                return
            status, payload = await refund_order(oid)
            if status == "ok":
                sym = "$" if payload["currency"] == "usd" else "₹"
                await log_admin_action(
                    user_id,
                    payload["user_id"],
                    "order_refund",
                    payload["amount"],
                    f"order={oid} {payload['currency']} "
                    f"product={payload['product_name']}",
                )
                await notify_user(
                    payload["user_id"],
                    "↩️ Refund Issued\n"
                    "━━━━━━━━━   ━━━━━━━━━━━━━\n\n"
                    f"Your order for {payload['product_name'] or 'a product'} was "
                    f"refunded.\n"
                    f"Refunded: {sym}{payload['amount']:.2f}\n"
                    f"New {sym} balance: {sym}{payload['new_balance']:.2f}",
                )
                await event.answer("Refunded.")
                await show(*await build_admin_order_view(oid))
            elif status == "already":
                await event.answer("Already refunded.", alert=True)
                await show(*await build_admin_order_view(oid))
            elif status == "not_found":
                await event.answer("Order not found.", alert=True)
            elif status == "supplier_managed":
                await event.answer("This order is managed by the supplier. Refresh its status from Wholesale Suppliers.", alert=True)
            else:  # no_user
                await event.answer("Buyer no longer exists.", alert=True)

        elif action.startswith("adm_redeliver:"):
            try:
                oid = int(action.split(":", 1)[1])
            except ValueError:
                await event.answer("Invalid order.", alert=True)
                return
            o = await get_order(oid)
            if not o:
                await event.answer("Order not found.", alert=True)
                return
            if o["status"] != "completed":
                await event.answer("Order was refunded.", alert=True)
                await show(*await build_admin_order_view(oid))
                return
            await show(
                f"🔁  Re-deliver Order #{oid}?\n"
                "━━━━━━━━━━━━━   ━━━━━━━━━\n\n"
                f"This claims a fresh item from {o['product_name'] or '(removed)'} and "
                f"sends it to user `{o['user_id']}` at no extra charge. It uses one unit "
                "of stock.",
                [
                    [
                        Button.inline(
                            "🔁 Yes, re-deliver", f"adm_redeliverok:{oid}".encode()
                        )
                    ],
                    [
                        Button.inline(
                            "Cancel",
                            f"adm_ordv:{oid}".encode(),
                            icon=PREMIUM_EMOJI_MAP.get("⬅️"),
                        )
                    ],
                ],
            )

        elif action.startswith("adm_redeliverok:"):
            try:
                oid = int(action.split(":", 1)[1])
            except ValueError:
                await event.answer("Invalid order.", alert=True)
                return
            status, payload = await redeliver_order(oid)
            if status == "ok":
                await log_admin_action(
                    user_id,
                    payload["user_id"],
                    "order_redeliver",
                    None,
                    f"order={oid} product={payload['product_name']}",
                )
                await notify_user(
                    payload["user_id"],
                    "🎁 Replacement Delivered\n"
                    "━━━━━━━  ━━━━━━━━━━━━\n\n"
                    f"A fresh item for {payload['product_name']} was sent to you at no "
                    "extra charge:\n"
                    f"`{payload['content']}`\n\n"
                    "It's also saved under 🧾 My Orders.",
                )
                await event.answer("Re-delivered.")
                await show(*await build_admin_order_view(oid))
            elif status == "out_of_stock":
                await event.answer("No stock left to re-deliver.", alert=True)
                await show(*await build_admin_order_view(oid))
            elif status == "already":
                await event.answer("Order was refunded.", alert=True)
                await show(*await build_admin_order_view(oid))
            elif status == "gone":
                await event.answer("Product no longer exists.", alert=True)
                await show(*await build_admin_order_view(oid))
            elif status == "supplier_managed":
                await event.answer("Supplier orders use their original API delivery. Open Wholesale Suppliers to check the order.", alert=True)
            else:  # not_found
                await event.answer("Order not found.", alert=True)

        else:
            await event.answer()
    else:
        await event.answer()


# ── User slash commands ────────   ───────────────────────────────────────────────


@client.on(events.NewMessage(pattern=r"^/balance$"))
async def cmd_balance(event):
    user_id = event.sender_id
    if await ensure_user_not_banned(user_id) and user_id not in ADMIN_IDS:
        await event.reply(banned_message())
        return
    balance_usd, balance_inr = await get_balances(user_id)
    await event.reply(
        build_balance_text(balance_usd, balance_inr),
        buttons=[
            [Button.inline("Deposit USDT", b"deposit", icon=5445353829304387411)],
            [Button.inline("🇮🇳 Deposit UPI (₹)", b"deposit_upi")],
            [btn_back()],
        ],
    )


@client.on(events.NewMessage(pattern=r"^/help$"))
async def cmd_help(event):
    if event.sender_id not in ADMIN_IDS and await is_banned(event.sender_id):
        await event.reply(banned_message())
        return
    msg = await event.reply("▍")
    text = (
        "How to use this bot\n\n"
        "1. Tap /menu, then 💵 Deposit.\n"
        "2. Pick your network: BNB Smart Chain (BEP20), Polygon, or Tron (TRC20).\n"
        "3. Enter how much USDT you want to deposit.\n"
        "4. I'll give you an EXACT amount to send (e.g. 10.017).\n"
        "5. Send that exact amount to the address shown, on that network, within\n"
        "   15 minutes.\n"
        "6. Paste your TxID and your balance is credited automatically.\n\n"
        "Deposits are USDT on BNB Smart Chain (BEP20), Polygon, or Tron (TRC20). Send on\n"
        "the SAME network you picked, and the amount must match exactly — the extra\n"
        "digits identify your payment.\n\n"
        "🇮🇳 UPI (₹) deposits:\n"
        "1. Tap /menu, then 🇮🇳 Deposit UPI (₹).\n"
        "2. Enter how much ₹ you want to add.\n"
        "3. Pay to the UPI ID / QR shown using any UPI app.\n"
        "4. Tap 'I've Paid', paste your UTR, and an admin will verify and\n"
        "   credit your ₹ balance. (₹ and $ balances are kept separate.)\n\n"
        "🛒 Shop:\n"
        "Tap /menu, then 🛒 Shop to buy digital products with your $ or ₹\n"
        "balance. Items are delivered here instantly and saved under 🧾 My Orders.\n\n"
        f"👤 Need help? Contact {ADMIN_CONTACT}."
    )
    await msg.edit(text)


@client.on(events.NewMessage(pattern=r"^/commands$"))
async def cmd_commands(event):
    if event.sender_id not in ADMIN_IDS and await is_banned(event.sender_id):
        await event.reply(banned_message())
        return
    msg = await event.reply("▍")
    text = (
        "All Commands\n\n"
        "/menu       — open the menu (deposit, balance, shop)\n"
        "/start      — welcome message\n"
        "/balance        your USD + INR balance\n"
        "/help       — how to deposit (USDT + UPI) and use the shop\n"
        "/commands   — this list"
    )
    await msg.edit(text)


# ── Admin commands ───   ───────────   ──────────────   ─  ───────────  ────────────────


@client.on(events.NewMessage(pattern=r"^/admin$"))
async def admin_panel(event):
    if event.sender_id not in ADMIN_IDS:
        return
    clear_admin_state(event.sender_id)
    await event.reply(await build_admin_panel_text(), buttons=admin_panel_buttons())


@client.on(events.NewMessage(pattern=r"^/emojiid\b"))
async def admin_emoji_id(event):
    if event.sender_id not in ADMIN_IDS:
        return
    # Use the replied-to message if any, otherwise the command message itself.
    msg = await event.get_reply_message() or event.message
    custom = [
        e for e in (msg.entities or []) if isinstance(e, MessageEntityCustomEmoji)
    ]
    if not custom:
        await event.reply(
            "No premium emoji found.\n\n"
            "From a Telegram Premium account, either:\n"
            "• send /emojiid followed by the premium emoji(s) in the SAME message, or\n"
            "• reply to a message containing premium emoji(s) with /emojiid\n\n"
            "Only custom/premium emojis have IDs — normal emojis won't show up."
        )
        return
    utf16 = (msg.message or "").encode("utf-16-le")
    lines, mapping = [], {}
    for e in custom:
        frag = utf16[e.offset * 2 : (e.offset + e.length) * 2].decode(
            "utf-16-le", "ignore"
        )
        lines.append(f"{frag} = {e.document_id}")
        mapping[frag] = e.document_id
    pretty = json.dumps(mapping, ensure_ascii=False)
    await event.reply(
        "Premium emoji IDs:\n\n"
        + "\n".join(lines)
        + "\n\nPaste this JSON back to me and I'll map them:\n`"
        + pretty
        + "`"
    )


@client.on(events.NewMessage(pattern=r"^/user\s+(\d+)$"))
async def admin_user_info(event):
    if event.sender_id not in ADMIN_IDS:
        return
    target_id = int(event.pattern_match.group(1))
    user = await get_user(target_id)
    if not user:
        await event.reply(f"User {target_id} not found.")
        return
    balance_usd, balance_inr = await get_balances(target_id)
    created_at = user.get("created_at", "Unknown")
    await event.reply(
        f"User Information\n\n"
        f"User ID     : {user['user_id']}\n"
        f"USD Balance : ${balance_usd:.2f}\n"
        f"INR Balance : ₹{balance_inr:.2f}\n"
        f"Join Date   : {created_at}"
    )


@client.on(events.NewMessage(pattern=r"^/stats$"))
async def admin_stats(event):
    if event.sender_id not in ADMIN_IDS:
        return
    total_users = await get_total_user_count()
    total_deposit = await get_total_deposit_usd()
    total_upi = await get_total_upi_deposit_inr()
    await event.reply(
        f"Bot Statistics\n\n"
        f"Total Users     : {total_users}\n"
        f"Total Deposited : ${total_deposit:.2f}\n"
        f"UPI Deposited   : ₹{total_upi:.2f}"
    )


@client.on(events.NewMessage(pattern=r"^/broadcast\s+(.+)$", func=lambda e: not e.out))
async def admin_broadcast(event):
    """Fast-path text broadcast: /broadcast <text> launches immediately with
    live progress, cancel, and delete support (same engine as the panel)."""
    if event.sender_id not in ADMIN_IDS:
        return
    message = event.pattern_match.group(1).strip()
    total = len(await get_all_user_ids())
    bcast_id = await bc_create(
        event.sender_id, (message[:60] + "…") if len(message) > 60 else message, total
    )
    status_msg = await event.reply(
        f"📢  Broadcasting  #{bcast_id}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{bc_progress_bar(0, total)}  0%\n\n"
        f"👥 Total:     {total}\n\n"
        "⚡ warming up…",
        buttons=[
            [Button.inline("⛔ Cancel Broadcast", f"adm_bc_stop:{bcast_id}".encode())]
        ],
    )
    client.loop.create_task(
        run_broadcast(bcast_id, message, status_msg.chat_id, status_msg.id)
    )


#   ─ Deposit submission (user pastes a tx hash) ────────  ───────────────────────   


async def handle_wallet_registration(event, user_id: int, text: str):
    AWAITING_WALLET.discard(user_id)
    AWAITING_AMOUNT.discard(user_id)
    PENDING_AMOUNT.pop(user_id, None)
    try:
        wallet = normalize_evm_address(text)
    except ValueError:
        await event.reply(
            "That doesn't look like a wallet address.\n"
            "It should be 0x followed by 40 characters. Open    Deposit to try again.",
            buttons=[
                [
                    Button.inline("Deposit", b"deposit", icon=5445353829304387411),
                    btn_back(),
                ]
            ],
        )
        return

    if DEPOSIT_ADDRESS and wallet.lower() == DEPOSIT_ADDRESS.lower():
        await event.reply(
            "That's the store's receiving address, not your own wallet.\n"
            "Enter the wallet you will PAY FROM.",
            buttons=[
                [
                    Button.inline("🔗 Try Again", b"bind_wallet"),
                    btn_back(),
                ]
            ],
        )
        return

    await set_bound_wallet(user_id, wallet)
    await event.reply(
        "✅  Wallet Registered\n"
        "━━━━━━━━  ━━━━━━━━━━━━━━\n\n"
        f"Your wallet:\n`{wallet}`\n\n"
        "Only deposits sent from this wallet will be credited to you.\n"
        "You can now make a deposit.",
        buttons=[
            [
                Button.inline("Deposit Now", b"deposit", icon=5445353829304387411),
                btn_back(),
            ]
        ],
    )


async def handle_deposit_submission(event, user_id: int, text: str):
    AWAITING_TX.discard(user_id)

    # Find THIS user's live reservation FIRST — it tells us which network the pasted
    # reference belongs to. This matters because the reference format differs by chain:
    # EVM/Tron use a hex tx hash, Solana uses a base58 signature.
    intent = await get_live_intent_for_user(user_id)
    if intent is None:
        await event.reply(
            "❌ You don't have an active deposit right now (or it expired).\n\n"
            "Please start a new deposit, send the exact amount shown, then paste "
            "your TxID.",
            buttons=[
                [
                    Button.inline("New Deposit", b"deposit", icon=5445353829304387411),
                    btn_back(),
                ]
            ],
        )
        return

    chain = intent["chain"]
    intent_id = intent["id"]
    created_at_iso = intent["created_at"]

    # Wallets often copy hashes with stray spaces or line breaks — join the
    # pieces back together before validating so a good hash isn't rejected.
    cleaned_text = "".join((text or "").split())

    txh = normalize_tx_ref(cleaned_text, chain)
    if not txh:
        if chain == "solana":
            hint = "It should be a Solana transaction signature (a long base58 string)."
        else:
            hint = (
                "It should be 64 characters (Tron) or 0x + 64 characters (BSC/Polygon)."
            )
        # Keep the submission flow alive so the user can simply paste the
        # corrected hash right away — no need to reopen Deposit.
        AWAITING_TX.add(user_id)
        await event.reply(
            "❌ Wrong hash ID — try again.\n"
            f"{hint}\n\n"
            "Just paste the correct hash here.",
            buttons=[
                [
                    Button.inline(
                        "Cancel",
                        b"deposit",
                        icon=5397586104981403273,
                        style="primary",
                    )
                ]
            ],
        )
        return

    if await deposit_exists(txh):
        AWAITING_TX.add(user_id)
        await event.reply(
            "⚠️ That transaction has already been used for a deposit.\n\n"
            "Paste a different hash to try again.",
            buttons=[
                [
                    Button.inline(
                        "Cancel",
                        b"deposit",
                        icon=5397586104981403273,
                        style="primary",
                    )
                ]
            ],
        )
        return

    status = await event.reply("🔎 Verifying your transaction on-chain…")

    connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
    kind = (DEPOSIT_TOKENS.get(chain) or {}).get("kind", "evm")
    coin_price = None
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            if kind in ("ton", "ltc", "btc"):
                # Native coins are verified by scanning our own address for
                # the exact reserved amount (wallet hash formats are
                # unreliable), and the USD credit uses the live price.
                verifier = {
                    "ton": verify_ton_deposit,
                    "ltc": verify_ltc_deposit,
                    "btc": verify_btc_deposit,
                }[kind]
                result = await verifier(
                    session,
                    DEPOSIT_TOKENS[chain],
                    intent["amount_milli"],
                    created_at_iso,
                )
                coin_price = await get_coin_price_usd(kind, session)
            else:
                result = await verify_usdt_deposit(session, txh, chain)
    except DepositError as e:
        # Wrong hash ID (valid format, not found / not matching on-chain) —
        # keep listening so the user just pastes the corrected hash here.
        AWAITING_TX.add(user_id)
        await status.edit(
            f"❌ Wrong hash ID — try again.\n{e}\n\n"
            "Just paste the correct hash here.\n\n"
            f"Still stuck? Contact {ADMIN_CONTACT} for help.",
            buttons=[
                [
                    Button.inline(
                        "Cancel",
                        b"deposit",
                        icon=5397586104981403273,
                        style="primary",
                    )
                ]
            ],
        )
        return
    except Exception:
        # Temporary network/API failure — the pasted hash may be fine, so let
        # the user simply paste it again without restarting the deposit.
        AWAITING_TX.add(user_id)
        await status.edit(
            "⚠️ Something went wrong while verifying. Paste your hash again "
            "to retry.\n\n"
            f"If it keeps happening, contact {ADMIN_CONTACT} for help.",
            buttons=[
                [
                    Button.inline(
                        "Cancel",
                        b"deposit",
                        icon=5397586104981403273,
                        style="primary",
                    )
                ]
            ],
        )
        return

    amount = result["amount"]
    amount_milli = int(round(amount * intent_unit_scale(chain)))
    if coin_price is not None:
        usd_value = amount * coin_price  # coin → USD at the live price
        txh = result["tx_hash"]  # canonical on-chain hash (dedupes reliably)
    else:
        usd_value = amount  # USDT ≈ $1

    # Unique-amount matching: the payment must equal the exact odd amount THIS user
    # reserved. The odd amount + 15-min window is what ties the on-chain payment to
    # the right person, so no one can claim a deposit they didn't request.
    if amount_milli != intent["amount_milli"]:
        await status.edit(
            "❌ We couldn't match this payment to your deposit.\n\n"
            f"You must send the EXACT amount you were given "
            f"({format_intent_amount(chain, intent['amount_milli'])} "
            f"{DEPOSIT_TOKENS.get(chain, DEPOSIT_TOKENS['bsc'])['symbol']}), "
            "and pay within the 15-minute window.\n\n"
            "This can happen if you sent a rounded amount, sent a different amount, "
            "or the reservation expired. Please start a new deposit and send the "
            "exact amount shown.\n\n"
            f"❓ Already paid the exact amount? Contact {ADMIN_CONTACT} for help.",
            buttons=[
                [
                    Button.inline("New Deposit", b"deposit", icon=5445353829304387411),
                    btn_back(),
                ]
            ],
        )
        for admin_id in ADMIN_IDS:
            await notify_user(
                admin_id,
                f"⚠️ Unmatched deposit\nUser: {user_id}\n"
                f"Network: {chain}\n"
                f"Sent: {amount:.8f} {DEPOSIT_TOKENS.get(chain, DEPOSIT_TOKENS['bsc'])['symbol']} "
                f"(expected {format_intent_amount(chain, intent['amount_milli'])})\n"
                f"Tx: {txh}\n"
                "(Amount didn't match the reservation — may need manual review.)",
            )
        return

    # Extra safety: the payment must not predate the reservation (blocks reusing an
    # old coincidental transaction of the same amount).
    block_time = result.get("block_time")
    if block_time and created_at_iso:
        try:
            created_at = datetime.fromisoformat(created_at_iso)
            if block_time < created_at - timedelta(minutes=2):
                await status.edit(
                    "❌ This transaction was made BEFORE your deposit request, so it "
                    "can't be matched.\n\n"
                    "Please start a new deposit and pay the exact amount shown.",
                    buttons=[
                        [
                            Button.inline("New Deposit", b"deposit", icon=5445353829304387411),
                            btn_back(),
                        ]
                    ],
                )
                return
        except ValueError:
            pass  # unparseable timestamp → skip this non-critical check

    if usd_value + 1e-9 < MIN_DEPOSIT_USD:
        await status.edit(
            f"❌ Deposit too small (${usd_value:.2f}). Minimum is ${MIN_DEPOSIT_USD:.2f}.",
            buttons=[[btn_back()]],
        )
        return

    # Consume the reservation AND record+credit the deposit as one atomic unit, so an
    # intent or tx hash can never be consumed without also crediting the balance, and a
    # payment can never be credited twice (UNIQUE tx_hash + single-winner intent claim).
    symbol = DEPOSIT_TOKENS.get(chain, DEPOSIT_TOKENS["bsc"])["symbol"]
    result, new_balance = await claim_and_credit_deposit(
        intent_id, user_id, amount_milli, txh, symbol, amount, usd_value
    )
    if result == "not_claimed":
        await status.edit(
            "⚠️ This deposit was already processed, or your reservation just expired.\n\n"
            f"If you paid and weren't credited, please contact {ADMIN_CONTACT}.",
            buttons=[[btn_back()]],
        )
        return
    if result == "dup_tx":
        await status.edit(
            "⚠️ That transaction has already been used for a deposit.",
            buttons=[[btn_back()]],
        )
        return

    PENDING_AMOUNT.pop(user_id, None)

    # Direct "tap and pay": if this credit belongs to a pending product
    # purchase, buy and deliver the product immediately.
    direct = DIRECT_BUY_PENDING.get(user_id)
    if direct and direct.get("intent_id") == intent_id:
        DIRECT_BUY_PENDING.pop(user_id, None)
        buy_qty = max(1, int(direct.get("qty", 1)))
        buy_status, payload = await buy_product(
            user_id, direct["pid"], "usd", buy_qty
        )
        if buy_status == "supplier_order":
            text, rows = await SUPPLIER_PANEL.order_view(payload, user_id)
            await status.edit(text, buttons=rows)
            return
        if buy_status == "supplier_error":
            balance = await get_balance_usd(user_id)
            await status.edit(
                f"Deposit Confirmed\n\nYour payment was added to your wallet.\n"
                f"Purchase could not be completed: {payload}\n"
                f"USD Balance: ${balance:.2f}",
                buttons=[[btn_orders()], [btn_back()]], parse_mode=None)
            return
        if buy_status == "ok":
            bought_qty = payload.get("qty", 1)
            item_word = "Your items:" if bought_qty > 1 else "Your item:"
            saved_word = "They're" if bought_qty > 1 else "It's"
            qty_line = (
                f"[➕](emoji/6025873488911274008) **Quantity: {bought_qty}**\n\n"
                if bought_qty > 1
                else ""
            )
            await status.edit(
                "[✅](emoji/5181903361456210670) **PURCHASE COMPLETE**\n"
                "**  ━━━━━━━━━━━━━━━━━━━━━━**\n\n"
                f"[📦](emoji/5884479287171485878) **Product: {payload['name']}**\n"
                f"{qty_line}"
                f"[✔️](emoji/5278411813468269386) **Paid: "
                f"{amount:.3f} {symbol} (~${usd_value:.2f})**\n\n"
                f"[💳](emoji/6143398710551252398) **New Balance: "
                f"${payload['new_balance']:.2f}**\n"
                "**━━━━━━━━━━━━━━━━━━━━━━━**\n"
                f"[🛍](emoji/5431597233738883049) **{item_word}**\n\n"
                f"`{payload['content']}`\n\n"
                f"**{saved_word} also saved under** "
                "[🧾](emoji/5444856076954520455) **My Orders.**",
                buttons=[
                    [Button.inline("🧾 My Orders", b"orders")],
                    [btn_back()],
                ],
            )
            for admin_id in ADMIN_IDS:
                await notify_user(
                    admin_id,
                    f"⚡ Direct crypto purchase\nUser: {user_id}\n"
                    f"Product: {payload['name']}\n"
                    f"Quantity: {bought_qty}\n"
                    f"Paid: {amount:.3f} {symbol} (~${usd_value:.2f})\nTx: {txh}",
                )
            await log_to_group(
                "🛒 New Purchase\n"
                f"User ID: {user_id}\n"
                f"Product: {payload['name']}\n"
                f"Payment: Crypto ({symbol})\n"
                f"Amount / Quantity: {amount:.3f} {symbol} "
                f"(~${usd_value:.2f}) / {bought_qty}\n"
                f"Time: {ist_now_str()}"
            )
            return
        # Purchase failed (sold out meanwhile, etc.) — balance is credited, so
        # fall through to the normal deposit-confirmed message.

    await status.edit(
        "✅  Deposit Confirmed\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Received: {amount:.3f} {symbol}  (~${usd_value:.2f})\n"
        f"New Balance: ${new_balance:.2f}\n\n"
        "Tap 💰 My Balance any time to check it.",
        buttons=[
            [
                Button.inline("💰 My Balance", b"balance"),
                btn_back(),
            ]
        ],
    )
    for admin_id in ADMIN_IDS:
        await notify_user(
            admin_id,
            f"💵 New deposit\nUser: {user_id}\nAmount: {amount:.3f} {symbol} (~${usd_value:.2f})\nTx: {txh}",
        )
    await log_to_group(
        "💰 New Wallet Top-Up\n"
        f"User ID: {user_id}\n"
        f"Payment: Crypto ({symbol})\n"
        f"Amount: {amount:.3f} {symbol} (~${usd_value:.2f})\n"
        f"Time: {ist_now_str()}"
    )


async def handle_amount_entry(event, user_id: int, text: str):
    """User typed how much they want to deposit → show the address to pay to."""
    raw = text.strip().lstrip("$").replace(",", "")
    try:
        amount = float(raw)
    except ValueError:
        await event.reply(
            "That doesn't look like a number. Enter an amount like 10.",
            buttons=[
                [
                    Button.inline("Deposit", b"deposit", icon=5445353829304387411),
                    btn_back(),
                ]
            ],
        )
        return

    if amount + 1e-9 < MIN_DEPOSIT_USD:
        await event.reply(
            f"Minimum deposit is ${MIN_DEPOSIT_USD:.2f}. Enter a larger amount.",
            buttons=[
                [
                    Button.inline("Deposit", b"deposit", icon=5445353829304387411),
                    btn_back(),
                ]
            ],
        )
        return

    AWAITING_AMOUNT.discard(user_id)
    chain = DEPOSIT_CHAIN_CHOICE.get(user_id, DEPOSIT_CHAINS[0])
    # Native coins (TON, LTC): entered amount is USD but the payment is in the
    # coin — convert at the live price so the reserved amount is in-coin.
    _kind = (DEPOSIT_TOKENS.get(chain) or {}).get("kind")
    _usd_amt_custom = amount if _kind in ("ton", "ltc", "btc") else None
    if _kind in ("ton", "ltc", "btc"):
        try:
            price = await get_coin_price_usd(_kind)
        except DepositError as e:
            await event.reply(
                f"❌ {e}",
                buttons=[[Button.inline("💵 Try Again", b"deposit"), btn_back()]],
            )
            return
        amount = amount / price
    intent = await create_deposit_intent(user_id, amount, chain, usd_amount=_usd_amt_custom)
    if intent is None:
        await event.reply(
            "⚠️ Too many deposits are in progress right now. Please try again in a "
            "few minutes.",
            buttons=[
                [
                    Button.inline("💵 Try Again", b"deposit"),
                    btn_back(),
                ]
            ],
        )
        return
    qr_path = SCREEN_BANNER_FILES.get(f"qr_{chain}")
    if qr_path and os.path.exists(qr_path):
        try:
            msg = await event.reply(
                build_deposit_text(intent),
                buttons=deposit_buttons(),
                file=cached_banner_file(qr_path),
            )
            remember_banner_upload(qr_path, msg)
            return
        except Exception:
            forget_banner_upload(qr_path)
    await event.reply(build_deposit_text(intent), buttons=deposit_buttons())


# ── UPI deposit input flows ────────────────────  ─   ─────────────────────────────


async def handle_upi_amount_entry(event, user_id: int, text: str):
    """User typed how many ₹ they want to add via UPI → show UPI ID + QR to pay."""
    if not upi_ready():
        AWAITING_UPI_AMOUNT.discard(user_id)
        await event.reply(
            "UPI deposits aren't available right now.",
            buttons=[[btn_back()]],
        )
        return
    raw = text.strip().lstrip("₹").replace(",", "")
    try:
        amount = float(raw)
    except ValueError:
        await event.reply(
            "That doesn't look like a number. Enter an amount like 500.",
            buttons=[
                [
                    Button.inline("🇮🇳 Deposit UPI", b"deposit_upi"),
                    btn_back(),
                ]
            ],
        )
        return
    # Reject NaN / Inf / non-positive / absurdly large amounts (financial integrity).
    if not math.isfinite(amount) or amount <= 0 or amount > 10_000_000:
        await event.reply(
            "That's not a valid amount. Enter a positive number like 500.",
            buttons=[
                [
                    Button.inline("🇮🇳 Deposit UPI", b"deposit_upi"),
                    btn_back(),
                ]
            ],
        )
        return
    if amount + 1e-9 < MIN_UPI_INR:
        await event.reply(
            f"Minimum UPI deposit is ₹{MIN_UPI_INR:.0f}. Enter a larger amount.",
            buttons=[
                [
                    Button.inline("🇮🇳 Deposit UPI", b"deposit_upi"),
                    btn_back(),
                ]
            ],
        )
        return

    AWAITING_UPI_AMOUNT.discard(user_id)
    req_id = await create_upi_request(user_id, amount)
    PENDING_UPI_REQ[user_id] = req_id
    caption = (
        "📲 Pay via UPI ID or QR CODE\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Amount to pay:     {amount:.2f}\n"
        f"UPI ID:  `{UPI_ID}`\n\n"
        "Scan the QR above OR pay to the UPI ID using any UPI app\n\n"
        "(GPay, PhonePe, Paytm, …).\n\n"
        f"☑️ Pay EXACTLY ₹{amount:.2f}\n\n"
        "After paying click \"✔️ I Have Paid\" & paste your UTR or\n"
        "Transaction ID so an admin can verify it.\n\n"
        f"❔ Need help - Dm {ADMIN_CONTACT}."
    )
    buttons = [
        [
            Button.inline(
                "I've Paid — Submit UTR",
                f"upi_submit:{req_id}".encode(),
                icon=PREMIUM_EMOJI_MAP.get("✔️"),
            )
        ],
        [btn_back()],
    ]
    try:
        # Reuse the cached Telegram photo when available — re-uploading the QR
        # PNG from disk on every deposit made this page feel slow.
        qr_file = cached_banner_file(UPI_QR_PATH)
        try:
            msg = await client.send_file(
                user_id, qr_file, caption=caption, buttons=buttons
            )
            remember_banner_upload(UPI_QR_PATH, msg)
        except Exception:
            if qr_file is not UPI_QR_PATH:
                # Stale cached photo reference — retry once from disk.
                forget_banner_upload(UPI_QR_PATH)
                msg = await client.send_file(
                    user_id, UPI_QR_PATH, caption=caption, buttons=buttons
                )
                remember_banner_upload(UPI_QR_PATH, msg)
            else:
                raise
    except Exception:
        # Fall back to a text-only message if the QR can't be sent.
        await event.reply(caption, buttons=buttons)


async def handle_upi_utr_entry(event, user_id: int, text: str):
    """User pasted their UTR → mark the request pending and notify admins."""
    req_id = PENDING_UPI_REQ.get(user_id)
    if not req_id:
        AWAITING_UPI_UTR.discard(user_id)
        await event.reply(
            "This deposit session expired. Start again from the menu.",
            buttons=[
                [
                    Button.inline("🇮🇳 Deposit UPI", b"deposit_upi"),
                    btn_back(),
                ]
            ],
        )
        return
    utr = text.strip()
    # UTRs are typically 12 digits, but references vary; accept 6–30 alphanumerics.
    cleaned = utr.replace(" ", "")
    if not (6 <= len(cleaned) <= 30) or not cleaned.isalnum():
        await event.reply(
            "That doesn't look like a valid UTR. It's usually a 12-digit number\n"
            "from your UPI app. Paste it again.",
            buttons=[[btn_back()]],
        )
        return
    ok = await attach_upi_utr(req_id, user_id, cleaned)
    if not ok:
        AWAITING_UPI_UTR.discard(user_id)
        PENDING_UPI_REQ.pop(user_id, None)
        await event.reply(
            "This deposit request is no longer active. Start again from the menu.",
            buttons=[
                [
                    Button.inline("🇮🇳 Deposit UPI", b"deposit_upi"),
                    btn_back(),
                ]
            ],
        )
        return

    AWAITING_UPI_UTR.discard(user_id)
    PENDING_UPI_REQ.pop(user_id, None)
    req = await get_upi_request(req_id)
    amount_inr = req["amount_inr"] if req else 0.0
    await event.reply(
        "✅  Submitted for Review\n"
        "  ━━━━━━━━━━━━━━━━━━━\n\n"
        f"Your UPI deposit of ₹{amount_inr:.2f} is now pending admin approval.\n"
        f"UTR: `{cleaned}`\n\n"
        "You'll be notified once it's approved.",
        buttons=[
            [
                Button.inline(
                    "My Balance",
                    b"balance",
                    icon=5397586104981403273,
                    style="primary",
                )
            ],
            [btn_back()],
        ],
    )
    await notify_admins(
        "🇮🇳 New UPI Deposit — Needs Approval\n"
        "━━━━━━━━━━━━━━━━━━━━━━   \n\n"
        f"User: `{user_id}`\n"
        f"Amount: ₹{amount_inr:.2f}\n"
        f"UTR: `{cleaned}`\n"
        f"Request: #{req_id}\n\n"
        "Verify the payment, then approve or reject:",
        buttons=[
            [
                Button.inline(f"✅ Approve #{req_id}", f"upi_ok:{req_id}".encode()),
                Button.inline(f"🚫 Reject #{req_id}", f"upi_no:{req_id}".encode()),
            ]
        ],
    )


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


async def handle_email_entry(event, user_id: int, text: str):
    """User typed their email for a manual (email-invite) purchase → move the
    order to 'processing' and notify admins with a 'Mail Sent' button."""
    order_id = PENDING_EMAIL_ORDER.get(user_id)
    if not order_id:
        AWAITING_EMAIL.discard(user_id)
        await event.reply(
            "This purchase session expired. Check 🧾 My Orders.",
            buttons=[
                [
                    Button.inline("🧾 My Orders", b"orders"),
                    btn_back(),
                ]
            ],
        )
        return
    email = text.strip()
    if not EMAIL_RE.match(email) or len(email) > 254:
        await event.reply(
            "That doesn't look like a valid email address. Please type it again\n"
            "(e.g. `you@example.com`).",
            buttons=[[Button.inline("✖️ Cancel", b"orders")]],
        )
        return
    order = await attach_order_email(order_id, user_id, email)
    if not order:
        AWAITING_EMAIL.discard(user_id)
        PENDING_EMAIL_ORDER.pop(user_id, None)
        AWAITING_QTY.pop(user_id, None)
        await event.reply(
            "This purchase is no longer awaiting an email. Check 🧾 My Orders.",
            buttons=[
                [
                    Button.inline("🧾 My Orders", b"orders"),
                    btn_back(),
                ]
            ],
        )
        return
    AWAITING_EMAIL.discard(user_id)
    PENDING_EMAIL_ORDER.pop(user_id, None)
    name = order.get("product_name") or "your product"
    await event.reply(
        "✅  Email Received\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Product: {name}\n"
        f"Email: `{email}`\n\n"
        "⏳ Your invite will be delivered within **1 hour**. We'll notify you\n"
        "here as soon as it's sent.\n\n"
        f"❓ Questions? Contact {ADMIN_CONTACT}.",
        buttons=[
            [Button.inline("🧾 My Orders", b"orders")],
            [btn_back()],
        ],
    )
    sym = "$" if order["currency"] == "usd" else "₹"
    await notify_admins(
        "📧 New Manual Order — Send Invite\n"
        "━━━━     ━━━━━━━━━━━━━━━━━\n\n"
        f"User: `{user_id}`\n"
        f"Product: {name}\n"
        f"Paid: {sym}{order['amount']:.2f}\n"
        f"Email: `{email}`\n"
        f"Order: #{order_id}\n\n"
        "Send the invite to that email, then tap below:",
        buttons=[
            [
                Button.inline(
                    f"✅ Mail Sent #{order_id}", f"omail_sent:{order_id}".encode()
                )
            ],
        ],
    )


# ── Admin panel input flows (multi-step text entry) ────────────────────────────


async def handle_admin_input(event, user_id: int, text: str):
    """Drive the admin panel's multi-step text-input flows (balance edits, bans,
    address changes, min deposit, broadcast). State lives in ADMIN_STATE."""
    state = ADMIN_STATE.get(user_id)
    if not state:
        return
    if state.get("action") == "supplier":
        await SUPPLIER_PANEL.admin_input(event, user_id, text)
        return
    action = state["action"]
    step = state["step"]
    data = state["data"]
    back = admin_panel_buttons()

    # ── Add / remove balance: step 1 = user id, step 2 = amount ──
    if action in ("addbal", "rmbal", "addbalinr", "rmbalinr"):
        if step == "user_id":
            if not text.isdigit():
                await event.reply(
                    "Send a numeric user ID (digits only).",
                    buttons=admin_cancel_buttons(),
                )
                return
            target = int(text)
            if not await get_user(target):
                await event.reply(
                    f"No user with ID {target} exists in the bot yet.",
                    buttons=admin_cancel_buttons(),
                )
                return
            data["target"] = target
            state["step"] = "amount"
            is_inr = action in ("addbalinr", "rmbalinr")
            is_add = action in ("addbal", "addbalinr")
            bal = await (get_balance_inr(target) if is_inr else get_balance_usd(target))
            sym = "₹" if is_inr else "$"
            verb = "add to" if is_add else "remove from"
            await event.reply(
                f"User {target} — current {sym} balance {sym}{bal:.2f}.\n\n"
                f"How much {sym} to {verb} their balance? (e.g. 10)",
                buttons=admin_cancel_buttons(),
            )
            return
        # step == "amount"
        is_inr = action in ("addbalinr", "rmbalinr")
        is_add = action in ("addbal", "addbalinr")
        sym = "₹" if is_inr else "$"
        try:
            amt = float(text.strip().lstrip("$").lstrip("₹").replace(",", ""))
        except ValueError:
            await event.reply("Send a number, e.g. 10.", buttons=admin_cancel_buttons())
            return
        if not math.isfinite(amt) or amt <= 0 or amt > 10_000_000:
            await event.reply(
                "Amount must be a positive number.", buttons=admin_cancel_buttons()
            )
            return
        target = data["target"]
        delta = amt if is_add else -amt
        res = await (
            admin_adjust_balance_inr(target, delta)
            if is_inr
            else admin_adjust_balance(target, delta)
        )
        clear_admin_state(user_id)
        if res is None:
            await event.reply("That user no longer exists.", buttons=back)
            return
        old, new = res
        await log_admin_action(
            user_id,
            target,
            ("add_balance_inr" if is_add else "remove_balance_inr")
            if is_inr
            else ("add_balance" if is_add else "remove_balance"),
            amt,
            f"{old}->{new}",
        )
        sign = "+" if is_add else "-"
        await event.reply(
            f"✅ {sym} Balance updated for {target}.\n\n"
            f"{sign}{sym}{amt:.2f}\n"
            f"Old: {sym}{old:.2f}  →  New: {sym}{new:.2f}",
            buttons=back,
        )
        try:
            if is_add:
                await client.send_message(
                    target,
                    f"✅ {sym}{amt:.2f} was added to your balance by support.\n"
                    f"New balance: {sym}{new:.2f}",
                )
            else:
                await client.send_message(
                    target,
                    f"➖ {sym}{amt:.2f} was removed from your balance by support.\n"
                    f"New balance: {sym}{new:.2f}",
                )
        except Exception:
            pass
        return

    # ── Find user ──
    if action == "find":
        if not text.isdigit():
            await event.reply(
                "Send a numeric user ID (digits only).", buttons=admin_cancel_buttons()
            )
            return
        target = int(text)
        u = await get_user(target)
        clear_admin_state(user_id)
        if not u:
            await event.reply(f"No user with ID {target}.", buttons=back)
            return
        bal, bal_inr = await get_balances(target)
        cnt, tot = await get_user_deposit_total(target)
        banned = bool(u.get("banned"))
        await event.reply(
            "🔍  User Info\n"
            "━━━━━━━━━  ━━━━━━━━━━━━━\n\n"
            f"User ID   : `{target}`\n"
            f"USD Bal   : ${bal:.2f}\n"
            f"INR Bal   : ₹{bal_inr:.2f}\n"
            f"Deposits  : {cnt} (${tot:.2f} total)\n"
            f"Status    : {'🚫 BANNED' if banned else '✅ Active'}\n"
            f"Joined    : {u.get('created_at', 'Unknown')}",
            buttons=[
                [
                    Button.inline("➕ Add $", b"adm_addbal"),
                    Button.inline("➖ Remove $", b"adm_rmbal"),
                ],
                [
                    Button.inline("➕ Add ₹", b"adm_addbalinr"),
                    Button.inline("➖ Remove ₹", b"adm_rmbalinr"),
                ],
                [
                    Button.inline(
                        "✅ Unban" if banned else "    Ban",
                        b"adm_unban" if banned else b"adm_ban",
                    )
                ],
                [Button.inline("🏠 Home to Admin", b"admin")],
            ],
        )
        return

    # ── Delete user data ──
    if action == "deluser":
        if not text.isdigit():
            await event.reply(
                "Send a numeric user ID (digits only).", buttons=admin_cancel_buttons()
            )
            return
        target = int(text)
        if target in ADMIN_IDS:
            clear_admin_state(user_id)
            await event.reply("You can't delete an admin.", buttons=back)
            return
        clear_admin_state(user_id)
        bal_usd, bal_inr = await get_balances(target)
        await event.reply(
            "⚠️  Confirm Deletion\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"User ID: `{target}`\n"
            f"Balance: ${bal_usd:.2f} / ₹{bal_inr:.2f}\n\n"
            "This PERMANENTLY erases their profile, balances, orders,\n"
            "deposits, UPI requests and referrals.\n"
            "This cannot be undone.",
            buttons=[
                [
                    Button.inline(
                        "🗑 Yes, delete everything",
                        f"adm_deluser_go:{target}".encode(),
                    )
                ],
                [Button.inline("↩️ Cancel", b"admin")],
            ],
        )
        return

    # ── Ban / unban ──
    if action in ("ban", "unban"):
        if not text.isdigit():
            await event.reply(
                "Send a numeric user ID (digits only).", buttons=admin_cancel_buttons()
            )
            return
        target = int(text)
        if target in ADMIN_IDS:
            clear_admin_state(user_id)
            await event.reply("You can't ban an admin.", buttons=back)
            return
        do_ban = action == "ban"
        await set_banned(target, do_ban)
        await log_admin_action(user_id, target, action, None, "")
        clear_admin_state(user_id)
        await event.reply(
            f"{'🚫 Banned' if do_ban else '✅ Unbanned'} user {target}.",
            buttons=back,
        )
        try:
            if do_ban:
                await client.send_message(
                    target,
                    f"🚫 Your access to this bot has been suspended.\n"
                    f"Contact {ADMIN_CONTACT} if you think this is a mistake.",
                )
            else:
                await client.send_message(
                    target, "✅ Your access to this bot has been restored."
                )
        except Exception:
            pass
        return

    # ── Set deposit address ──
    if action == "setaddr":
        kind = data["kind"]
        skey, vkind, label = ADDR_KINDS[kind]
        cleaned = validate_deposit_address(vkind, text)
        if not cleaned:
            await event.reply(
                f"That doesn't look like a valid {label} address. Try again.",
                buttons=admin_cancel_buttons(),
            )
            return
        await set_setting(skey, cleaned)
        await apply_settings_overrides()
        await log_admin_action(user_id, None, "set_address", None, f"{kind}={cleaned}")
        clear_admin_state(user_id)
        await event.reply(
            f"✅ {label} deposit address updated to:\n`{cleaned}`",
            buttons=back,
        )
        return

    # ── Set minimum deposit ──
    if action == "setmin":
        try:
            val = float(text.strip().lstrip("$").replace(",", ""))
        except ValueError:
            await event.reply("Send a number, e.g. 5.", buttons=admin_cancel_buttons())
            return
        if val < 0:
            await event.reply(
                "Minimum can't be negative.", buttons=admin_cancel_buttons()
            )
            return
        await set_setting("min_deposit", str(val))
        await apply_settings_overrides()
        await log_admin_action(user_id, None, "set_min_deposit", val, "")
        clear_admin_state(user_id)
        await event.reply(f"✅ Minimum deposit set to ${val:.2f}.", buttons=back)
        return

    # ── Set low-stock alert threshold ──
    if action == "setlowstock":
        try:
            val = int(text.strip().replace(",", ""))
        except ValueError:
            await event.reply(
                "Send a whole number, e.g. 3.", buttons=admin_cancel_buttons()
            )
            return
        if val < 0:
            await event.reply(
                "Threshold can't be negative.", buttons=admin_cancel_buttons()
            )
            return
        await set_setting("low_stock", str(val))
        await log_admin_action(user_id, None, "set_low_stock", val, "")
        clear_admin_state(user_id)
        if val == 0:
            note = "Admins will be alerted only when a product sells out."
        else:
            note = (
                f"Admins will be alerted when a product drops to {val} or fewer left."
            )
        await event.reply(
            f"✅ Low-stock alert threshold set to {val}.\n{note}", buttons=back
        )
        return

    #        Set UPI ID ──
    if action == "setupiid":
        vpa = text.strip()
        # A UPI VPA looks like name@handle — a loose but useful sanity check.
        if "@" not in vpa or len(vpa) < 3 or " " in vpa:
            await event.reply(
                "That doesn't look like a UPI ID. It should look like `name@bank`.",
                buttons=admin_cancel_buttons(),
            )
            return
        await set_setting("upi_id", vpa)
        await apply_settings_overrides()
        await log_admin_action(user_id, None, "set_upi_id", None, vpa)
        clear_admin_state(user_id)
        await event.reply(f"✅ UPI ID updated to:\n`{vpa}`", buttons=back)
        return

    # ── Set UPI QR (photo upload) ──
    if action == "setupiqr":
        if not event.photo and not (
            event.document
            and (event.file and (event.file.mime_type or "").startswith("image/"))
        ):
            await event.reply(
                "Please send the QR as an image (photo).",
                buttons=admin_cancel_buttons(),
            )
            return
        try:
            await event.download_media(file=UPI_QR_PATH)
        except Exception:
            await event.reply(
                "Couldn't save that image. Try again.", buttons=admin_cancel_buttons()
            )
            return
        await log_admin_action(user_id, None, "set_upi_qr", None, "")
        clear_admin_state(user_id)
        await event.reply(
            "✅ UPI QR image saved.\n"
            f"{'🟢 UPI deposits are now ENABLED.' if upi_ready() else '⚠️ Still need a UPI ID to enable UPI deposits.'}",
            buttons=back,
        )
        return

    # ── Set minimum UPI deposit ──
    if action == "setminupi":
        try:
            val = float(text.strip().lstrip("₹").replace(",", ""))
        except ValueError:
            await event.reply(
                "Send a number, e.g. 100.", buttons=admin_cancel_buttons()
            )
            return
        if not math.isfinite(val) or val < 0 or val > 10_000_000:
            await event.reply(
                "Send a valid non-negative amount, e.g. 100.",
                buttons=admin_cancel_buttons(),
            )
            return
        await set_setting("min_upi", str(val))
        await apply_settings_overrides()
        await log_admin_action(user_id, None, "set_min_upi", val, "")
        clear_admin_state(user_id)
        await event.reply(f"✅ Minimum UPI deposit set to ₹{val:.2f}.", buttons=back)
        return

    # ── Set product image (photo upload) ──
    if action == "editimg":
        if text.strip() == "-":
            # Clear the image
            await set_product_image(data["pid"], None)
            clear_admin_state(user_id)
            await log_admin_action(user_id, None, "shop_img_clear", None, f"pid={data['pid']}")
            await event.reply(
                "✅ Product image removed.",
                buttons=[
                    [Button.inline("📦 Manage Product", f"adm_prod:{data['pid']}".encode())],
                    [Button.inline("🏠 Home to Shop", b"adm_shop")],
                ],
            )
            return
        if not event.photo and not (
            event.document
            and (event.file and (event.file.mime_type or "").startswith("image/"))
        ):
            await event.reply(
                "Please send a photo (image).", buttons=admin_cancel_buttons()
            )
            return
        try:
            # Save the image to local disk (product_images/<pid>.jpg) and store
            # the path. This is fully reliable across Telethon versions —
            # pack_bot_file_id breaks on PhotoSize in newer layers.
            img_dir = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "product_images")
            os.makedirs(img_dir, exist_ok=True)
            img_path = os.path.join(img_dir, f"{data['pid']}.jpg")
            saved = await event.download_media(file=img_path)
            if not saved or not os.path.exists(saved):
                await event.reply(
                    "Couldn't process that image. Try again.",
                    buttons=admin_cancel_buttons(),
                )
                return
            await set_product_image(data["pid"], saved)
            clear_admin_state(user_id)
            await log_admin_action(
                user_id, None, "shop_img_set", None, f"pid={data['pid']}"
            )
            await event.reply(
                "✅ Product image saved. Buyers will see it in the shop.",
                buttons=[
                    [Button.inline("📦 Manage Product", f"adm_prod:{data['pid']}".encode())],
                    [Button.inline("🏠 Home to Shop", b"adm_shop")],
                ],
            )
        except Exception as e:
            await event.reply(
                f"Error saving image: {e}", buttons=admin_cancel_buttons()
            )
        return

    # ── Set category image (photo upload) ──
    if action == "editcatimg":
        cid = data["cid"]
        if text.strip() == "-":
            await set_category_image(cid, None)
            clear_admin_state(user_id)
            await log_admin_action(user_id, None, "cat_img_clear", None, f"cid={cid}")
            await event.reply(
                "✅ Category image removed. The shop banner shows instead.",
                buttons=[
                    [Button.inline("🗂 Open Category", f"adm_cat:{cid}".encode())],
                    [Button.inline("🏠 Home to Shop", b"adm_shop")],
                ],
            )
            return
        if not event.photo and not (
            event.document
            and (event.file and (event.file.mime_type or "").startswith("image/"))
        ):
            await event.reply(
                "Please send a photo (image).", buttons=admin_cancel_buttons()
            )
            return
        try:
            # Save to local disk (category_images/<cid>.jpg) and store the path,
            # same reliable approach as product images.
            img_dir = os.path.join(
                os.path.dirname(os.path.abspath(DB_PATH)), "category_images"
            )
            os.makedirs(img_dir, exist_ok=True)
            img_path = os.path.join(img_dir, f"{cid}.jpg")
            saved = await event.download_media(file=img_path)
            if not saved or not os.path.exists(saved):
                await event.reply(
                    "Couldn't process that image. Try again.",
                    buttons=admin_cancel_buttons(),
                )
                return
            await set_category_image(cid, saved)
            clear_admin_state(user_id)
            await log_admin_action(user_id, None, "cat_img_set", None, f"cid={cid}")
            await event.reply(
                "✅ Category image saved. Buyers will see it when they open "
                "this category in the shop.",
                buttons=[
                    [Button.inline("🗂 Open Category", f"adm_cat:{cid}".encode())],
                    [Button.inline("🏠 Home to Shop", b"adm_shop")],
                ],
            )
        except Exception as e:
            await event.reply(
                f"Error saving image: {e}", buttons=admin_cancel_buttons()
            )
        return

    # ── Edit product name / description ──
    if action == "editmeta":
        pid = data["pid"]
        p = await get_product(pid)
        if not p:
            clear_admin_state(user_id)
            await event.reply("That product no longer exists.", buttons=back)
            return
        if step == "name":
            name, emoji_char, emoji_id, err = await parse_category_input(
                event, text, label="product", maxlen=120
            )
            if err:
                await event.reply(err, buttons=admin_cancel_buttons())
                return
            old_char = p.get("emoji_char")
            await set_product_meta(
                pid,
                name,
                p["description"] or "",
                emoji_id=emoji_id,
                emoji_char=emoji_char,
            )
            if emoji_id:
                register_category_emoji(emoji_char, emoji_id)
                if old_char and old_char != emoji_char:
                    await unregister_category_emoji(old_char)
            note = f"pid={pid} name"
            done = f"✅ Name updated to: {name}"
        elif step == "warranty_days":
            try:
                days = int(text.strip())
                await set_product_warranty(pid, days)
            except ValueError:
                await event.reply(
                    "Send a whole number from 0 to 36500 days (0 for no warranty).",
                    buttons=admin_cancel_buttons(),
                )
                return
            note = f"pid={pid} warranty_days={days}"
            done = f"✅ Warranty updated to {days} days."
        elif step == "learn_more":
            if text.strip() == "-":
                learn = ""
            else:
                # Same rich-formatting pipeline as descriptions: bold, quotes
                # and premium emojis are preserved exactly as the admin sent them.
                learn, pairs = serialize_message_markdown(event.message)
                learn = learn.strip()
                await save_emoji_mappings(pairs)
            await set_product_learn_more(pid, learn)
            note = f"pid={pid} learn_more"
            done = (
                "✅ Learn More text updated. Buyers now see a Learn More "
                "button on the product page."
                if learn
                else "✅ Learn More text cleared."
            )
        elif step == "howto":
            if text.strip() == "-":
                howto = ""
            else:
                # Same rich-formatting pipeline as descriptions: bold, quotes
                # and premium emojis are preserved exactly as the admin sent them.
                howto, pairs = serialize_message_markdown(event.message)
                howto = howto.strip()
                await save_emoji_mappings(pairs)
            await set_product_howto(pid, howto)
            note = f"pid={pid} howto"
            done = (
                "✅ How-to-Use guide updated. Buyers now see a     button "
                "after purchase and in their order details."
                if howto
                else "✅ How-to-Use guide cleared."
            )
        else:  # step == "description"
            if text.strip() == "-":
                desc = ""
            else:
                desc, pairs = serialize_message_markdown(event.message)
                desc = desc.strip()
                await save_emoji_mappings(pairs)
            await set_product_meta(pid, p["name"], desc)
            note = f"pid={pid} desc"
            done = "✅ Description updated."
        clear_admin_state(user_id)
        await log_admin_action(user_id, None, "shop_editmeta", None, note)
        await event.reply(
            done,
            buttons=[
                [Button.inline("    Manage Product", f"adm_prod:{pid}".encode())],
                [Button.inline("🏠 Home to Shop", b"adm_shop")],
            ],
        )
        return

    # ── Add product: name → description → price_usd → price_inr ──
    # ── Add category: single name step ─   
    if action == "addcat":
        name, emoji_char, emoji_id, err = await parse_category_input(event, text)
        if err:
            await event.reply(err, buttons=admin_cancel_buttons())
            return
        cid = await create_category(name, emoji_id, emoji_char)
        register_category_emoji(emoji_char, emoji_id)
        clear_admin_state(user_id)
        await log_admin_action(
            user_id,
            None,
            "shop_addcat",
            None,
            f"cid={cid} name={name} emoji_id={emoji_id}",
        )
        await event.reply(
            f"✅ Category created: {emoji_char or '🗂'} {name}\n\n"
            "Now add products inside it.",
            buttons=[
                [Button.inline("➕ Add Product", f"adm_addprod:{cid}".encode())],
                [Button.inline("🗂 Categories", b"adm_products")],
                [Button.inline("     Home to Shop", b"adm_shop")],
            ],
        )
        return

    # ── Rename category ──
    if action == "renamecat":
        cid = data["cid"]
        cat = await get_category(cid)
        if not cat:
            clear_admin_state(user_id)
            await event.reply("That category no longer exists.", buttons=back)
            return
        name, emoji_char, emoji_id, err = await parse_category_input(event, text)
        if err:
            await event.reply(err, buttons=admin_cancel_buttons())
            return
        await rename_category(cid, name, emoji_id, emoji_char)
        register_category_emoji(emoji_char, emoji_id)
        if emoji_id is not None and cat.get("emoji_char") != emoji_char:
            await unregister_category_emoji(cat.get("emoji_char"))
        clear_admin_state(user_id)
        await log_admin_action(
            user_id,
            None,
            "shop_renamecat",
            None,
            f"cid={cid} name={name} emoji_id={emoji_id}",
        )
        shown_emoji = emoji_char or cat.get("emoji_char") or "🗂"
        await event.reply(
            f"✅ Category renamed to {shown_emoji} {name}.",
            buttons=[
                [Button.inline("🗂 Open Category", f"adm_cat:{cid}".encode())],
                [Button.inline("🏠 Home to Shop", b"adm_shop")],
            ],
        )
        return

    if action == "addprod":
        if step == "name":
            name, emoji_char, emoji_id, err = await parse_category_input(
                event, text, label="product", maxlen=120
            )
            if err:
                await event.reply(err, buttons=admin_cancel_buttons())
                return
            data["name"] = name
            data["emoji_id"] = emoji_id
            data["emoji_char"] = emoji_char
            state["step"] = "description"
            await event.reply(
                "Now send a short description (or send `-` to skip).\n\n"
                "Formatting is kept: bold, multi-line quotes (blank lines "
                "inside a quote too), expandable quotes and premium emojis "
                "show exactly how you send them.",
                buttons=admin_cancel_buttons(),
            )
            return
        if step == "description":
            if text.strip() == "-":
                data["description"] = ""
            else:
                desc_md, pairs = serialize_message_markdown(event.message)
                await save_emoji_mappings(pairs)
                data["description"] = desc_md.strip()
            state["step"] = "price_usd"
            await event.reply(
                "Send the USD price (e.g. 5). Send 0 if it can't be bought with $.",
                buttons=admin_cancel_buttons(),
            )
            return
        if step == "price_usd":
            try:
                pu = float(text.strip().lstrip("$").replace(",", ""))
            except ValueError:
                await event.reply(
                    "Send a number, e.g. 5 (or 0).", buttons=admin_cancel_buttons()
                )
                return
            if not math.isfinite(pu) or pu < 0 or pu > 10_000_000:
                await event.reply(
                    "Send a non-negative number.", buttons=admin_cancel_buttons()
                )
                return
            data["price_usd"] = pu
            state["step"] = "price_inr"
            await event.reply(
                "Send the ₹ price (e.g. 400). Send 0 if it can't be bought with ₹.",
                buttons=admin_cancel_buttons(),
            )
            return
        # step == "price_inr"
        try:
            pi = float(text.strip().lstrip("₹").replace(",", ""))
        except ValueError:
            await event.reply(
                "Send a number, e.g. 400 (or 0).", buttons=admin_cancel_buttons()
            )
            return
        if not math.isfinite(pi) or pi < 0 or pi > 10_000_000:
            await event.reply(
                "Send a non-negative number.", buttons=admin_cancel_buttons()
            )
            return
        if (data["price_usd"] <= 0) and (pi <= 0):
            await event.reply(
                "A product needs at least one non-zero price. Send the ₹ price "
                "(or /cancel and start over).",
                buttons=admin_cancel_buttons(),
            )
            return
        cat_id = data.get("category_id") or None  # 0 → NULL (uncategorized)
        pid = await create_product(
            data["name"],
            data["description"],
            data["price_usd"],
            pi,
            cat_id,
            emoji_id=data.get("emoji_id"),
            emoji_char=data.get("emoji_char"),
        )
        register_category_emoji(data.get("emoji_char"), data.get("emoji_id"))
        clear_admin_state(user_id)
        await log_admin_action(
            user_id,
            None,
            "shop_add",
            None,
            f"pid={pid} name={data['name']} cid={cat_id}",
        )
        await event.reply(
            f"✅ Product created: {data['name']}\n"
            f"Price: {shop_price_label({'price_usd': data['price_usd'], 'price_inr': pi})}\n\n"
            "It has no stock yet — add deliverables now,\n"
            "or switch it to 📧 manual (email invite) delivery.",
            buttons=[
                [Button.inline("➕ Add Stock", f"adm_stock:{pid}".encode())],
                [
                    Button.inline(
                        "📧 Switch to Manual (email)", f"adm_ftype:{pid}".encode()
                    )
                ],
                [Button.inline("📦 Products", b"adm_products")],
                [Button.inline("🏠 Home to Admin", b"admin")],
            ],
        )
        return

    # ── Add stock: each message = ONE deliverable (links may contain text,
    # so never split by lines). Admin sends items one message at a time. ──
    if action == "addstock":
        pid = data["pid"]
        p = await get_product(pid)
        if not p:
            clear_admin_state(user_id)
            await event.reply("That product no longer exists.", buttons=back)
            return
        item = text.strip()
        if not item:
            await event.reply(
                "Send the deliverable (link/code — text around it is fine).",
                buttons=admin_cancel_buttons(),
            )
            return
        added = await add_product_items(pid, [item])
        stock = await product_stock(pid)
        # Keep the state alive so the admin can keep sending more items,
        # one message each, until they tap Done.
        await log_admin_action(user_id, None, "shop_stock", added, f"pid={pid}")
        await event.reply(
            f"✅ Added 1 item to {p['name']}.\nStock now: {stock} available.\n\n"
            "Send the next item as a new message, or tap ✅ Done.",
            buttons=[
                [Button.inline("✅ Done", f"adm_prod:{pid}".encode())],
                [Button.inline("🏠 Home to Shop", b"adm_shop")],
            ],
        )
        return

    # ── Edit prices: price_usd → price_inr ──
    if action == "setprice":
        pid = data["pid"]
        p = await get_product(pid)
        if not p:
            clear_admin_state(user_id)
            await event.reply("That product no longer exists.", buttons=back)
            return
        if step == "price_usd":
            try:
                pu = float(text.strip().lstrip("$").replace(",", ""))
            except ValueError:
                await event.reply(
                    "Send a number, e.g. 5 (or 0).", buttons=admin_cancel_buttons()
                )
                return
            if not math.isfinite(pu) or pu < 0 or pu > 10_000_000:
                await event.reply(
                    "Send a non-negative number.", buttons=admin_cancel_buttons()
                )
                return
            data["price_usd"] = pu
            state["step"] = "price_inr"
            await event.reply(
                "Now send the ₹ price (send 0 if it can't be bought with ₹).",
                buttons=admin_cancel_buttons(),
            )
            return
        # step == "price_inr"
        try:
            pi = float(text.strip().lstrip("₹").replace(",", ""))
        except ValueError:
            await event.reply(
                "Send a number, e.g. 400 (or 0).", buttons=admin_cancel_buttons()
            )
            return
        if not math.isfinite(pi) or pi < 0 or pi > 10_000_000:
            await event.reply(
                "Send a non-negative number.", buttons=admin_cancel_buttons()
            )
            return
        if (data["price_usd"] <= 0) and (pi <= 0):
            await event.reply(
                "A product needs at least one non-zero price. Send the ₹ price.",
                buttons=admin_cancel_buttons(),
            )
            return
        await set_product_prices(pid, data["price_usd"], pi)
        clear_admin_state(user_id)
        await log_admin_action(user_id, None, "shop_setprice", None, f"pid={pid}")
        await event.reply(
            f"✅ Prices updated for {p['name']}:\n"
            f"{shop_price_label({'price_usd': data['price_usd'], 'price_inr': pi})}",
            buttons=[
                [Button.inline("📦 Manage Product", f"adm_prod:{pid}".encode())],
                [Button.inline("🏠 Home to Shop", b"adm_shop")],
            ],
        )
        return

    # ── Broadcast: capture the message, then show a preview + confirm step ──
    if action == "broadcast":
        if not (text.strip() or event.message.media):
            await event.reply(
                "Send some text or media to broadcast.",
                buttons=admin_cancel_buttons(),
            )
            return
        clear_admin_state(user_id)
        # Keep the admin's ORIGINAL message object so ALL rich content is
        # preserved exactly on re-send: premium (custom) emojis, blockquotes,
        # text links, bold/italic/spoilers, and attached media.
        PENDING_BROADCASTS[user_id] = event.message
        total = len(await get_all_user_ids())
        await event.reply(
            "👀  Broadcast Preview\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📝 {bc_preview_of(event.message)}\n\n"
            f"👥 Recipients: {total} users\n"
            "✨ Your message above will be re-sent EXACTLY as is.\n\n"
            "Ready to launch?",
            buttons=[
                [Button.inline("🚀 Confirm & Send", b"adm_bc_send")],
                [Button.inline("✖️ Discard", b"adm_bc_discard")],
            ],
        )
        return

    # Unknown / stale state — reset cleanly.
    clear_admin_state(user_id)
    await event.reply("Cancelled.", buttons=back)


# ─   Main message handler ───────────────────────────────────────────────────────


@client.on(events.NewMessage(incoming=True))
async def handler(event):
    if event.out:
        return

    text = event.raw_text or ""
    user_id = event.sender_id

    # Admin input flows take priority (before slash-command fall-through) so an
    # admin can type user IDs / amounts / addresses / messages into the panel.
    if user_id in ADMIN_IDS and user_id in ADMIN_STATE:
        stripped = text.strip()
        if stripped.lower() in ("/cancel", "cancel"):
            clear_admin_state(user_id)
            await event.reply("Cancelled.", buttons=admin_panel_buttons())
            return
        # Any other slash command aborts the flow and runs normally.
        if stripped.startswith("/"):
            clear_admin_state(user_id)
        else:
            await handle_admin_input(event, user_id, stripped)
            return

    # Let all slash commands fall through to their own handlers
    if re.match(
        r"^/(start|menu|balance|help|commands|admin|user|stats|broadcast|emojiid)",
        text.strip(),
    ):
        return

    # Banned users can't interact (admins are never banned).
    if user_id not in ADMIN_IDS and await is_banned(user_id):
        await event.reply(banned_message())
        return

    if await SUPPLIER_PANEL.customer_input(event, user_id, text.strip()):
        return

    # If we're waiting on this user to register their sending wallet, handle that.
    if user_id in AWAITING_WALLET:
        await handle_wallet_registration(event, user_id, text.strip())
        return

    # If we're waiting on this user to type a deposit amount, handle that.
    if user_id in AWAITING_AMOUNT:
        await handle_amount_entry(event, user_id, text.strip())
        return

    # If we're waiting on this user for a deposit tx hash, treat the message as one.
    if user_id in AWAITING_TX:
        await handle_deposit_submission(event, user_id, text.strip())
        return

    # UPI: waiting on the ₹ amount they want to add.
    if user_id in AWAITING_UPI_AMOUNT:
        await handle_upi_amount_entry(event, user_id, text.strip())
        return

    # UPI: waiting on the UTR / reference number after they paid.
    if user_id in AWAITING_UPI_UTR:
        await handle_upi_utr_entry(event, user_id, text.strip())
        return

    # Shop (manual product): waiting on the email for their invite.
    if user_id in AWAITING_EMAIL:
        await handle_email_entry(event, user_id, text.strip())
        return

    # Shop: waiting on a typed custom quantity.
    if user_id in AWAITING_QTY:
        ctx = AWAITING_QTY[user_id]
        pid, cur = ctx["pid"], ctx["cur"]
        raw = text.strip()
        p = await get_product(pid)
        if not p or not p["active"]:
            AWAITING_QTY.pop(user_id, None)
            await event.reply("That product is no longer available.")
            return
        stock = await product_stock(pid)
        max_qty = min(stock, 99)
        if not raw.isdigit() or not (1 <= int(raw) <= max_qty):
            await event.reply(
                f"Please send a number between 1 and {max_qty}.",
                buttons=[
                    [
                        Button.inline(
                            "Cancel",
                            f"shop_view:{pid}".encode(),
                            icon=PREMIUM_EMOJI_MAP.get("⬅️"),
                        )
                    ]
                ],
            )
            return
        qty = int(raw)
        AWAITING_QTY.pop(user_id, None)
        if cur == "crypto":
            # Crypto flow: show the coin picker for this quantity.
            unit_usd = p["price_usd"] or 0
            total_usd = round(unit_usd * qty, 2)
            await event.reply(
                "**💸  Pay with Crypto — Choose Coin**\n"
                "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
                f"**Quantity: {qty} × ${unit_usd:.2f} = ${total_usd:.2f}**\n"
                "**Choose the coin you'll use to pay for this product.**\n"
                "Delivery is automatic once your payment confirms.",
                buttons=[
                    [
                        Button.inline(
                            "USDT",
                            f"buypaynet:{pid}:{qty}".encode(),
                            style="primary",
                            icon=USDT_ICON_ID,
                        ),
                        Button.inline(
                            "GRAM",
                            f"buypay:{pid}:ton:{qty}".encode(),
                            style="primary",
                            icon=GRAM_ICON_ID,
                        ),
                    ],
                    [
                        Button.inline(
                            "LTC",
                            f"buypay:{pid}:ltc:{qty}".encode(),
                            style="primary",
                            icon=LTC_ICON_ID,
                        ),
                        Button.inline(
                            "BTC",
                            f"buypay:{pid}:btc:{qty}".encode(),
                            style="primary",
                            icon=BTC_ICON_ID,
                        ),
                    ],
                    back_home_row(f"shop_view:{pid}".encode()),
                ],
            )
            return
        price = p["price_usd"] if cur == "usd" else p["price_inr"]
        sym = "$" if cur == "usd" else "₹"
        total = round(price * qty, 6 if cur == "usd" else 2)
        try:
            confirm_data = await purchase_confirmation_data(user_id, p, cur, qty)
        except (ValueError, SupplierError) as exc:
            await event.reply(str(exc), parse_mode=None)
            return
        delivery_note = (
            f"This will be deducted from your {sym} balance and the "
            f"{'items' if qty > 1 else 'item'} will be delivered instantly."
        )
        if p.get("supplier_id"):
            delivery_note = (
                f"This will be deducted from your {sym} balance. "
                "Delivery or activation progress will appear in My Orders."
            )
        await event.reply(
            "[🧾](emoji/5444856076954520455) **Confirm Purchase** "
            "[✅](emoji/5206607081334906820)\n"
            "**━━━━━━━━━━━━━━━  ━━━━   ━━**\n\n"
            f"{product_emoji(p)}**Product: {p['name']}**\n"
            f"[➕](emoji/6025873488911274008) **Quantity: {qty} × {sym}{price:.2f}**\n\n"
            f"[💸](emoji/5350658940117986785) **Total Price: {sym}{total:.2f}**\n\n"
            f"[🖊](emoji/5985774024968379294) **Note: {delivery_note}**\n\n"
            "**Click Confirm to Continue** [👇](emoji/5228931982037116397)",
            buttons=[
                [
                    Button.inline(
                        f"✅ Confirm — pay {sym}{total:.2f}",
                        confirm_data,
                    )
                ],
                [
                    Button.inline(
                        "Cancel",
                        f"shop_view:{pid}".encode(),
                        icon=PREMIUM_EMOJI_MAP.get("⬅️"),
                    )
                ],
            ],
        )
        return

    # Any other message (random typed text, no active flow) → ignore it.
    # Sending the home screen here was confusing when a user typed something
    # by mistake, so we silently do nothing. They can always use /menu.
    return


# ══════════════════   ═════════════════════════════════   ═════════════════   ════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════  ══════════════════════

SUPPLIERS = SupplierService(DB_PATH, invalidate=invalidate_shop_cache)
SUPPLIER_PANEL = SupplierPanel(SUPPLIERS, sys.modules[__name__])


if __name__ == "__main__":
    print("Starting wallet checker bot (fast async + USD mode + credit system)...")

    async def _startup():
        # Initialize database
        await init_db()
        # Overlay any admin-changed settings (deposit addresses, min deposit) saved
        # in the DB on top of the hardcoded config constants.
        await apply_settings_overrides()
        print("Database initialized.")

        # Pre-fetch prices
        print("Pre-fetching prices...")
        async with aiohttp.ClientSession() as s:
            global PRICES, _prices_fetched_at
            PRICES = await fetch_prices(s)
            _prices_fetched_at = time.monotonic()

        p = PRICES
        print(
            "Prices: "
            + "  ".join(f"{k}=${v:,.4f}" for k, v in p.items() if k != "USDT" and v > 0)
        )

    async def _post_start():
        # Auto-set the bot username (used for deep links) from the connected account.
        global BOT_USERNAME
        try:
            me = await client.get_me()
            if me and me.username:
                BOT_USERNAME = me.username
        except Exception:
            pass
        # Load premium custom-emoji IDs from the configured pack, if any.
        await load_premium_emoji_pack()
        await load_category_emojis()
        # Auto-send the database file to admins every 5 days (disaster recovery).
        client.loop.create_task(auto_db_backup_loop())
        # Keep coin USD prices warm so deposit amount taps never block on
        # CoinGecko (which can take up to 10s per cold call).
        client.loop.create_task(coin_price_refresh_loop())
        client.loop.create_task(SUPPLIER_PANEL.worker())
        client.loop.create_task(product_stock_notification_loop())
        await load_maintenance_state()

    if not API_ID or not API_HASH or not BOT_TOKEN:
        raise SystemExit(
            "Set TELEGRAM_API_ID, TELEGRAM_API_HASH, and TELEGRAM_BOT_TOKEN "
            "as environment variables before starting the bot."
        )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_startup())
    client.start(bot_token=BOT_TOKEN)
    loop.run_until_complete(_post_start())
    loop.run_until_complete(start_health_server())
    print("Bot is running.")
    client.run_until_disconnected()
