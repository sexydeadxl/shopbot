"""Offline regression checks for manually priced wholesale products."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from supplier_panel import SupplierPanel
from suppliers import SupplierService


class ManualPricingTests(unittest.IsolatedAsyncioTestCase):
    def product(self, rate=0):
        return {
            "id": 7, "supplier_id": 1, "supplier_product_id": 12,
            "active": 0, "supplier_available": 1, "supplier_enabled": 1,
            "price_usd": 8, "price_inr": 800, "supplier_cost_usd": 5,
            "usd_inr_rate": rate,
        }

    def service(self, balance=100, stock=10):
        return SimpleNamespace(call=AsyncMock(return_value={
            "quote": {"product_id": 12, "quantity": 1, "total": 5,
                      "stock": stock, "delivery_type": "stock"},
            "wallet_balance": balance,
        }))

    async def test_publish_manual_inr_without_rate_or_with_old_rate(self):
        for rate in (0, 1000):
            with self.subTest(rate=rate):
                service = SimpleNamespace(
                    product=AsyncMock(return_value=self.product(rate)),
                    write=AsyncMock(), invalidate=Mock(),
                )
                panel = SupplierPanel(service, None)
                panel.audit = AsyncMock()
                panel.product_admin = AsyncMock(return_value=("Published", []))
                await panel.admin_callback(1, ["wa", "publish", "7"])
                service.write.assert_awaited_once_with(
                    "UPDATE products SET active=? WHERE id=?", (1, 7))

    async def test_inr_checkout_uses_manual_price_without_rate_or_with_old_rate(self):
        for rate in (0, 1000):
            with self.subTest(rate=rate):
                cost = await SupplierService.validate_quote(
                    self.service(), self.product(rate), "inr", 1, 800)
                self.assertEqual(cost, 5)

    async def test_usd_checkout_accepts_eight_dollars(self):
        self.assertEqual(await SupplierService.validate_quote(
            self.service(), self.product(), "usd", 1, 8), 5)

    async def test_supplier_balance_and_stock_still_required(self):
        for service in (self.service(balance=4), self.service(stock=0)):
            with self.assertRaises(ValueError):
                await SupplierService.validate_quote(
                    service, self.product(), "inr", 1, 800)

    async def test_usd_cost_check_still_applies(self):
        with self.assertRaisesRegex(ValueError, "price needs updating"):
            await SupplierService.validate_quote(
                self.service(), self.product(), "usd", 1, 4)


if __name__ == "__main__":
    unittest.main()
