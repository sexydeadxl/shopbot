"""VenteBot-compatible wholesale suppliers and durable wallet purchases.

Wallet mutations use atomic D1 batches. Supplier HTTP calls run outside them.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urlsplit, quote

import aiohttp
import d1_database as database
from d1_database import statement as sql, retry_conflicts
from cryptography.fernet import Fernet, InvalidToken

VENTE_URL = "https://ventetelegrambotrailway-production.up.railway.app"
TERMINAL = {"completed", "refunded"}
REMOTE_STATES = {"COMPLETED", "PAID_PENDING_DELIVERY", "AWAITING_ACTIVATION_INFO",
                 "AWAITING_ACTIVATION", "CANCELLED"}


def now():
    return datetime.now(timezone.utc).isoformat()


def money(value, places=6):
    try:
        value = Decimal(str(value))
        if not value.is_finite() or value < 0 or value > Decimal("1000000000"):
            raise ValueError("Amount must be a finite, non-negative number.")
        return value.quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError):
        raise ValueError("Enter a valid amount.") from None


def base_url(value):
    value = value.strip().rstrip("/")
    for suffix in ("/api/swagger", "/api/reseller/openapi.json", "/api/reseller"):
        if value.endswith(suffix):
            value = value[:-len(suffix)]
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
            parsed.password or parsed.query or parsed.fragment or parsed.path or
            parsed.port not in (None, 443)):
        raise ValueError("Use the supplier's public HTTPS origin, for example https://example.com.")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith((".local", ".localhost", ".internal")):
        raise ValueError("A public supplier address is required.")
    try:
        if not ipaddress.ip_address(host).is_global:
            raise ValueError("A public supplier address is required.")
    except ValueError as exc:
        if str(exc) == "A public supplier address is required.":
            raise
    return value


class PublicResolver(aiohttp.resolver.DefaultResolver):
    async def resolve(self, host, port=0, family=0):
        entries = await super().resolve(host, port, family)
        if any(not ipaddress.ip_address(entry["host"]).is_global for entry in entries):
            raise OSError("Supplier hostname resolved to a non-public address.")
        return entries


class SupplierError(Exception):
    def __init__(self, message, *, status=0, code="", retry_after=0):
        super().__init__(message)
        self.status, self.code, self.retry_after = status, code, retry_after


class VenteAPI:
    """No implicit retries for writes; the service owns persisted idempotency keys."""
    def __init__(self):
        self._locks = {}
        self._next_request = {}

    async def request(self, supplier, key, method, path, payload=None):
        # Leave headroom below the documented 60 requests/minute per key.
        identity = (supplier["base_url"], supplier.get("account_id"), supplier["id"])
        lock = self._locks.setdefault(identity, asyncio.Lock())
        async with lock:
            wait = self._next_request.get(identity, 0) - time.monotonic()
            if wait > 60:
                raise SupplierError("Supplier rate limit; try again later.", status=429, retry_after=wait)
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_request[identity] = time.monotonic() + 1.15
            try:
                connector = aiohttp.TCPConnector(resolver=PublicResolver())
                async with aiohttp.ClientSession(connector=connector,
                        timeout=aiohttp.ClientTimeout(total=35, connect=10)) as session:
                    async with session.request(method, base_url(supplier["base_url"]) +
                            "/api/reseller" + path, headers={"X-Reseller-Key": key},
                            json=payload, allow_redirects=False) as response:
                        chunks, size = [], 0
                        async for chunk in response.content.iter_chunked(65536):
                            size += len(chunk)
                            if size > 4_000_000:
                                raise SupplierError("Supplier response is too large.")
                            chunks.append(chunk)
                        raw = b"".join(chunks)
                        try:
                            data = json.loads(raw)
                        except (ValueError, UnicodeError):
                            raise SupplierError("Supplier returned an unreadable response.") from None
                        if not isinstance(data, dict):
                            raise SupplierError("Supplier returned an invalid response.")
                        if response.status >= 300 or data.get("success") is not True:
                            try:
                                delay = max(2, min(float(response.headers.get("Retry-After", 60)), 3600))
                            except ValueError:
                                delay = 60
                            if response.status == 429:
                                self._next_request[identity] = time.monotonic() + delay
                            code = re.sub(r"[^A-Z0-9_]", "", str(data.get("code", "")))[:70]
                            # Never echo untrusted API messages that might include a key.
                            label = {401: "API key is invalid or revoked.", 403: "Supplier access denied; check its IP allowlist.",
                                     402: "Supplier wallet has insufficient balance.", 429: "Supplier rate limit; try again later."}.get(
                                         response.status, "Supplier could not complete this request.")
                            # Only the documented error shape can establish a
                            # definitive rejection; arbitrary proxy JSON cannot.
                            status = response.status if data.get("success") is False and code else 0
                            raise SupplierError(label, status=status, code=code, retry_after=delay)
                        return data
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                raise SupplierError("Supplier connection timed out or is unavailable.") from None


class SupplierService:
    def __init__(self, db_path, *, api=None, encryption_key=None, invalidate=None):
        self.db_path = str(db_path)
        self.api = api or VenteAPI()
        self._key = encryption_key
        self._fernet = None
        self.invalidate = invalidate or (lambda: None)
        self._order_locks = {}
        self._sync_locks = {}

    @contextlib.asynccontextmanager
    async def db(self):
        async with database.connect() as db:
            yield db

    async def rows(self, sql, params=()):
        async with self.db() as db:
            async with db.execute(sql, params) as cur:
                return [dict(row) for row in await cur.fetchall()]

    async def one(self, sql, params=()):
        rows = await self.rows(sql, params)
        return rows[0] if rows else None

    async def write(self, query, params=()):
        async with self.db() as db:
            cur = await db.execute(query, params)
            return cur.lastrowid

    async def init(self):
        async with self.db() as db:
            statements = [
                """CREATE TABLE IF NOT EXISTS wholesale_suppliers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    base_url TEXT NOT NULL, encrypted_key TEXT NOT NULL, key_prefix TEXT NOT NULL,
                    account_id INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    markup_percent REAL NOT NULL DEFAULT 20, usd_inr_rate REAL NOT NULL DEFAULT 0,
                    wallet_balance REAL, checked_at TEXT, synced_at TEXT, last_error TEXT,
                    created_at TEXT NOT NULL, UNIQUE(base_url, account_id))""",
                """CREATE TABLE IF NOT EXISTS wholesale_catalog (
                    supplier_id INTEGER NOT NULL, remote_id INTEGER NOT NULL,
                    data TEXT NOT NULL, available INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(supplier_id, remote_id))""",
                """CREATE TABLE IF NOT EXISTS wholesale_checkouts (
                    token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
                    supplier_id INTEGER NOT NULL, remote_product_id INTEGER NOT NULL,
                    currency TEXT NOT NULL, quantity INTEGER NOT NULL, amount REAL NOT NULL,
                    product_name TEXT NOT NULL, expires REAL NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS wholesale_orders (
                    order_id INTEGER PRIMARY KEY, checkout_token TEXT NOT NULL UNIQUE,
                    supplier_id INTEGER NOT NULL, request_json TEXT NOT NULL,
                    product_name TEXT NOT NULL, quantity INTEGER NOT NULL,
                    remote_id INTEGER, remote_status TEXT, cost_usd REAL,
                    state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                    content TEXT NOT NULL DEFAULT '', last_error TEXT, next_check REAL NOT NULL DEFAULT 0,
                    notified_state TEXT, updated_at TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS wholesale_deposits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, supplier_id INTEGER NOT NULL,
                    request_json TEXT NOT NULL, remote_id TEXT, data TEXT,
                    created_at TEXT NOT NULL)""",
                "CREATE INDEX IF NOT EXISTS idx_wholesale_pending ON wholesale_orders(state,next_check)",
            ]
            for sql in statements:
                await db.execute(sql)
            cols = {row[1] for row in await (await db.execute("PRAGMA table_info(products)")).fetchall()}
            for name, definition in {
                "supplier_id": "INTEGER", "supplier_product_id": "INTEGER",
                "supplier_cost_usd": "REAL", "supplier_stock": "INTEGER",
                "supplier_delivery": "TEXT", "supplier_available": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in cols:
                    await db.execute(f"ALTER TABLE products ADD COLUMN {name} {definition}")
            await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_supplier_product ON products(supplier_id,supplier_product_id)")

    def cipher(self):
        if self._fernet is None:
            value = self._key or os.environ.get("SUPPLIER_ENCRYPTION_KEY")
            if not value:
                path = Path(self.db_path).with_name(".supplier-key")
                try:
                    value = path.read_bytes().strip()
                except FileNotFoundError:
                    value = Fernet.generate_key()
                    try:
                        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    except FileExistsError:
                        value = path.read_bytes().strip()
                    else:
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(value)
            try:
                self._fernet = Fernet(value.encode() if isinstance(value, str) else value)
            except (ValueError, TypeError):
                raise SupplierError("SUPPLIER_ENCRYPTION_KEY must be a valid Fernet key.") from None
        return self._fernet

    def decrypt(self, supplier):
        try:
            return self.cipher().decrypt(supplier["encrypted_key"].encode()).decode()
        except InvalidToken:
            raise SupplierError("Supplier key cannot be decrypted. Restore .supplier-key or the encryption environment variable.") from None

    async def supplier(self, supplier_id):
        result = await self.one("SELECT * FROM wholesale_suppliers WHERE id=?", (supplier_id,))
        if not result:
            raise ValueError("Supplier not found.")
        return result

    async def call(self, supplier_id, method, path, payload=None):
        supplier = await self.supplier(supplier_id)
        try:
            async with asyncio.timeout(100):
                return await self.api.request(supplier, self.decrypt(supplier), method, path, payload)
        except asyncio.TimeoutError:
            raise SupplierError("Supplier request timed out while waiting for API capacity.") from None

    async def connect(self, name, url, key, supplier_id=None):
        if not 1 <= len(name.strip()) <= 80:
            raise ValueError("Supplier name must contain 1–80 characters.")
        if not 16 <= len(key) <= 512 or not key.isascii() or any(c.isspace() for c in key) or "..." in key:
            raise ValueError("Send the complete API key, not the displayed prefix.")
        url = base_url(url)
        data = await self.api.request({"id": supplier_id or 0, "base_url": url}, key, "GET", "/me")
        account = int(data["user_telegram_id"])
        balance = float(money(data["wallet_balance"]))
        encrypted = self.cipher().encrypt(key.encode()).decode()
        if supplier_id:
            old = await self.supplier(supplier_id)
            if old["account_id"] != account or old["base_url"] != url:
                raise ValueError("Replacement key must belong to the same supplier account. Add a separate supplier for another account.")
            await self.write("UPDATE wholesale_suppliers SET encrypted_key=?,key_prefix=?,wallet_balance=?,checked_at=?,last_error=NULL WHERE id=?",
                             (encrypted, key[:8], balance, now(), supplier_id))
            return supplier_id
        try:
            return await self.write("INSERT INTO wholesale_suppliers(name,base_url,encrypted_key,key_prefix,account_id,wallet_balance,checked_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                                    (name.strip(), url, encrypted, key[:8], account, balance, now(), now()))
        except database.IntegrityError:
            raise ValueError("This supplier account is already connected. Use Replace API key on its page.") from None

    async def balance(self, sid):
        data = await self.call(sid, "GET", "/me")
        supplier = await self.supplier(sid)
        if int(data["user_telegram_id"]) != supplier["account_id"]:
            raise SupplierError("Supplier account identity changed.")
        balance = float(money(data["wallet_balance"]))
        await self.write("UPDATE wholesale_suppliers SET wallet_balance=?,checked_at=?,last_error=NULL WHERE id=?", (balance, now(), sid))
        return balance

    async def sync(self, sid):
        async with self._sync_locks.setdefault(sid, asyncio.Lock()):
            data = await self.call(sid, "GET", "/products?lang=en")
            products = data.get("products")
            if not isinstance(products, list):
                raise SupplierError("Supplier returned an invalid catalog.")
            clean, seen = [], set()
            for p in products:
                if not isinstance(p, dict) or type(p.get("id")) is not int or p["id"] < 1 or p["id"] in seen:
                    raise SupplierError("Supplier returned an invalid product ID.")
                seen.add(p["id"])
                if not isinstance(p.get("name"), str) or not p["name"].strip():
                    raise SupplierError("Supplier returned an invalid product name.")
                cost = float(money(p["price_usd"]))
                stock = p.get("stock")
                if stock is not None and (type(stock) is not int or stock < 0):
                    raise SupplierError("Supplier returned an invalid stock count.")
                if p.get("delivery_type") not in ("stock", "activation", "supplier_api", "api_test"):
                    raise SupplierError("Supplier returned an unsupported delivery type.")
                # Unknown/test products stay available for admin inspection only.
                allowed = p.get("api_test") is False and p["delivery_type"] != "api_test"
                clean.append((p, cost, stock, int(allowed)))
            catalog = json.dumps([dict(data=p, cost=cost, stock=stock, available=allowed)
                                  for p, cost, stock, allowed in clean])
            async with self.db() as db:
                await db.batch([
                    sql("UPDATE wholesale_catalog SET available=0 WHERE supplier_id=?", (sid,)),
                    sql("UPDATE products SET supplier_available=0 WHERE supplier_id=?", (sid,)),
                    sql("INSERT INTO wholesale_catalog(supplier_id,remote_id,data,available) "
                        "SELECT ?,json_extract(value,'$.data.id'),json_extract(value,'$.data'),json_extract(value,'$.available') "
                        "FROM json_each(?) WHERE true ON CONFLICT(supplier_id,remote_id) "
                        "DO UPDATE SET data=excluded.data,available=excluded.available", (sid, catalog)),
                    sql("UPDATE products SET supplier_cost_usd=json_extract(j.value,'$.cost'), "
                        "supplier_stock=json_extract(j.value,'$.stock'), "
                        "supplier_delivery=json_extract(j.value,'$.data.delivery_type'), "
                        "supplier_available=json_extract(j.value,'$.available') "
                        "FROM json_each(?) AS j WHERE supplier_id=? AND supplier_product_id=json_extract(j.value,'$.data.id')", (catalog, sid)),
                    sql("UPDATE wholesale_suppliers SET synced_at=?,last_error=NULL WHERE id=?", (now(), sid)),
                ])
            self.invalidate()
            return len(clean)

    async def import_product(self, sid, remote_id):
        supplier = await self.supplier(sid)
        row = await self.one("SELECT * FROM wholesale_catalog WHERE supplier_id=? AND remote_id=? AND available=1", (sid, remote_id))
        if not row:
            raise ValueError("This product is unavailable or is an API test product.")
        p = json.loads(row["data"])
        price = money(money(p["price_usd"]) * (1 + money(supplier["markup_percent"]) / 100), 2)
        inr = money(price * money(supplier["usd_inr_rate"]), 2)
        async with self.db() as db:
            results = await db.batch([
                sql("INSERT OR IGNORE INTO products(name,description,price_usd,price_inr,active,created_at,fulfillment,supplier_id,supplier_product_id,supplier_cost_usd,supplier_stock,supplier_delivery,supplier_available) VALUES(?,?,?,?,0,?,'supplier',?,?,?,?,?,1)",
                    (p["name"], p.get("description", ""), float(price), float(inr), now(), sid, remote_id, float(money(p["price_usd"])), p.get("stock"), p["delivery_type"])),
                sql("SELECT id FROM products WHERE supplier_id=? AND supplier_product_id=?", (sid, remote_id)),
            ])
            row = await results[-1].fetchone()
        self.invalidate()
        return row[0]

    async def product(self, pid):
        return await self.one("SELECT p.*,s.enabled AS supplier_enabled,s.usd_inr_rate FROM products p JOIN wholesale_suppliers s ON s.id=p.supplier_id WHERE p.id=?", (pid,))

    async def checkout(self, user_id, pid, currency, quantity):
        if currency not in ("usd", "inr") or type(quantity) is not int or not 1 <= quantity <= 99:
            raise ValueError("Choose a quantity between 1 and 99 and a supported currency.")
        p = await self.product(pid)
        if not p or not p["active"] or not p["supplier_enabled"] or not p["supplier_available"]:
            raise ValueError("This product is currently unavailable.")
        price = money(p["price_" + currency], 6 if currency == "usd" else 2)
        if price <= 0:
            raise ValueError("This product is not sold in that currency.")
        amount = money(price * quantity, 6 if currency == "usd" else 2)
        await self.validate_quote(p, currency, quantity, amount)
        token = uuid.uuid4().hex
        await self.write("INSERT INTO wholesale_checkouts VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (token, user_id, pid, p["supplier_id"], p["supplier_product_id"], currency, quantity, float(amount), p["name"], time.time() + 600))
        return await self.one("SELECT * FROM wholesale_checkouts WHERE token=?", (token,))

    async def validate_quote(self, p, currency, quantity, amount):
        data = await self.call(p["supplier_id"], "POST", "/quote", {"product_id": p["supplier_product_id"], "quantity": quantity})
        q = data.get("quote", {})
        if q.get("product_id") != p["supplier_product_id"] or q.get("quantity") != quantity:
            raise SupplierError("Supplier returned a mismatched quote.")
        cost = money(q["total"])
        stock = q.get("stock")
        if stock is not None and (type(stock) is not int or stock < quantity):
            raise ValueError("The requested quantity is no longer in stock.")
        if q.get("delivery_type") == "api_test":
            raise ValueError("API test products cannot be sold.")
        if money(data["wallet_balance"]) < cost:
            raise ValueError("This product is temporarily unavailable. Please contact support.")
        if currency == "usd" and money(amount) < cost:
            raise ValueError("This product's price needs updating. Please contact support.")
        return cost

    async def confirm(self, user_id, token):
        existing = await self.one("SELECT w.order_id FROM wholesale_orders w JOIN orders o ON o.id=w.order_id WHERE w.checkout_token=? AND o.user_id=?", (token, user_id))
        if existing:
            return await self.process(existing["order_id"])
        checkout = await self.one("SELECT * FROM wholesale_checkouts WHERE token=? AND user_id=?", (token, user_id))
        if not checkout or checkout["expires"] < time.time():
            raise ValueError("This checkout expired. Open the product and try again.")
        p = await self.product(checkout["product_id"])
        if not p or not p["active"] or not p["supplier_enabled"] or not p["supplier_available"]:
            raise ValueError("This product is currently unavailable.")
        currency, qty = checkout["currency"], checkout["quantity"]
        cost = await self.validate_quote(p, currency, qty, checkout["amount"])
        payload = {"product_id": checkout["remote_product_id"], "quantity": qty,
                   "customer_reference": f"telegram_user_{user_id}", "idempotency_key": "shop-" + token}
        oid = await self._reserve_order(user_id, token, checkout, p, currency, qty, cost, payload)
        return await self.process(oid)

    async def order(self, oid):
        return await self.one("SELECT o.*,w.*, (SELECT COUNT(*) FROM orders n WHERE n.user_id=o.user_id AND n.id<=o.id) AS user_order_no FROM orders o JOIN wholesale_orders w ON w.order_id=o.id WHERE o.id=?", (oid,))

    async def process(self, oid):
        async with self._order_locks.setdefault(oid, asyncio.Lock()):
            # A DB lease also protects against another worker/process. Its length
            # covers the maximum rate-limit wait plus the HTTP timeout.
            order, acquired = await self._lease_order(oid)
            if not acquired:
                return order
            first_attempt = order["attempts"] == 0
            # Persist before sending. A crash after this point is an uncertain
            # purchase; future failures cannot prove that the wallet wasn't debited.
            try:
                if order["remote_id"] is not None:
                    data = await self.call(order["supplier_id"], "GET", f"/orders/{order['remote_id']}")
                else:
                    data = await self.call(order["supplier_id"], "POST", "/orders", json.loads(order["request_json"]))
                remote = data.get("order")
                if not isinstance(remote, dict):
                    raise SupplierError("Supplier order response is incomplete; reconciliation will retry.")
                await self.apply_order(order, remote)
            except SupplierError as exc:
                # Only a structured rejection of the very first request is a
                # definitive no-purchase outcome. All timeouts/5xx/409 stay reserved.
                if first_attempt and not order["remote_id"] and exc.status in (400, 401, 402, 403, 422, 429):
                    await self.refund(oid, "Supplier rejected the purchase.")
                else:
                    await self.write("UPDATE wholesale_orders SET last_error=?,next_check=? WHERE order_id=?",
                                     (str(exc), time.time() + max(65, exc.retry_after), oid))
            except (KeyError, TypeError, ValueError):
                await self.write("UPDATE wholesale_orders SET last_error=? WHERE order_id=?",
                                 ("Supplier order response could not be validated; funds remain reserved.", oid))
            return await self.order(oid)

    @retry_conflicts
    async def apply_order(self, order, remote):
        payload = json.loads(order["request_json"])
        rid, state = remote.get("id"), remote.get("status")
        if (type(rid) is not int or rid <= 0 or state not in REMOTE_STATES or
                remote.get("product_id") != payload["product_id"] or remote.get("quantity") != payload["quantity"] or
                remote.get("idempotency_key") != payload["idempotency_key"] or
                remote.get("customer_reference") != payload["customer_reference"] or
                (order["remote_id"] is not None and order["remote_id"] != rid)):
            raise SupplierError("Supplier returned a mismatched order; funds remain reserved for review.")
        cost = float(money(remote["amount_usd"]))
        items = remote.get("items", [])
        if not isinstance(items, list) or any(not isinstance(item, dict) or not isinstance(item.get("account_data"), str) for item in items):
            raise SupplierError("Supplier returned invalid delivery data.")
        content = "\n\n".join(item["account_data"] for item in items)
        # A completed stock order without its promised items needs reconciliation.
        if state == "COMPLETED" and remote.get("delivery_type") == "stock" and (len(items) != order["quantity"] or any(not i["account_data"] for i in items)):
            raise SupplierError("Supplier delivery is incomplete; reconciliation will retry.")
        async with self.db() as db:
            current = await (await db.execute("SELECT state FROM wholesale_orders WHERE order_id=?", (order["order_id"],))).fetchone()
            if not current or current[0] in TERMINAL:
                return
            local_state = "completed" if state == "COMPLETED" else "pending"
            local_status = "completed" if state == "COMPLETED" else "supplier_activation" if state == "AWAITING_ACTIVATION_INFO" else "supplier_pending"
            await db.batch([
                sql("UPDATE wholesale_orders SET remote_id=?,remote_status=?,cost_usd=?,content=?,state=?,last_error=NULL,updated_at=?,next_check=? WHERE order_id=? AND state NOT IN ('completed','refunded')",
                    (rid, state, cost, content, local_state, now(), time.time() + 65, order["order_id"]), 1),
                sql("UPDATE orders SET status=? WHERE id=? AND status!='refunded'", (local_status, order["order_id"]), 1),
            ], guarded=True)
        if state == "CANCELLED":
            await self.refund(order["order_id"], "Supplier cancelled the order.")
        self.invalidate()

    @retry_conflicts
    async def refund(self, oid, reason):
        async with self.db() as db:
            row = await (await db.execute("SELECT o.*,w.state FROM orders o JOIN wholesale_orders w ON w.order_id=o.id WHERE o.id=?", (oid,))).fetchone()
            if not row or row["status"] == "refunded" or row["state"] == "completed":
                return
            col, places = ("balance_usd", 6) if row["currency"] == "usd" else ("balance_inr", 2)
            if not await (await db.execute('SELECT 1 FROM users WHERE user_id=?', (row['user_id'],))).fetchone():
                raise ValueError('Buyer missing; refund requires review.')
            await db.batch([
                sql(f"UPDATE users SET {col}=ROUND(COALESCE({col},0)+?,{places}) WHERE user_id=?", (row["amount"], row["user_id"]), 1),
                sql("UPDATE orders SET status='refunded',refunded_at=? WHERE id=? AND status!='refunded'", (now(), oid), 1),
                sql("UPDATE wholesale_orders SET state='refunded',last_error=?,updated_at=? WHERE order_id=? AND state NOT IN ('completed','refunded')", (reason, now(), oid), 1),
            ], guarded=True)

    async def activate(self, oid, user_id, identifier):
        identifier = identifier.strip()
        if not 2 <= len(identifier) <= 500:
            raise ValueError("Activation details must contain 2–500 characters.")
        async with self._order_locks.setdefault(oid, asyncio.Lock()):
            order = await self.order(oid)
            if not order or order["user_id"] != user_id or order["remote_status"] != "AWAITING_ACTIVATION_INFO" or order["state"] in TERMINAL:
                raise ValueError("This order is not waiting for your activation details.")
            data = await self.call(order["supplier_id"], "POST", f"/orders/{order['remote_id']}/activation-identifier", {"activation_identifier": identifier})
            await self.apply_order(order, data["order"])
        return await self.order(oid)

    async def create_deposit(self, sid, amount):
        amount = money(amount, 2)
        if not Decimal("0.01") <= amount <= 10000:
            raise ValueError("Deposit amount must be between $0.01 and $10,000.")
        await self.supplier(sid)
        body = {"amount_usd": float(amount), "network": "BEP20", "idempotency_key": "fund-" + uuid.uuid4().hex,
                "reference": f"shop_supplier_{sid}"}
        did = await self.write("INSERT INTO wholesale_deposits(supplier_id,request_json,created_at) VALUES(?,?,?)", (sid, json.dumps(body), now()))
        return did

    async def refresh_deposit(self, did):
        row = await self.one("SELECT * FROM wholesale_deposits WHERE id=?", (did,))
        if not row:
            raise ValueError("Deposit not found.")
        if row["remote_id"]:
            data = await self.call(row["supplier_id"], "GET", "/wallet/deposits/" + quote(row["remote_id"], safe="") + "?refresh=true")
        else:
            data = await self.call(row["supplier_id"], "POST", "/wallet/deposits", json.loads(row["request_json"]))
        dep = data.get("deposit")
        if not isinstance(dep, dict) or not isinstance(dep.get("deposit_id"), str):
            raise SupplierError("Supplier deposit response is incomplete. Refresh this same deposit to retry.")
        if dep.get("idempotency_key") != json.loads(row["request_json"])["idempotency_key"]:
            raise SupplierError("Supplier returned a mismatched deposit.")
        await self.write("UPDATE wholesale_deposits SET remote_id=?,data=? WHERE id=?", (dep["deposit_id"], json.dumps(dep), did))
        return dep

    async def reconcile(self):
        due = await self.rows("SELECT order_id FROM wholesale_orders WHERE state NOT IN ('completed','refunded') AND next_check<=? ORDER BY next_check LIMIT 20", (time.time(),))
        for row in due:
            try:
                await self.process(row["order_id"])
            except Exception:
                # Keep the durable order reserved and revisit after restart/error.
                await self.write("UPDATE wholesale_orders SET next_check=?,last_error=? WHERE order_id=?", (time.time() + 120, "Reconciliation interrupted; will retry.", row["order_id"]))



    @retry_conflicts
    async def _reserve_order(self, user_id, token, checkout, p, currency, qty, cost, payload):
        async with self.db() as db:
            existing = await (await db.execute("SELECT order_id FROM wholesale_orders WHERE checkout_token=?", (token,))).fetchone()
            if existing:
                return existing[0]
            live = await (await db.execute("SELECT p.*,s.enabled FROM products p JOIN wholesale_suppliers s ON s.id=p.supplier_id WHERE p.id=?", (p["id"],))).fetchone()
            if not live or not live["active"] or not live["enabled"] or not live["supplier_available"]:
                raise ValueError("This product is currently unavailable.")
            if (live["supplier_id"] != checkout["supplier_id"] or live["supplier_product_id"] != checkout["remote_product_id"] or
                    money(money(live["price_" + currency]) * qty, 6 if currency == "usd" else 2) != money(checkout["amount"])):
                raise ValueError("The price changed. Open the product for a new checkout.")
            col, places = ("balance_usd", 6) if currency == "usd" else ("balance_inr", 2)
            buyer = await (await db.execute(f'SELECT {col},banned FROM users WHERE user_id=?', (user_id,))).fetchone()
            if not buyer or buyer[1] or (buyer[0] or 0) < checkout['amount']:
                raise ValueError('Not enough wallet balance, or your account is unavailable.')
            # last_insert_rowid() is evaluated inside this one D1 batch, before
            # any other INSERT can change it. No IDs cross HTTP round trips.
            results = await db.batch([
                sql(f"UPDATE users SET {col}=ROUND({col}-?,{places}) WHERE user_id=? AND COALESCE(banned,0)=0 AND {col}>=?", (checkout["amount"], user_id, checkout["amount"]), 1),
                sql("INSERT INTO orders(user_id,product_id,item_id,currency,amount,created_at,status) VALUES(?,?,0,?,?,?,'supplier_pending')", (user_id, p["id"], currency, checkout["amount"], now())),
                sql("INSERT INTO wholesale_orders(order_id,checkout_token,supplier_id,request_json,product_name,quantity,cost_usd,updated_at) VALUES(last_insert_rowid(),?,?,?,?,?,?,?)", (token, p["supplier_id"], json.dumps(payload, sort_keys=True), checkout["product_name"], qty, float(cost), now())),
            ], guarded=True)
            return results[1].lastrowid

    @retry_conflicts
    async def _lease_order(self, oid):
        async with self.db() as db:
            row = await (await db.execute("SELECT o.*,w.* FROM orders o JOIN wholesale_orders w ON w.order_id=o.id WHERE o.id=?", (oid,))).fetchone()
            order = dict(row) if row else None
            if not order or order["state"] in TERMINAL or order["next_check"] > time.time():
                return order, False
            await db.batch([sql("UPDATE wholesale_orders SET attempts=attempts+1,next_check=?,updated_at=? WHERE order_id=?", (time.time() + 120, now(), oid), 1)], guarded=True)
            return order, True
