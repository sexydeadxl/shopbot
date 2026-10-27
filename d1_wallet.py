"""Wallet mutations expressed as atomic D1 batches, shared by both entry points."""
from datetime import datetime, timedelta, timezone
import random
import json

from d1_database import connect, statement as sql, retry_conflicts, IntegrityError


async def one(db, query, params=()):
    return await (await db.execute(query, params)).fetchone()


class Wallet:
    def __init__(self, host):
        self.h = host

    async def supplier_order(self, oid):
        service = self.h.get('SUPPLIERS')
        return await service.order(oid) if service else None

    @retry_conflicts
    async def buy_product(self, user_id, product_id, currency, qty=1):
        currency = currency.lower()
        if currency not in ('usd', 'inr'):
            raise ValueError('Invalid currency')
        col, places = 'balance_' + currency, 6 if currency == 'usd' else 2
        now = datetime.now(timezone.utc).isoformat()
        async with connect() as db:
            prod = await one(db, 'SELECT * FROM products WHERE id=?', (product_id,))
            if not prod or not prod['active'] or prod['fulfillment'] == 'supplier':
                return ('gone', None)
            price = round(prod['price_' + currency] or 0, places)
            if price <= 0:
                return ('no_price', None)
            manual = prod['fulfillment'] == 'manual'
            qty = 1 if manual else max(1, min(int(qty), 99))
            items = []
            if not manual:
                items = await (await db.execute("SELECT id,content FROM product_items WHERE product_id=? AND status='available' ORDER BY id LIMIT ?", (product_id, qty))).fetchall()
                if len(items) < qty:
                    return ('out_of_stock', len(items))
            total = round(price * qty, places)
            user = await one(db, f'SELECT {col},banned FROM users WHERE user_id=?', (user_id,))
            if not user or user[1] or (user[0] or 0) < total:
                return ('insufficient', total)
            statements = [sql(f'UPDATE users SET {col}=ROUND({col}-?,{places}) WHERE user_id=? AND {col}>=? AND COALESCE(banned,0)=0', (total, user_id, total), 1)]
            if manual:
                statements.append(sql("INSERT INTO orders(user_id,product_id,item_id,currency,amount,created_at,status) VALUES(?,?,0,?,?,?,'awaiting_email')", (user_id, product_id, currency, price, now)))
            else:
                ids = json.dumps([item[0] for item in items])
                statements += [
                    sql("UPDATE product_items SET status='sold',sold_to=?,sold_at=? WHERE id IN (SELECT value FROM json_each(?)) AND status='available'", (user_id, now, ids), qty),
                    sql('INSERT INTO orders(user_id,product_id,item_id,currency,amount,created_at) SELECT ?,?,value,?,?,? FROM json_each(?)', (user_id, product_id, currency, price, now, ids), qty),
                ]
            statements.append(sql(f'SELECT {col} FROM users WHERE user_id=?', (user_id,)))
            result = await db.batch(statements, guarded=True)
            payload = dict(amount=total, currency=currency,
                           new_balance=(await result[-1].fetchone())[0], name=prod['name'])
            if manual:
                payload['order_id'] = result[1].lastrowid
                return ('ok_manual', payload)
            payload.update(item_id=items[0][0], content='\n'.join(i[1] for i in items), qty=qty)
            return ('ok', payload)

    @retry_conflicts
    async def refund_order(self, order_id):
        if await self.supplier_order(order_id):
            return ('supplier_managed', None)
        async with connect() as db:
            row = await one(db, 'SELECT o.*,p.name AS product_name FROM orders o LEFT JOIN products p ON p.id=o.product_id WHERE o.id=?', (order_id,))
            if not row:
                return ('not_found', None)
            if row['status'] == 'refunded':
                return ('already', None)
            uid, currency, amount = row['user_id'], row['currency'], row['amount']
            col, places = 'balance_' + currency, 6 if currency == 'usd' else 2
            if currency not in ('usd', 'inr'):
                raise ValueError('Invalid order currency')
            if not await one(db, 'SELECT 1 FROM users WHERE user_id=?', (uid,)):
                return ('no_user', None)
            result = await db.batch([
                sql("UPDATE orders SET status='refunded',refunded_at=? WHERE id=? AND status!='refunded'", (datetime.now(timezone.utc).isoformat(), order_id), 1),
                sql(f'UPDATE users SET {col}=ROUND(COALESCE({col},0)+?,{places}) WHERE user_id=?', (amount, uid), 1),
                sql(f'SELECT {col} FROM users WHERE user_id=?', (uid,)),
            ], guarded=True)
            return ('ok', dict(user_id=uid, currency=currency, amount=round(amount, places), new_balance=(await result[-1].fetchone())[0], product_name=row['product_name']))

    @retry_conflicts
    async def redeliver_order(self, order_id):
        if await self.supplier_order(order_id):
            return ('supplier_managed', None)
        async with connect() as db:
            row = await one(db, 'SELECT o.*,p.name AS product_name FROM orders o LEFT JOIN products p ON p.id=o.product_id WHERE o.id=?', (order_id,))
            if not row:
                return ('not_found', None)
            if row['status'] != 'completed':
                return ('already', None)
            if row['product_name'] is None:
                return ('gone', None)
            item = await one(db, "SELECT id,content FROM product_items WHERE product_id=? AND status='available' ORDER BY id LIMIT 1", (row['product_id'],))
            if not item:
                return ('out_of_stock', None)
            await db.batch([
                sql("UPDATE product_items SET status='sold',sold_to=?,sold_at=? WHERE id=? AND status='available'", (row['user_id'], datetime.now(timezone.utc).isoformat(), item[0]), 1),
                sql("UPDATE orders SET item_id=? WHERE id=? AND status='completed'", (item[0], order_id), 1),
            ], guarded=True)
            return ('ok', dict(user_id=row['user_id'], content=item[1], product_name=row['product_name']))

    async def attach_order_email(self, order_id, user_id, email):
        async with connect() as db:
            result = await db.execute("UPDATE orders SET email=?,status='processing' WHERE id=? AND user_id=? AND status='awaiting_email'", (email, order_id, user_id))
        return await self.h['get_order'](order_id) if result.rowcount else None

    async def mark_order_mailed(self, order_id):
        async with connect() as db:
            result = await db.execute("UPDATE orders SET status='delivered' WHERE id=? AND status='processing'", (order_id,))
        return await self.h['get_order'](order_id) if result.rowcount else None

    async def mark_order_received(self, order_id, user_id):
        async with connect() as db:
            result = await db.execute("UPDATE orders SET status='completed' WHERE id=? AND user_id=? AND status='delivered'", (order_id, user_id))
        return await self.h['get_order'](order_id) if result.rowcount else None

    @retry_conflicts
    async def _adjust(self, user_id, delta, currency):
        col, places = 'balance_' + currency, 6 if currency == 'usd' else 2
        async with connect() as db:
            row = await one(db, f'SELECT {col} FROM users WHERE user_id=?', (user_id,))
            if not row:
                return None
            old = round(row[0] or 0, places)
            new = round(max(0, old + delta), places)
            await db.batch([sql(f'UPDATE users SET {col}=? WHERE user_id=?', (new, user_id), 1)], guarded=True)
            return old, new

    async def admin_adjust_balance(self, user_id, delta):
        return await self._adjust(user_id, delta, 'usd')

    async def admin_adjust_balance_inr(self, user_id, delta):
        return await self._adjust(user_id, delta, 'inr')

    @retry_conflicts
    async def approve_upi_request(self, req_id, admin_id):
        async with connect() as db:
            row = await one(db, "SELECT user_id,amount_inr FROM upi_requests WHERE id=? AND status='pending'", (req_id,))
            if not row:
                return None
            uid, amount = row
            if not await one(db, 'SELECT 1 FROM users WHERE user_id=?', (uid,)):
                raise ValueError('Buyer missing; request was not approved.')
            result = await db.batch([
                sql("UPDATE upi_requests SET status='approved',decided_by=?,decided_at=? WHERE id=? AND status='pending'", (admin_id, datetime.now(timezone.utc).isoformat(), req_id), 1),
                sql('UPDATE users SET balance_inr=ROUND(COALESCE(balance_inr,0)+?,2) WHERE user_id=?', (amount, uid), 1),
                sql('SELECT balance_inr FROM users WHERE user_id=?', (uid,)),
            ], guarded=True)
            return uid, amount, (await result[-1].fetchone())[0]

    @retry_conflicts
    async def claim_and_credit_deposit(self, intent_id, user_id, amount_milli, tx_hash, token, amount, usd_value):
        now = datetime.now(timezone.utc).isoformat()
        async with connect() as db:
            intent = await one(db, "SELECT 1 FROM deposit_intents WHERE id=? AND user_id=? AND amount_milli=? AND status='pending' AND expires_at>?", (intent_id, user_id, amount_milli, now))
            if not intent:
                return ('not_claimed', None)
            if not await one(db, 'SELECT 1 FROM users WHERE user_id=?', (user_id,)):
                return ('not_claimed', None)
            try:
                result = await db.batch([
                    sql("UPDATE deposit_intents SET status='credited',tx_hash=? WHERE id=? AND status='pending' AND expires_at>?", (tx_hash, intent_id, now), 1),
                    sql('INSERT INTO deposits(user_id,tx_hash,token,amount,usd_value,created_at) VALUES(?,?,?,?,?,?)', (user_id, tx_hash, token, amount, usd_value, now)),
                    sql('UPDATE users SET balance_usd=ROUND(COALESCE(balance_usd,0)+?,6) WHERE user_id=?', (usd_value, user_id), 1),
                    sql('SELECT balance_usd FROM users WHERE user_id=?', (user_id,)),
                ], guarded=True)
            except IntegrityError:
                return ('dup_tx', None)
            return ('ok', (await result[-1].fetchone())[0])

    @retry_conflicts
    async def create_deposit_intent(self, user_id, base_amount, chain='bsc', usd_amount=None):
        scale = self.h.get('intent_unit_scale', lambda chain: 1000)(chain)
        base = int(round(base_amount * scale))
        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=self.h['DEPOSIT_INTENT_TTL_MINUTES'])
        async with connect() as db:
            taken = {r[0] for r in await (await db.execute("SELECT amount_milli FROM deposit_intents WHERE status='pending' AND expires_at>? AND user_id!=?", (now.isoformat(), user_id))).fetchall()}
            chosen = None
            for hi in (self.h['DEPOSIT_SUFFIX_MAX'], 999):
                suffixes = list(range(1, hi + 1))
                random.shuffle(suffixes)
                chosen = next((base+s for s in suffixes if base+s not in taken), None)
                if chosen is not None:
                    break
            if chosen is None:
                return None
            result = await db.batch([
                sql("UPDATE deposit_intents SET status='expired' WHERE status='pending' AND (expires_at<=? OR user_id=?)", (now.isoformat(), user_id)),
                sql("INSERT INTO deposit_intents(user_id,base_milli,amount_milli,chain,status,created_at,expires_at) VALUES(?,?,?,?,'pending',?,?)", (user_id, base, chosen, chain, now.isoformat(), expires.isoformat())),
            ], guarded=True)
            return dict(id=result[-1].lastrowid, amount_milli=chosen, amount=chosen/scale, chain=chain, usd_amount=usd_amount, created_at=now, expires_at=expires)

    @retry_conflicts
    async def process_referral(self, referrer_id, referred_user_id):
        if referrer_id == referred_user_id:
            return False
        async with connect() as db:
            if not await one(db, 'SELECT 1 FROM users WHERE user_id=?', (referrer_id,)):
                return False
            row = await one(db, 'SELECT referral_rewarded FROM users WHERE user_id=?', (referred_user_id,))
            if not row or row[0] != 0 or await one(db, 'SELECT 1 FROM referrals WHERE referred_user=?', (referred_user_id,)):
                return False
            reward = self.h['REFERRAL_REWARD_CREDITS']
            await db.batch([
                sql('UPDATE users SET referral_rewarded=1 WHERE user_id=? AND referral_rewarded=0', (referred_user_id,), 1),
                sql('UPDATE users SET credits=credits+? WHERE user_id=?', (reward, referrer_id), 1),
                sql('INSERT INTO referrals(referrer_id,referred_user,reward,created_at) VALUES(?,?,?,?)', (referrer_id, referred_user_id, reward, datetime.now(timezone.utc).isoformat())),
            ], guarded=True)
            return True

    async def deduct_credit(self, user_id):
        async with connect() as db:
            result = await db.execute('UPDATE users SET credits=credits-1 WHERE user_id=? AND credits>0', (user_id,))
            return result.rowcount == 1

    async def add_credits(self, user_id, amount):
        async with connect() as db:
            row = await one(db, 'UPDATE users SET credits=credits+? WHERE user_id=? RETURNING credits', (amount, user_id))
            return row[0] if row else 0

    async def remove_credits(self, user_id, amount):
        async with connect() as db:
            row = await one(db, 'UPDATE users SET credits=MAX(0,credits-?) WHERE user_id=? RETURNING credits', (amount, user_id))
            return row[0] if row else 0

    async def add_balance_usd(self, user_id, amount):
        async with connect() as db:
            row = await one(db, 'UPDATE users SET balance_usd=ROUND(COALESCE(balance_usd,0)+?,6) WHERE user_id=? RETURNING balance_usd', (amount, user_id))
            return row[0] if row else 0.0

    @retry_conflicts
    async def delete_user_data(self, uid):
        async with connect() as db:
            if await one(db, "SELECT 1 FROM wholesale_orders w JOIN orders o ON o.id=w.order_id WHERE o.user_id=? AND w.state NOT IN ('completed','refunded') LIMIT 1", (uid,)):
                raise ValueError('Resolve pending supplier purchases before deleting this user.')
            entries = [
                ('supplier_checkouts', sql('DELETE FROM wholesale_checkouts WHERE user_id=?', (uid,))),
                ('supplier_orders', sql('DELETE FROM wholesale_orders WHERE order_id IN (SELECT id FROM orders WHERE user_id=?)', (uid,))),
            ]
            entries += [(t, sql(f'DELETE FROM {t} WHERE user_id=?', (uid,))) for t in ('users', 'orders', 'deposits', 'deposit_intents', 'upi_requests')]
            entries += [('referrals', sql('DELETE FROM referrals WHERE referrer_id=? OR referred_user=?', (uid, uid))),
                        ('items_anonymized', sql('UPDATE product_items SET sold_to=NULL WHERE sold_to=?', (uid,)))]
            results = await db.batch([s for _, s in entries], guarded=True)
            return {label: result.rowcount for (label, _), result in zip(entries, results)}
