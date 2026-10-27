"""Telegram admin and customer screens for the wholesale integration."""
from __future__ import annotations

import asyncio
import contextlib
import io
import ipaddress
import json
import time

from telethon import Button, errors

from suppliers import SupplierError, VENTE_URL, money


def button(label, data, *, icon=None):
    return Button.inline(label, data.encode(), icon=icon)


def plain(value, limit=160):
    # These labels also appear inside the legacy bot's markdown screens.
    return str(value or "").translate(str.maketrans({"`": "ˋ", "*": "∗", "_": "＿", "[": "［", "]": "］"}))[:limit]


class SupplierPanel:
    def __init__(self, service, host):
        self.s, self.h = service, host
        self.activation_inputs = {}

    async def show(self, event, text, rows, *, reply=False):
        if not reply:
            try:
                await event.edit(text, buttons=rows, parse_mode=None, link_preview=False)
                return
            except errors.MessageNotModifiedError:
                return
            except Exception:
                pass
        await event.respond(text, buttons=rows, parse_mode=None, link_preview=False)

    async def audit(self, uid, action, sid=None):
        await self.h.log_admin_action(uid, None, "supplier_" + action, None, f"supplier={sid}" if sid else "")

    async def home(self, page=0):
        suppliers = await self.s.rows("SELECT * FROM wholesale_suppliers ORDER BY id LIMIT 9 OFFSET ?", (page * 8,))
        rows = [[button(("🟢 " if s["enabled"] else "⏸ ") + s["name"], f"wa:view:{s['id']}")] for s in suppliers[:8]]
        nav = []
        if page:
            nav.append(button("Previous", f"wa:home:{page-1}"))
        if len(suppliers) > 8:
            nav.append(button("Next", f"wa:home:{page+1}"))
        if nav:
            rows.append(nav)
        rows += [[button("➕ Connect wholesaler", "wa:add")], [button("Admin panel", "admin")]]
        return ("🏭 Wholesale Suppliers\n\nConnect VenteBot-compatible supplier accounts, choose products, "
                "and sell them using your supplier wallet balance.\n\n"
                "Imports start hidden. Set your prices and publish when ready.", rows)

    async def supplier_view(self, sid):
        s = await self.s.supplier(sid)
        count = await self.s.one("SELECT COUNT(*) AS n FROM products WHERE supplier_id=?", (sid,))
        balance = "Not checked" if s["wallet_balance"] is None else f"${s['wallet_balance']:.2f}"
        text = (f"🏭 {s['name']}\n\nStatus: {'Selling enabled' if s['enabled'] else 'New sales paused'}\n"
                f"API: {s['base_url']}\nAccount: {s['account_id']}\nKey: {s['key_prefix']}…\n"
                f"Connected: {s['created_at'][:19]} UTC\nSupplier wallet: {balance}\n"
                f"Balance checked: {s['checked_at'] or 'Never'}\nCatalog synced: {s['synced_at'] or 'Never'}\n"
                f"Imported products: {count['n']}\nDefault markup: {s['markup_percent']:g}%\n"
                f"Optional INR import rate: {s['usd_inr_rate']:g} (0 = set INR prices manually)\n")
        if s["last_error"]:
            text += f"\nLast connection problem: {s['last_error']}\n"
        rows = [
            [button("Refresh balance", f"wa:balance:{sid}"), button("Sync catalog", f"wa:sync:{sid}")],
            [button("Browse catalog", f"wa:catalog:{sid}:0"), button("My imported products", f"wa:imports:{sid}:0")],
            [button("Fund supplier wallet", f"wa:fund:{sid}"), button("Deposits", f"wa:deposits:{sid}:0")],
            [button("Deposit networks and minimums", f"wa:methods:{sid}")],
            [button("Wallet history", f"wa:wallet:{sid}:0"), button("Orders", f"wa:orders:{sid}:0")],
            [button("Pricing defaults", f"wa:pricing:{sid}"), button("Replace API key", f"wa:key:{sid}")],
            [button("Rename", f"wa:rename:{sid}"), button("Security settings", f"wa:security:{sid}")],
            [button("Pause sales" if s["enabled"] else "Enable sales", f"wa:toggle:{sid}")],
            [Button.url("API documentation", s["base_url"] + "/api/swagger/")],
            [button("All suppliers", "wa:home:0")],
        ]
        return text, rows

    async def catalog(self, sid, page=0, imported=False):
        if imported:
            records = await self.s.rows("SELECT * FROM products WHERE supplier_id=? ORDER BY id LIMIT 9 OFFSET ?", (sid, page * 8))
            rows = [[button(("🟢 " if p["active"] else "◻ ") + p["name"][:55], f"wa:product:{p['id']}")] for p in records[:8]]
        else:
            records = await self.s.rows("SELECT * FROM wholesale_catalog WHERE supplier_id=? ORDER BY remote_id LIMIT 9 OFFSET ?", (sid, page * 8))
            rows = []
            for row in records[:8]:
                p = json.loads(row["data"])
                label = f"{p['name'][:45]} · ${p['price_usd']:.2f}"
                if not row["available"]:
                    label = "Unavailable/test · " + label
                rows.append([button(label, f"wa:remote:{sid}:{row['remote_id']}")])
        kind = "imports" if imported else "catalog"
        nav = []
        if page:
            nav.append(button("Previous", f"wa:{kind}:{sid}:{page-1}"))
        if len(records) > 8:
            nav.append(button("Next", f"wa:{kind}:{sid}:{page+1}"))
        if nav:
            rows.append(nav)
        rows.append([button("Supplier", f"wa:view:{sid}")])
        return ("Imported products" if imported else "Supplier catalog") + ("\n\nNo products yet. Sync the catalog first." if not records else "\n\nSelect a product to manage it."), rows

    async def remote_view(self, sid, rid):
        row = await self.s.one("SELECT * FROM wholesale_catalog WHERE supplier_id=? AND remote_id=?", (sid, rid))
        if not row:
            raise ValueError("Product not found. Sync the catalog first.")
        p = json.loads(row["data"])
        local = await self.s.one("SELECT id FROM products WHERE supplier_id=? AND supplier_product_id=?", (sid, rid))
        text = (f"{p['name']}\nSupplier product #{rid}\n\n{p.get('description', '')[:1700]}\n\n"
                f"Wholesale cost: ${p['price_usd']:.6f}\nPricing: {p.get('pricing_type', 'standard')}\n"
                f"Stock: {p.get('stock') if p.get('stock') is not None else 'On demand'}\n"
                f"Delivery: {p['delivery_type']}\nWarranty: {p.get('warranty_days', 0)} days\n")
        for tier in p.get("price_tiers", [])[:8]:
            text += f"\n{tier.get('min_qty')}–{tier.get('max_qty') or '∞'} units: ${tier.get('price_usd')} each"
        rows = []
        if local:
            rows.append([button("Manage imported product", f"wa:product:{local['id']}")])
        elif row["available"]:
            rows.append([button("Import as hidden product", f"wa:import:{sid}:{rid}")])
        else:
            text += "\nThis product is unavailable or reserved for API testing and cannot be published."
        rows.append([button("Catalog", f"wa:catalog:{sid}:0")])
        return text, rows

    async def product_admin(self, pid):
        p = await self.s.product(pid)
        if not p:
            raise ValueError("Product not found.")
        text = (f"📦 {plain(p['name'])}\n\nVisibility: {'Published' if p['active'] else 'Hidden'}\n"
                f"Supplier product: #{p['supplier_product_id']}\nWholesale cost: ${p['supplier_cost_usd']:.6f}\n"
                f"Your prices: ${p['price_usd']:.2f} / ₹{p['price_inr']:.2f}\n"
                f"Warranty: {p.get('warranty_days') or 0} days\n"
                f"Stock: {p['supplier_stock'] if p['supplier_stock'] is not None else 'On demand'}\n"
                f"Delivery: {p['supplier_delivery']}\nSupplier available: {'Yes' if p['supplier_available'] and p['supplier_enabled'] else 'No'}\n"
                f"Category: {p['category_id'] or 'Other'}\n\n"
                f"Share link: {self.h.product_deep_link(pid)}")
        rows = [
            [button("Hide product" if p["active"] else "Publish product", f"wa:publish:{pid}")],
            [button("Retail prices", f"adm_setprice:{pid}"), button("Category", f"wa:category:{pid}:0")],
            [button("Edit warranty", f"adm_warranty:{pid}")],
            [button("Name", f"adm_rename:{pid}"), button("Description", f"adm_redesc:{pid}")],
            [button("How to use", f"adm_rehowto:{pid}"), button("Learn more", f"adm_relearn:{pid}")],
            [button("Product image", f"adm_reimg:{pid}")],
            [button("Supplier catalog entry", f"wa:remote:{p['supplier_id']}:{p['supplier_product_id']}")],
            [button("Supplier", f"wa:view:{p['supplier_id']}")],
        ]
        return text, rows

    async def product_page(self, uid, p):
        return await self.h.build_product_page(uid, p)

    async def order_view(self, oid, uid, admin=False):
        o = await self.s.order(oid)
        if not o or (not admin and o["user_id"] != uid):
            raise ValueError("Order not found.")
        content = o["content"] or "Activation completed."
        if not admin and o["state"] in ("completed", "refunded") and len(content) <= 2000 and "`" not in content:
            return await self.h.build_user_order_view(oid, uid)
        labels = {"COMPLETED": "Delivered", "PAID_PENDING_DELIVERY": "Paid — awaiting delivery",
                  "AWAITING_ACTIVATION_INFO": "Waiting for your activation details",
                  "AWAITING_ACTIVATION": "Activation in progress", "CANCELLED": "Cancelled"}
        status = "Refunded to wallet" if o["state"] == "refunded" else labels.get(o["remote_status"], "Confirming your purchase")
        symbol = "$" if o["currency"] == "usd" else "₹"
        number = oid if admin else o["user_order_no"]
        text = (f"🧾 Order #{number}\n\n{plain(o['product_name'])}\nQuantity: {o['quantity']}\n"
                f"Paid: {symbol}{o['amount']:.2f}\nStatus: {status}\nDate: {o['created_at'][:19]} UTC\n")
        rows = []
        if o["state"] == "completed":
            if o["content"]:
                rows.append([button("View delivered items", f"{'wa' if admin else 'wc'}:delivery:{oid}")])
            else:
                text += "\nYour activation has been completed."
        elif o["state"] != "refunded":
            rows.append([button("Refresh status", f"{'wa' if admin else 'wc'}:refresh:{oid}")])
            if o["remote_status"] == "AWAITING_ACTIVATION_INFO" and not admin:
                rows.append([button("Send activation details", f"wc:activate:{oid}")])
            text += "\nThis purchase is saved. Refresh here or open My Orders; a new purchase is unnecessary."
        if admin:
            text += (f"\nBuyer: {o['user_id']}\nSupplier order: {o['remote_id'] or 'Awaiting response'}\n"
                     f"Wholesale total: ${o['cost_usd'] or 0:.6f}\nState: {o['state']}\n")
            if o["last_error"]:
                text += f"Last update: {plain(o['last_error'], 250)}\n"
            rows.append([button("Supplier orders", f"wa:orders:{o['supplier_id']}:0")])
        else:
            if await self.h.product_howto_by_id(o["product_id"]):
                rows.append([button("How to use", f"howto:{o['product_id']}")])
            rows.append([button("My Orders", "orders")])
        return text, rows

    async def deposit_view(self, did, refresh=False):
        row = await self.s.one("SELECT * FROM wholesale_deposits WHERE id=?", (did,))
        if not row:
            raise ValueError("Deposit not found.")
        issue = ""
        if refresh:
            try:
                await self.s.refresh_deposit(did)
            except SupplierError as exc:
                issue = str(exc)
            row = await self.s.one("SELECT * FROM wholesale_deposits WHERE id=?", (did,))
        d = json.loads(row["data"]) if row["data"] else {}
        text = (f"Supplier deposit #{did}\n\nStatus: {d.get('status', 'Not confirmed by supplier')}\n"
                f"Wallet credit: ${json.loads(row['request_json'])['amount_usd']:.2f}\n")
        if d.get("address"):
            text += (f"\nNetwork: {d.get('network', 'BEP20')}\nCurrency: {d.get('pay_currency', 'USDTBSC')}\n"
                     f"Send EXACTLY: {d.get('pay_amount')}\nAddress: {d['address']}\n"
                     f"Memo: {d.get('memo') or 'None'}\nExpires: {d.get('expires_at') or 'See supplier'}\n"
                     f"Actually paid: {d.get('actually_paid', 0)}\n")
            if d.get("fees"):
                text += "Fees: " + json.dumps(d["fees"])[:450] + "\n"
        if issue:
            text += f"\n{issue}\nRefresh this deposit to retry safely."
        text += "\nPay from your own wallet using the supplier's instructions. This funds the supplier account; it does not add customer balance in this bot."
        return text, [[button("Refresh deposit", f"wa:deprefresh:{did}")], [button("Supplier", f"wa:view:{row['supplier_id']}")]]

    def prompt(self, uid, kind, sid=0, **extra):
        self.h.clear_admin_state(uid)
        self.h.ADMIN_STATE[uid] = {"action": "supplier", "step": kind, "data": {"sid": sid, **extra}}

    async def callback(self, event, data):
        parts = data.split(":")
        admin = parts[0] == "wa"
        uid = event.sender_id
        if admin and uid not in self.h.ADMIN_IDS:
            await event.answer("Admin access required.", alert=True)
            return
        if not event.is_private:
            await event.answer("Open a private chat with the bot.", alert=True)
            return
        self.h.clear_deposit_state(uid)
        self.activation_inputs.pop(uid, None)
        if admin:
            self.h.clear_admin_state(uid)
        try:
            if admin:
                view = await self.admin_callback(uid, parts)
            else:
                view = await self.customer_callback(uid, parts)
            if parts[1] == "delivery":
                oid = int(parts[2])
                order = await self.s.order(oid)
                if not order or (not admin and order["user_id"] != uid) or order["state"] != "completed":
                    raise ValueError("Delivery not found.")
                content = order["content"]
                if len(content) <= 3500:
                    await event.respond(content or "Activation completed.", parse_mode=None)
                else:
                    file = io.BytesIO(content.encode())
                    file.name = f"order-{order['user_order_no']}-items.txt"
                    await self.h.client.send_file(event.chat_id, file, caption="Your delivered items")
                return
            if view:
                await self.show(event, *view)
        except (ValueError, SupplierError) as exc:
            rows = [[button("Suppliers", "wa:home:0")]] if admin else [[button("My Orders", "orders"), button("Shop", "shop")]]
            await self.show(event, str(exc), rows)
        except (KeyError, TypeError, IndexError):
            await self.show(event, "This request could not be read. Open the screen again. Existing orders are saved in My Orders.", [[button("Back", "wa:home:0" if admin else "orders")]])

    async def admin_callback(self, uid, parts):
        action = parts[1]
        sid = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        page = max(0, int(parts[3])) if len(parts) > 3 else 0
        if action == "home":
            return await self.home(sid)
        if action == "add":
            self.prompt(uid, "name")
            return "Connect wholesaler\n\nSend a name for this supplier, for example VenteBot.", [[button("Cancel", "wa:home:0")]]
        if action == "view":
            return await self.supplier_view(sid)
        if action in ("balance", "sync"):
            if action == "balance":
                await self.s.balance(sid)
            else:
                await self.s.sync(sid)
            return await self.supplier_view(sid)
        if action in ("catalog", "imports"):
            return await self.catalog(sid, page, action == "imports")
        if action == "remote":
            return await self.remote_view(sid, int(parts[3]))
        if action == "import":
            pid = await self.s.import_product(sid, int(parts[3]))
            await self.audit(uid, "import", sid)
            return await self.product_admin(pid)
        if action == "product":
            return await self.product_admin(sid)
        if action == "publish":
            p = await self.s.product(sid)
            if not p:
                raise ValueError("Product not found.")
            if not p["active"]:
                if not p["supplier_available"] or not p["supplier_enabled"]:
                    raise ValueError("Enable the supplier and sync an available commercial product first.")
                if money(p["price_usd"]) <= 0 and money(p["price_inr"]) <= 0:
                    raise ValueError("Set a retail price first.")
                if p["price_usd"] > 0 and money(p["price_usd"]) < money(p["supplier_cost_usd"]):
                    raise ValueError("USD retail price is below wholesale cost.")
            await self.s.write("UPDATE products SET active=? WHERE id=?", (0 if p["active"] else 1, sid))
            self.s.invalidate()
            await self.audit(uid, "visibility", p["supplier_id"])
            return await self.product_admin(sid)
        if action == "category":
            p = await self.s.product(sid)
            if not p:
                raise ValueError("Product not found.")
            cats = await self.s.rows("SELECT * FROM categories ORDER BY id LIMIT 9 OFFSET ?", (page * 8,))
            rows = [[button(c["name"], f"wa:setcat:{sid}:{c['id']}")] for c in cats[:8]]
            nav = []
            if page:
                nav.append(button("Previous", f"wa:category:{sid}:{page-1}"))
            if len(cats) > 8:
                nav.append(button("Next", f"wa:category:{sid}:{page+1}"))
            if nav:
                rows.append(nav)
            rows += [[button("Other", f"wa:setcat:{sid}:0")], [button("Product", f"wa:product:{sid}")]]
            return "Choose the shop category for this product.", rows
        if action == "setcat":
            if not await self.s.product(sid):
                raise ValueError("Product not found.")
            cid = int(parts[3])
            if cid and not await self.s.one("SELECT id FROM categories WHERE id=?", (cid,)):
                raise ValueError("Category not found.")
            await self.h.set_product_category(sid, cid or None)
            return await self.product_admin(sid)
        if action in ("key", "rename", "pricing", "fund", "allowlist"):
            supplier = await self.s.supplier(sid)
            self.prompt(uid, action, sid)
            messages = {
                "key": "Send the complete replacement API key from the same supplier account. The key message will be deleted where Telegram permits.",
                "rename": "Send the new supplier name (1–80 characters).",
                "pricing": "Send default markup percentage and optional INR import rate, separated by a space.\nExample: 20 90\nUse 20 0 to set INR prices manually.\n\nThese defaults only calculate initial prices for new imports. You can set each product's USD and INR selling prices yourself. No conversion rate is required for INR sales.",
                "fund": "Enter the USD amount to add to this supplier wallet. You'll review it before requesting BEP20 payment instructions. No money is sent automatically.",
                "allowlist": "Send up to 20 public IPs or CIDR ranges, separated by spaces, or 'clear'. Include the bot server's public outgoing IP to keep API access. You will review the list before saving.",
            }
            return f"{supplier['name']}\n\n" + messages[action], [[button("Cancel", f"wa:view:{sid}")]]
        if action == "toggle":
            s = await self.s.supplier(sid)
            await self.s.write("UPDATE wholesale_suppliers SET enabled=? WHERE id=?", (0 if s["enabled"] else 1, sid))
            self.s.invalidate()
            await self.audit(uid, "toggle", sid)
            return await self.supplier_view(sid)
        if action == "wallet":
            data = await self.s.call(sid, "GET", f"/wallet/transactions?limit=8&offset={page*8}")
            transactions = data.get("transactions", [])
            lines = ["Supplier wallet history", ""]
            for tx in transactions:
                lines.append(f"#{tx.get('id')} · {tx.get('type')} · ${tx.get('amount')}\n{str(tx.get('description', ''))[:150]}\nBalance: ${tx.get('balance_after')} · {tx.get('created_at')}\n")
            nav = []
            if page:
                nav.append(button("Previous", f"wa:wallet:{sid}:{page-1}"))
            if len(transactions) == 8:
                nav.append(button("Next", f"wa:wallet:{sid}:{page+1}"))
            return "\n".join(lines)[:3500], ([nav] if nav else []) + [[button("Supplier", f"wa:view:{sid}")]]
        if action == "methods":
            data = await self.s.call(sid, "GET", "/wallet/deposit-methods")
            # This endpoint intentionally has no response schema in OpenAPI 1.2.
            # Render its returned fields without assuming a minimum or network.
            text = "Supplier deposit networks and minimums\n\n" + json.dumps({k: v for k, v in data.items() if k != "success"}, indent=2, ensure_ascii=False)
            return text[:3500], [[button("Fund supplier wallet", f"wa:fund:{sid}")], [button("Supplier", f"wa:view:{sid}")]]
        if action in ("orders", "deposits"):
            if action == "orders":
                records = await self.s.rows("SELECT w.*,o.user_id FROM wholesale_orders w JOIN orders o ON o.id=w.order_id WHERE w.supplier_id=? ORDER BY w.order_id DESC LIMIT 9 OFFSET ?", (sid, page * 8))
                rows = [[button(f"#{r['order_id']} · {r['state']} · {r['product_name'][:32]}", f"wa:order:{r['order_id']}")] for r in records[:8]]
            else:
                records = await self.s.rows("SELECT * FROM wholesale_deposits WHERE supplier_id=? ORDER BY id DESC LIMIT 9 OFFSET ?", (sid, page * 8))
                rows = [[button(f"Deposit #{r['id']} · ${json.loads(r['request_json'])['amount_usd']:.2f}", f"wa:deposit:{r['id']}")] for r in records[:8]]
            nav = []
            if page:
                nav.append(button("Previous", f"wa:{action}:{sid}:{page-1}"))
            if len(records) > 8:
                nav.append(button("Next", f"wa:{action}:{sid}:{page+1}"))
            if nav:
                rows.append(nav)
            rows.append([button("Supplier", f"wa:view:{sid}")])
            return f"Supplier {action}\n\n" + ("No records yet." if not records else "Select a record."), rows
        if action in ("order", "refresh"):
            if action == "refresh":
                await self.s.process(sid)  # Respect retry deadlines and rate limits.
            return await self.order_view(sid, uid, admin=True)
        if action == "delivery":
            return None
        if action in ("deposit", "deprefresh"):
            return await self.deposit_view(sid, refresh=action == "deprefresh")
        if action == "security":
            data = await self.s.call(sid, "GET", "/security")
            sec = data.get("security", data)
            text = (f"Supplier API security\n\nAllowed IPs: {', '.join(sec.get('ip_allowlist', [])) or 'No restriction'}\n"
                    f"Supplier webhooks: {'Enabled' if sec.get('webhook_enabled') else 'Disabled'}\n\n"
                    "This bot checks order status automatically. Manage webhook endpoints in the supplier service if another application needs them.")
            return text, [[button("Edit IP allowlist", f"wa:allowlist:{sid}")], [button("Supplier", f"wa:view:{sid}")]]
        raise ValueError("Unknown supplier action.")

    async def customer_callback(self, uid, parts):
        action = parts[1]
        if action == "choose":
            pid, currency = int(parts[2]), parts[3]
            p = await self.s.product(pid)
            if not p or currency not in ("usd", "inr"):
                raise ValueError("Product not found.")
            rows = [[button(str(q), f"wc:quote:{pid}:{currency}:{q}") for q in (1, 5, 10)]]
            rows += [[button("Custom quantity", f"wc:quantity:{pid}:{currency}")], [button("Back", f"shop_view:{pid}")]]
            return "Choose quantity\n\n" + p["name"], rows
        if action == "quantity":
            pid, currency = int(parts[2]), parts[3]
            if currency not in ("usd", "inr") or not await self.s.product(pid):
                raise ValueError("Product not found.")
            self.activation_inputs[uid] = {"kind": "quantity", "pid": pid, "currency": currency}
            return "Send the quantity you want (1–99), or /cancel.", [[button("Cancel", f"shop_view:{pid}")]]
        if action == "quote":
            checkout = await self.s.checkout(uid, int(parts[2]), parts[3], int(parts[4]))
            return self.checkout_view(checkout)
        if action == "confirm":
            order = await self.s.confirm(uid, parts[2])
            if not order:
                raise ValueError("Order not found.")
            return await self.order_view(order["order_id"], uid)
        if action in ("order", "refresh", "activate", "delivery"):
            oid = int(parts[2])
            order = await self.s.order(oid)
            if not order or order["user_id"] != uid:
                raise ValueError("Order not found.")
            if action == "delivery":
                return None
            if action == "refresh":
                await self.s.process(oid)
            if action == "activate":
                if order["remote_status"] != "AWAITING_ACTIVATION_INFO" or order["state"] == "refunded":
                    raise ValueError("This order is not waiting for activation details.")
                self.activation_inputs[uid] = {"kind": "activation", "oid": oid}
                return "Send the email, Telegram ID, or service identifier required by the product (2–500 characters). Use /cancel to go back.", [[button("Cancel", f"wc:order:{oid}")]]
            return await self.order_view(oid, uid)
        raise ValueError("Unknown order action.")

    def checkout_view(self, c):
        sym = "$" if c["currency"] == "usd" else "₹"
        return (f"Confirm purchase\n\n{c['product_name']}\nQuantity: {c['quantity']}\n"
                f"Total: {sym}{c['amount']:.2f}\n\nThis amount will be deducted from your wallet. "
                "Delivery or activation progress will appear in My Orders. This checkout expires in 10 minutes.",
                [[button(f"Confirm · {sym}{c['amount']:.2f}", f"wc:confirm:{c['token']}")], [button("Cancel", f"shop_view:{c['product_id']}")]])

    async def admin_input(self, event, uid, text):
        if uid not in self.h.ADMIN_IDS or not event.is_private:
            return
        state = self.h.ADMIN_STATE.get(uid, {})
        if state.get("action") != "supplier":
            return
        step, data = state["step"], state["data"]
        sid = data["sid"]
        back = [[button("Cancel", f"wa:view:{sid}" if sid else "wa:home:0")]]
        try:
            if step in ("api_key", "key"):
                with contextlib.suppress(Exception):
                    await event.delete()
                if step == "key":
                    s = await self.s.supplier(sid)
                    await self.s.connect(s["name"], s["base_url"], text, sid)
                else:
                    sid = await self.s.connect(data["name"], data["url"], text)
                self.h.clear_admin_state(uid)
                await self.audit(uid, "connect" if step == "api_key" else "replace_key", sid)
                await self.show(event, *await self.supplier_view(sid), reply=True)
                return
            if step == "name":
                if not 1 <= len(text) <= 80:
                    raise ValueError("Use a name with 1–80 characters.")
                data["name"] = text
                state["step"] = "url"
                await self.show(event, f"Send the supplier's HTTPS website address, or type vente for:\n{VENTE_URL}\n\nOther suppliers must implement the same VenteBot Reseller API.", back, reply=True)
                return
            if step == "url":
                from suppliers import base_url
                data["url"] = base_url(VENTE_URL if text.lower() == "vente" else text)
                state["step"] = "api_key"
                await self.show(event, "Send the full reseller API key privately. A prefix such as 354a48bfe4… will not work. The message will be deleted where Telegram permits, and the saved key will be encrypted.", back, reply=True)
                return
            if step == "rename":
                if not 1 <= len(text) <= 80:
                    raise ValueError("Use a name with 1–80 characters.")
                await self.s.write("UPDATE wholesale_suppliers SET name=? WHERE id=?", (text, sid))
            elif step == "pricing":
                parts = text.split()
                if len(parts) != 2:
                    raise ValueError("Send two numbers, for example: 20 90")
                markup, rate = money(parts[0], 2), money(parts[1], 4)
                if markup > 10000 or rate > 100000:
                    raise ValueError("Markup or conversion rate is too large.")
                await self.s.write("UPDATE wholesale_suppliers SET markup_percent=?,usd_inr_rate=? WHERE id=?", (float(markup), float(rate), sid))
                self.s.invalidate()
            elif step == "fund":
                amount = money(text, 2)
                if not 0.01 <= amount <= 10000:
                    raise ValueError("Enter an amount between $0.01 and $10,000.")
                data["amount"] = float(amount)
                state["step"] = "fund_confirm"
                await self.show(event, f"Request payment instructions for ${amount:.2f}?\n\nSend YES to create a BEP20 deposit request. You will then send the exact amount from your own wallet.", back, reply=True)
                return
            elif step == "fund_confirm":
                if text.upper() != "YES":
                    raise ValueError("Send YES to request instructions, or cancel.")
                # Record the intent before calling the supplier, retaining it even
                # when a timeout means the deposit was created but not returned.
                did = await self.s.create_deposit(sid, data["amount"])
                self.h.clear_admin_state(uid)
                await self.audit(uid, "deposit_request", sid)
                await self.show(event, *await self.deposit_view(did, refresh=True), reply=True)
                return
            elif step == "allowlist":
                values = [] if text.lower() == "clear" else text.replace(",", " ").split()
                if len(values) > 20:
                    raise ValueError("Maximum 20 IP ranges.")
                allowlist = []
                for value in values:
                    try:
                        network = ipaddress.ip_network(value, strict=False)
                    except ValueError:
                        raise ValueError("Enter valid public IP addresses or CIDR ranges.") from None
                    if not network.is_global:
                        raise ValueError("Only public IP ranges are accepted.")
                    allowlist.append(str(network))
                data["allowlist"] = allowlist
                state["step"] = "allowlist_confirm"
                await self.show(event, "New IP allowlist:\n" + ("\n".join(allowlist) or "No IP restriction") + "\n\nSend YES to save. If your bot server's outgoing IP is absent, the supplier will reject this bot's API requests.", back, reply=True)
                return
            elif step == "allowlist_confirm":
                if text.upper() != "YES":
                    raise ValueError("Send YES to save the shown list, or cancel.")
                await self.s.call(sid, "PUT", "/security", {"ip_allowlist": data["allowlist"]})
            else:
                raise ValueError("Open the supplier screen again.")
            self.h.clear_admin_state(uid)
            await self.audit(uid, step, sid)
            await self.show(event, *await self.supplier_view(sid), reply=True)
        except (ValueError, SupplierError) as exc:
            await self.show(event, str(exc), back, reply=True)
        except (KeyError, TypeError):
            await self.show(event, "Supplier response could not be read. Try again from the supplier screen.", back, reply=True)

    async def customer_input(self, event, uid, text):
        state = self.activation_inputs.get(uid)
        if not state:
            return False
        if not event.is_private:
            return True
        if text.strip().lower() in ("cancel", "/cancel"):
            self.activation_inputs.pop(uid, None)
            await self.show(event, "Cancelled.", [[button("My Orders", "orders")]], reply=True)
            return True
        if text.startswith("/"):
            self.activation_inputs.pop(uid, None)
            return False
        try:
            if state["kind"] == "quantity":
                checkout = await self.s.checkout(uid, state["pid"], state["currency"], int(text))
                view = self.checkout_view(checkout)
            else:
                await self.s.activate(state["oid"], uid, text)
                view = await self.order_view(state["oid"], uid)
            self.activation_inputs.pop(uid, None)
            await self.show(event, *view, reply=True)
        except (ValueError, SupplierError) as exc:
            await self.show(event, str(exc), [[button("My Orders", "orders")]], reply=True)
        return True

    async def worker(self):
        last_sync = 0
        while True:
            try:
                # Finish existing purchases even if new sales have been paused.
                await self.s.reconcile()
                orders = await self.s.rows("SELECT w.order_id,w.state,o.user_id,w.state || ':' || COALESCE(w.remote_status,'') AS notification FROM wholesale_orders w JOIN orders o ON o.id=w.order_id WHERE w.notified_state IS NULL OR w.notified_state!=(w.state || ':' || COALESCE(w.remote_status,'')) LIMIT 20")
                for row in orders:
                    text, rows = await self.order_view(row["order_id"], row["user_id"])
                    try:
                        await self.h.client.send_message(row["user_id"], text, buttons=rows, parse_mode=None)
                    except (errors.UserIsBlockedError, errors.InputUserDeactivatedError):
                        pass  # Order remains available in My Orders.
                    except Exception:
                        continue
                    await self.s.write("UPDATE wholesale_orders SET notified_state=? WHERE order_id=?", (row["notification"], row["order_id"]))
                    await self.h.notify_admins(f"Supplier order #{row['order_id']}: {row['state']}", buttons=[[button("View order", f"wa:order:{row['order_id']}")]])
                if time.monotonic() - last_sync >= 300:
                    last_sync = time.monotonic()
                    for supplier in await self.s.rows("SELECT id FROM wholesale_suppliers WHERE enabled=1"):
                        try:
                            await self.s.sync(supplier["id"])
                        except (SupplierError, ValueError, KeyError, TypeError):
                            await self.s.write("UPDATE wholesale_suppliers SET last_error=? WHERE id=?", ("Catalog sync failed. Check the connection and API key.", supplier["id"]))
                await self.s.write("DELETE FROM wholesale_checkouts WHERE expires<? AND token NOT IN (SELECT checkout_token FROM wholesale_orders)", (time.time() - 86400,))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Do not print keys, supplier response bodies, or delivered items.
                print(f"Supplier background check failed ({type(exc).__name__}); retrying.")
            await asyncio.sleep(30)

