"""Exercise actual shop functions without starting Telegram or using a database."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from telethon.extensions import markdown
from telethon.tl import types

from suppliers import SupplierError


class PurchaseUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8"))
        names = {"buy_product", "purchase_confirmation_data", "build_product_page", "on_callback",
                 "product_emoji", "product_footer", "shop_price_label", "product_is_manual",
                 "handle_admin_input", "clear_admin_state"}
        functions = [n for n in tree.body if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name in names]
        for fn in functions:
            fn.decorator_list = []
        self.p = dict(id=7, name="Example", description="Description", active=1,
                      price_usd=8, price_inr=800, supplier_id=1, supplier_product_id=12,
                      supplier_stock=10)
        self.checkout = dict(token="a" * 32, user_id=123, product_id=7,
                             currency="usd", quantity=1, amount=8)
        self.order = dict(order_id=9, state="completed", product_name="Example",
                          quantity=1, amount=8, content="account:password")
        self.service = SimpleNamespace(
            checkout=AsyncMock(return_value=self.checkout), one=AsyncMock(return_value=self.checkout),
            confirm=AsyncMock(return_value=self.order), validate_quote=AsyncMock(return_value=5))
        self.panel = SimpleNamespace(activation_inputs={}, callback=AsyncMock(),
                                     order_view=AsyncMock(return_value=("Activation in progress", [])))
        self.ns = dict(
            asyncio=asyncio, SupplierError=SupplierError, SUPPLIERS=self.service,
            SUPPLIER_PANEL=self.panel, _D1_WALLET=SimpleNamespace(buy_product=AsyncMock(return_value=("ok", {}))),
            get_product=AsyncMock(side_effect=lambda pid: self.p),
            get_balances=AsyncMock(return_value=(20, 2000)), product_stock=AsyncMock(return_value=10),
            product_is_manual=lambda p: False, product_learn_more=lambda p: False,
            Button=SimpleNamespace(inline=lambda text, data, **kw: SimpleNamespace(text=text, data=data, **kw)),
            PREMIUM_EMOJI_MAP={}, ADMIN_IDS=[], ensure_user_not_banned=AsyncMock(return_value=False),
            PRODUCT_IMAGE_MSGS=set(), SCREEN_BANNER_MSGS={}, AWAITING_QTY={},
            clear_deposit_state=Mock(), back_home_row=lambda data: [],
            btn_orders=lambda: None, btn_back=lambda: None, btn_deposit=lambda: None,
            product_howto_by_id=AsyncMock(return_value=""), maybe_alert_low_stock=AsyncMock(),
            notify_admins=AsyncMock(), log_to_group=AsyncMock(), ist_now_str=lambda: "test",
            send_product_page=AsyncMock(), USDT_ICON_ID=1, GRAM_ICON_ID=2, LTC_ICON_ID=3, BTC_ICON_ID=4,
            subscribe_product_stock=AsyncMock(), ADMIN_STATE={},
            admin_cancel_buttons=lambda: [], admin_panel_buttons=lambda: [],
            set_product_warranty=AsyncMock(), log_admin_action=AsyncMock(),
        )
        exec(compile(ast.Module(body=functions, type_ignores=[]), "shop-functions", "exec"), self.ns)

    async def callback(self, data):
        event = SimpleNamespace(
            sender_id=123, chat_id=123, message_id=1, is_private=True, data=data.encode(),
            edit=AsyncMock(), respond=AsyncMock(return_value=SimpleNamespace(id=2)),
            answer=AsyncMock(), delete=AsyncMock())
        try:
            await self.ns["on_callback"](event)
        finally:
            if hasattr(event, "_spinner_task"):
                event._spinner_task.cancel()
        return event

    def buttons(self, event):
        call = event.edit.call_args or event.respond.call_args
        return [b for row in call.kwargs["buttons"] for b in row if b]

    async def test_product_page_is_identical_for_normal_and_supplier(self):
        supplier = await self.ns["build_product_page"](123, self.p)
        normal = await self.ns["build_product_page"](123, {**self.p, "supplier_id": None})
        self.assertEqual(supplier, normal)
        self.assertEqual([b.data for row in supplier[1] for b in row][:2],
                         [b"paycrypto:7", b"paywallet:7"])

    async def test_wallet_and_crypto_routes_use_normal_controls(self):
        event = await self.callback("paywallet:7")
        self.assertIn(b"shop_buy:7:usd", [b.data for b in self.buttons(event)])
        self.assertIn(b"shop_buy:7:inr", [b.data for b in self.buttons(event)])
        event = await self.callback("paycrypto:7:1")
        self.assertIn(b"buypaynet:7:1", [b.data for b in self.buttons(event)])
        self.panel.callback.assert_not_awaited()

    async def test_quantity_then_confirmation_uses_saved_token(self):
        event = await self.callback("shop_buy:7:usd")
        self.assertIn(b"shop_qty:7:usd:5", [b.data for b in self.buttons(event)])
        event = await self.callback("shop_qty:7:usd:1")
        self.assertIn("Confirm Purchase", event.edit.call_args.args[0])
        self.assertIn(b"wc:confirm:" + b"a" * 32, [b.data for b in self.buttons(event)])
        self.service.checkout.assert_awaited_once_with(123, 7, "usd", 1)

    async def test_confirm_and_repeat_use_same_token_and_normal_receipt(self):
        for _ in range(2):
            event = await self.callback("wc:confirm:" + "a" * 32)
            self.assertIn("PURCHASE COMPLETE", event.edit.call_args.args[0])
            self.assertIn("account:password", event.edit.call_args.args[0])
        self.assertEqual([call.args for call in self.service.confirm.await_args_list],
                         [(123, "a" * 32), (123, "a" * 32)])
        self.service.checkout.assert_not_awaited()
        self.ns["_D1_WALLET"].buy_product.assert_not_awaited()

    async def test_unknown_checkout_and_old_unsigned_confirm_cannot_purchase(self):
        self.service.one.return_value = None
        await self.callback("wc:confirm:" + "b" * 32)
        await self.callback("shop_ok:7:usd:1")
        self.service.confirm.assert_not_awaited()
        self.ns["send_product_page"].assert_awaited_once()

    async def test_pending_activation_does_not_show_completed_receipt(self):
        self.order["state"] = "pending"
        event = await self.callback("wc:confirm:" + "a" * 32)
        self.assertEqual(event.edit.call_args.args[0], "Activation in progress")

    async def test_direct_crypto_completion_uses_supplier_purchase(self):
        status, payload = await self.ns["buy_product"](123, 7, "usd", 1)
        self.assertEqual(status, "ok")
        self.assertEqual(payload["content"], "account:password")
        self.service.checkout.assert_awaited_once_with(123, 7, "usd", 1)
        self.service.confirm.assert_awaited_once_with(123, "a" * 32)
        self.ns["_D1_WALLET"].buy_product.assert_not_awaited()

    async def test_local_purchase_still_uses_local_wallet(self):
        self.p["supplier_id"] = None
        await self.ns["buy_product"](123, 7, "usd", 1)
        self.ns["_D1_WALLET"].buy_product.assert_awaited_once_with(123, 7, "usd", 1)
        self.service.confirm.assert_not_awaited()

    async def test_footer_has_current_prices_warranty_and_stock_at_end(self):
        self.p["warranty_days"] = 30
        for stock in (10, 1, 0):
            self.ns["product_stock"].return_value = stock
            text, _ = await self.ns["build_product_page"](123, self.p)
            self.assertTrue(text.startswith("Description\n\n"))
            self.assertIn("Price: $8.00 / ₹800.00", text)
            self.assertIn("Warranty: 30 days", text)
            self.assertIn(f"Stock: {stock} available", text)
            self.assertIn("**💲 Price: $8.00 / ₹800.00**", text)
            self.assertIn("**🛡 Warranty: 30 days**", text)
            self.assertIn(f"**📊 Stock: {stock} available**", text)
            self.assertTrue(text.endswith("⚡ Delivery is automatic after payment confirmation."))

    async def test_sold_out_page_has_three_requested_buttons_and_no_purchase(self):
        self.ns["product_stock"].return_value = 0
        self.ns["product_learn_more"] = lambda p: "Guide"
        _, rows = await self.ns["build_product_page"](123, self.p)
        self.assertEqual([row[0].text for row in rows], [
            "❌ Out of Stock", "🔔 Notify me when back in stock", "Back to Store"])
        self.assertEqual([row[0].data for row in rows], [b"stock_empty:7", b"stock_notify:7", b"shop"])
        self.assertEqual(rows[0][0].style, "danger")
        self.assertEqual(rows[1][0].style, "success")

    async def test_notify_subscribes_only_for_available_product_that_is_sold_out(self):
        self.ns["product_stock"].return_value = 0
        await self.callback("stock_empty:7")
        self.ns["subscribe_product_stock"].assert_not_awaited()
        event = await self.callback("stock_notify:7")
        self.ns["subscribe_product_stock"].assert_awaited_once_with(7, 123)
        self.ns["send_product_page"].assert_awaited_once()
        self.assertEqual(
            event.respond.call_args.args[0],
            "✅ Done! I'll notify you as soon as this product is back in stock.",
        )
        self.ns["subscribe_product_stock"].reset_mock()
        self.ns["send_product_page"].reset_mock()
        self.ns["product_stock"].return_value = 3
        await self.callback("stock_notify:7")
        self.ns["subscribe_product_stock"].assert_not_awaited()
        self.ns["send_product_page"].assert_awaited_once()
        self.p["active"] = 0
        await self.callback("stock_notify:7")
        self.ns["subscribe_product_stock"].assert_not_awaited()

    async def test_all_quantity_routes_use_product_specific_custom_emoji(self):
        for emoji_id in (5323289282499064033, 5440502878491930005):
            self.p.update(emoji_char="📦", emoji_id=emoji_id)
            for route in ("shop_buy:7:usd", "shop_buy:7:inr", "shop_qtyc:7:usd",
                          "paycrypto:7", "paycqtyc:7"):
                with self.subTest(emoji_id=emoji_id, route=route):
                    event = await self.callback(route)
                    call = event.edit.call_args or event.respond.call_args
                    self.assertIn(f"[📦](emoji/{emoji_id})", call.args[0])

    async def test_product_emoji_overrides_global_mapping_in_telegram_entities(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8"))
        parser = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PremiumEmojiParseMode")
        self.ns.update({name: getattr(types, name) for name in (
            "MessageEntityTextUrl", "MessageEntityCustomEmoji", "MessageEntityBlockquote",
            "MessageEntityCode", "MessageEntityPre")})
        self.ns["_tl_markdown"] = markdown
        self.ns["PREMIUM_EMOJI_MAP"] = {"📦": 5323289282499064033}
        exec(compile(ast.Module(body=[parser], type_ignores=[]), "emoji-parser", "exec"), self.ns)
        self.p.update(emoji_char="📦", emoji_id=5440502878491930005)
        text, entities = self.ns["PremiumEmojiParseMode"].parse(
            self.ns["product_emoji"](self.p) + "**Product: Example**")
        emojis = [e for e in entities if isinstance(e, types.MessageEntityCustomEmoji)]
        self.assertEqual(text, "📦 Product: Example")
        self.assertEqual([e.document_id for e in emojis], [5440502878491930005])
        self.p.pop("emoji_id")
        self.p.pop("emoji_char")
        self.assertEqual(self.ns["product_emoji"](self.p), "")

    async def test_manual_and_supplier_on_demand_are_not_reported_as_empty_or_99(self):
        self.ns["product_stock"].return_value = 0
        self.p.update(supplier_id=None, fulfillment="manual")
        text, rows = await self.ns["build_product_page"](123, self.p)
        self.assertIn("Stock: On demand", text)
        self.assertIn("Delivery is by email invite", text)
        self.assertIn(b"paywallet:7", [b.data for row in rows for b in row])
        self.p.update(supplier_id=1, supplier_stock=None, fulfillment="instant")
        self.ns["product_stock"].return_value = 99
        text, _ = await self.ns["build_product_page"](123, self.p)
        self.assertIn("Stock: On demand", text)
        self.assertNotIn("99 available", text)

    async def test_admin_warranty_flow_and_customer_access(self):
        await self.callback("adm_warranty:7")
        self.assertNotIn(123, self.ns["ADMIN_STATE"])
        self.ns["ADMIN_IDS"] = [123]
        await self.callback("adm_warranty:7")
        self.assertEqual(self.ns["ADMIN_STATE"][123]["step"], "warranty_days")
        event = SimpleNamespace(reply=AsyncMock())
        await self.ns["handle_admin_input"](event, 123, "30")
        self.ns["set_product_warranty"].assert_awaited_once_with(7, 30)
        self.assertNotIn(123, self.ns["ADMIN_STATE"])


if __name__ == "__main__":
    unittest.main()
