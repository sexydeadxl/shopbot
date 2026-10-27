"""Run against `node cloudflare/local-test-server.mjs`; uses no live services."""
import ast
import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon import Button, errors

# Hard-coded local endpoint so these tests can NEVER use a production .env.
os.environ['D1_WORKER_URL'] = 'http://127.0.0.1:8799'
os.environ['D1_WORKER_TOKEN'] = 'local-test-only'

from d1_database import connect, statement as sql, Conflict, IntegrityError, D1Error
from d1_wallet import Wallet
from d1_backup import snapshot
from migrate_d1 import export
from suppliers import SupplierService

ROOT = Path(__file__).resolve().parents[1]


class D1Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async with connect() as db:
            tables = await (await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '_cf_%' AND name NOT LIKE '_d1_%'")).fetchall()
            for row in tables:
                await db.execute('DROP TABLE "' + row[0] + '"')
        self.service = SupplierService('unused.db', encryption_key='not-used-by-tests')
        namespace = {'get_db': connect, 'SUPPLIERS': self.service}
        tree = ast.parse((ROOT / 'bot.py').read_text(encoding='utf-8'))
        init = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'init_db')
        exec(compile(ast.Module(body=[init], type_ignores=[]), 'bot.py:init_db', 'exec'), namespace)
        await namespace['init_db']()
        self.wallet = Wallet({'DEPOSIT_INTENT_TTL_MINUTES': 15,
                              'DEPOSIT_SUFFIX_MAX': 99, 'REFERRAL_REWARD_CREDITS': 3})
        async with connect() as db:
            await db.batch([
                sql('INSERT INTO users(user_id,balance_usd,balance_inr) VALUES(1,20,100),(2,20,100)'),
                sql("INSERT INTO products(id,name,price_usd,price_inr,created_at) VALUES(1,'test',10,50,'now')"),
                sql("INSERT INTO product_items(product_id,content,created_at) VALUES(1,'secret-code','now')"),
            ])

    async def one(self, query, params=()):
        async with connect() as db:
            return await (await db.execute(query, params)).fetchone()

    def product_ui_namespace(self):
        names = {'init_db', 'get_product', 'product_stock', 'product_is_manual',
                 'product_emoji', 'subscribe_product_stock', 'notify_product_restock',
                 'set_product_warranty'}
        tree = ast.parse((ROOT / 'bot.py').read_text(encoding='utf-8'))
        functions = [n for n in tree.body if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name in names]
        namespace = dict(get_db=connect, SUPPLIERS=self.service, asyncio=asyncio,
                         client=SimpleNamespace(send_message=AsyncMock()), Button=Button, errors=errors,
                         ensure_user_not_banned=AsyncMock(return_value=False))
        exec(compile(ast.Module(body=functions, type_ignores=[]), 'product-ui', 'exec'), namespace)
        return namespace

    async def test_warranty_migration_and_saved_values_survive_reinitialization(self):
        ns = self.product_ui_namespace()
        self.assertEqual((await ns['get_product'](1))['warranty_days'], 0)
        for bad_value in (-1, 36501, 1.5, True):
            with self.assertRaises(ValueError):
                await ns['set_product_warranty'](1, bad_value)
        await ns['set_product_warranty'](1, 30)
        await ns['init_db']()
        self.assertEqual((await ns['get_product'](1))['warranty_days'], 30)
        self.assertEqual(await ns['product_stock'](1), 1)

    async def test_restock_subscription_is_persistent_deduplicated_and_sent_once(self):
        ns = self.product_ui_namespace()
        async with connect() as db:
            await db.execute("UPDATE product_items SET status='sold' WHERE product_id=1")
        await ns['subscribe_product_stock'](1, 1)
        await ns['subscribe_product_stock'](1, 1)
        await ns['init_db']()
        self.assertEqual((await self.one('SELECT COUNT(*) FROM product_stock_subscriptions'))[0], 1)
        await ns['notify_product_restock']()
        ns['client'].send_message.assert_not_awaited()
        async with connect() as db:
            await db.execute("UPDATE product_items SET status='available' WHERE product_id=1")
        await ns['notify_product_restock']()
        ns['client'].send_message.assert_awaited_once()
        self.assertEqual(ns['client'].send_message.call_args.args[0], 1)
        self.assertEqual((await self.one('SELECT COUNT(*) FROM product_stock_subscriptions'))[0], 0)
        await ns['notify_product_restock']()
        ns['client'].send_message.assert_awaited_once()

    async def test_restock_retries_failed_sends_and_skips_hidden_products(self):
        ns = self.product_ui_namespace()
        await ns['subscribe_product_stock'](1, 1)
        async with connect() as db:
            await db.execute('UPDATE products SET active=0 WHERE id=1')
        await ns['notify_product_restock']()
        ns['client'].send_message.assert_not_awaited()
        async with connect() as db:
            await db.execute('UPDATE products SET active=1 WHERE id=1')
        ns['client'].send_message.side_effect = OSError('temporary failure')
        await ns['notify_product_restock']()
        self.assertEqual((await self.one('SELECT COUNT(*) FROM product_stock_subscriptions'))[0], 1)
        ns['client'].send_message.side_effect = None
        await ns['notify_product_restock']()
        self.assertEqual((await self.one('SELECT COUNT(*) FROM product_stock_subscriptions'))[0], 0)

    async def test_restock_uses_supplier_stock_without_local_inventory(self):
        ns = self.product_ui_namespace()
        ns['SUPPLIERS'] = SimpleNamespace(product=AsyncMock(return_value=dict(
            supplier_enabled=1, supplier_available=1, supplier_stock=0)))
        async with connect() as db:
            await db.execute('UPDATE products SET supplier_id=1 WHERE id=1')
        await ns['subscribe_product_stock'](1, 1)
        await ns['notify_product_restock']()
        ns['client'].send_message.assert_not_awaited()
        ns['SUPPLIERS'].product.return_value['supplier_stock'] = 2
        await ns['notify_product_restock']()
        ns['client'].send_message.assert_awaited_once()

    async def test_concurrent_purchase_never_double_sells(self):
        results = await asyncio.gather(self.wallet.buy_product(1, 1, 'usd'), self.wallet.buy_product(2, 1, 'usd'))
        self.assertEqual(sorted(r[0] for r in results), ['ok', 'out_of_stock'])
        self.assertEqual((await self.one('SELECT SUM(balance_usd) FROM users'))[0], 30)
        self.assertEqual((await self.one('SELECT COUNT(*) FROM orders'))[0], 1)

    async def test_concurrent_refund_once(self):
        await self.wallet.buy_product(1, 1, 'usd')
        oid = (await self.one('SELECT id FROM orders'))[0]
        results = await asyncio.gather(self.wallet.refund_order(oid), self.wallet.refund_order(oid))
        self.assertEqual(sorted(r[0] for r in results), ['already', 'ok'])
        self.assertEqual((await self.one('SELECT balance_usd FROM users WHERE user_id=1'))[0], 20)

    async def test_large_quantity_atomic_and_insufficient(self):
        async with connect() as db:
            await db.execute('UPDATE users SET balance_usd=2000 WHERE user_id=1')
            await db.executemany("INSERT INTO product_items(product_id,content,created_at) VALUES(1,?,'now')", [(f'code-{i}',) for i in range(98)])
        result = await self.wallet.buy_product(1, 1, 'usd', 99)
        self.assertEqual(result[0], 'ok')
        self.assertEqual(result[1]['new_balance'], 1010)
        self.assertEqual((await self.one('SELECT COUNT(*) FROM orders'))[0], 99)
        async with connect() as db:
            await db.execute("INSERT INTO product_items(product_id,content,created_at) VALUES(1,'extra','now')")
            await db.execute('UPDATE users SET balance_usd=0 WHERE user_id=1')
        self.assertEqual((await self.wallet.buy_product(1, 1, 'usd'))[0], 'insufficient')
        self.assertEqual((await self.one("SELECT COUNT(*) FROM product_items WHERE status='available'"))[0], 1)

    async def test_duplicate_deposit_rolls_back_whole_batch(self):
        a = await self.wallet.create_deposit_intent(1, 5)
        b = await self.wallet.create_deposit_intent(2, 5)
        self.assertEqual((await self.wallet.claim_and_credit_deposit(a['id'], 1, a['amount_milli'], 'tx-1', 'USDT', 5, 5))[0], 'ok')
        self.assertEqual((await self.wallet.claim_and_credit_deposit(b['id'], 2, b['amount_milli'], 'tx-1', 'USDT', 5, 5))[0], 'dup_tx')
        self.assertEqual((await self.one('SELECT status FROM deposit_intents WHERE id=?', (b['id'],)))[0], 'pending')
        self.assertEqual((await self.one('SELECT balance_usd FROM users WHERE user_id=2'))[0], 20)

    async def test_upi_and_referral_at_most_once(self):
        async with connect() as db:
            await db.execute("INSERT INTO upi_requests(user_id,amount_inr,status,created_at) VALUES(1,50,'pending','now')")
        results = await asyncio.gather(self.wallet.approve_upi_request(1, 7), self.wallet.approve_upi_request(1, 7))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual((await self.one('SELECT balance_inr FROM users WHERE user_id=1'))[0], 150)
        self.assertEqual(sorted(await asyncio.gather(self.wallet.process_referral(1, 2), self.wallet.process_referral(1, 2))), [False, True])
        self.assertEqual((await self.one('SELECT credits FROM users WHERE user_id=1'))[0], 7)

    async def test_guard_and_change_count_rollback(self):
        async with connect() as a, connect() as b:
            await a.execute('SELECT balance_usd FROM users WHERE user_id=1')
            await b.execute('UPDATE users SET balance_usd=19 WHERE user_id=1')
            with self.assertRaises(Conflict):
                await a.batch([sql('UPDATE users SET balance_usd=0 WHERE user_id=1')], guarded=True)
            with self.assertRaises(Conflict):
                await b.batch([sql('UPDATE users SET balance_usd=0 WHERE user_id=1'), sql('UPDATE users SET balance_usd=0 WHERE user_id=999', (), 1)])
        self.assertEqual((await self.one('SELECT balance_usd FROM users WHERE user_id=1'))[0], 19)

    async def test_emoji_precision_and_backup(self):
        emoji = 5440502878491930005
        async with connect() as db:
            await db.execute('INSERT INTO emoji_map VALUES(?,?)', ('x', emoji))
        self.assertEqual((await self.one('SELECT doc_id FROM emoji_map'))[0], emoji)
        with tempfile.TemporaryDirectory() as directory:
            filename = await snapshot(directory)
            with closing(sqlite3.connect(filename)) as db:
                self.assertEqual(db.execute('SELECT doc_id FROM emoji_map').fetchone()[0], str(emoji))
                self.assertEqual(db.execute('SELECT COUNT(*) FROM users').fetchone()[0], 2)

    async def test_supplier_reserve_and_refund(self):
        async with connect() as db:
            await db.batch([
                sql("INSERT INTO wholesale_suppliers(id,name,base_url,encrypted_key,key_prefix,account_id,created_at) VALUES(1,'s','https://example.com','unused','x',1,'now')"),
                sql("UPDATE products SET fulfillment='supplier',supplier_id=1,supplier_product_id=7,supplier_available=1 WHERE id=1"),
            ])
        checkout = dict(supplier_id=1, supplier_product_id=7, remote_product_id=7, amount=10, product_name='test')
        p = dict(id=1, supplier_id=1)
        results = await asyncio.gather(*[self.service._reserve_order(1, 'token', checkout, p, 'usd', 1, 5, {}) for _ in range(2)])
        self.assertEqual(results[0], results[1])
        self.assertEqual((await self.one('SELECT balance_usd FROM users WHERE user_id=1'))[0], 10)
        self.assertEqual((await self.one('SELECT order_id FROM wholesale_orders'))[0], results[0])
        leases = await asyncio.gather(self.service._lease_order(results[0]), self.service._lease_order(results[0]))
        self.assertEqual(sum(acquired for _, acquired in leases), 1)
        await asyncio.gather(self.service.refund(results[0], 'test'), self.service.refund(results[0], 'test'))
        self.assertEqual((await self.one('SELECT balance_usd FROM users WHERE user_id=1'))[0], 20)

    async def test_supplier_catalog_sync_and_import(self):
        async with connect() as db:
            await db.batch([
                sql("INSERT INTO wholesale_suppliers(id,name,base_url,encrypted_key,key_prefix,account_id,created_at) VALUES(1,'s','https://example.com','unused','x',1,'now')"),
                sql("UPDATE products SET supplier_id=1,supplier_product_id=7,supplier_available=0 WHERE id=1"),
            ])
        async def fake_call(*args):
            return {'products': [dict(id=7,name='test',price_usd='5.50',stock=12,delivery_type='stock',api_test=False),
                                 dict(id=8,name='new',price_usd='4.00',stock=None,delivery_type='activation',api_test=False)]}
        self.service.call = fake_call
        self.assertEqual(await self.service.sync(1), 2)
        row = await self.one('SELECT supplier_cost_usd,supplier_stock,supplier_available FROM products WHERE id=1')
        self.assertEqual(tuple(row), (5.5,12,1))
        pid = await self.service.import_product(1,8)
        self.assertEqual((await self.one('SELECT price_usd,active FROM products WHERE id=?', (pid,)))[0], 4.8)

    async def test_manual_order_and_redelivery(self):
        async with connect() as db:
            await db.execute("UPDATE products SET fulfillment='manual' WHERE id=1")
        result = await self.wallet.buy_product(1,1,'inr')
        self.assertEqual(result[0], 'ok_manual')
        oid = result[1]['order_id']
        self.wallet.h['get_order'] = lambda oid: self.one('SELECT * FROM orders WHERE id=?',(oid,))
        self.assertIsNone(await self.wallet.attach_order_email(oid,2,'wrong@example.com'))
        self.assertIsNotNone(await self.wallet.attach_order_email(oid,1,'buyer@example.com'))
        self.assertIsNotNone(await self.wallet.mark_order_mailed(oid))
        self.assertIsNotNone(await self.wallet.mark_order_received(oid,1))
        self.assertEqual((await self.wallet.redeliver_order(oid))[0], 'ok')
        self.assertEqual((await self.wallet.redeliver_order(oid))[0], 'out_of_stock')

    async def test_legacy_main_initializes_with_d1(self):
        namespace = {'database': __import__('d1_database'), 'DB_PATH': 'unused.db'}
        tree = ast.parse((ROOT/'main.py').read_text(encoding='utf-8'))
        init = next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='init_db')
        exec(compile(ast.Module(body=[init],type_ignores=[]),'main.py:init_db','exec'),namespace)
        await namespace['init_db']()
        intent = await self.wallet.create_deposit_intent(1,5,'bsc')
        self.assertGreater(intent['amount_milli'],5000)

    async def test_authentication_and_explicit_transactions_rejected(self):
        async with connect() as db:
            with self.assertRaises(D1Error):
                await db.execute('BEGIN IMMEDIATE')
            db.token = 'incorrect'
            with self.assertRaises(D1Error):
                await db.execute('SELECT 1')


class ExportTests(unittest.TestCase):
    def test_wal_export_retains_rows_ids_and_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.db'
            db = sqlite3.connect(source)
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE emoji_map(id INTEGER PRIMARY KEY AUTOINCREMENT,doc_id INTEGER,note TEXT)')
            db.execute('INSERT INTO emoji_map VALUES(100,?,?)', (5440502878491930005, 'line1\nquote\'line2'))
            db.execute('INSERT INTO emoji_map VALUES(101,1,NULL)')
            db.execute('DELETE FROM emoji_map WHERE id=101')
            db.commit()
            try:
                manifest = export(source, Path(directory) / 'export')
                self.assertEqual(manifest['tables']['emoji_map']['count'], 1)
                clone = sqlite3.connect(':memory:')
                clone.executescript((Path(directory) / 'export/wallet_bot.sql').read_text(encoding='utf-8'))
                self.assertEqual(clone.execute('SELECT doc_id FROM emoji_map').fetchone()[0], '5440502878491930005')
                self.assertEqual(clone.execute('SELECT seq FROM sqlite_sequence').fetchone()[0], 101)
                clone.close()
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
