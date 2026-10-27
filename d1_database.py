"""Async D1 access through the authenticated Worker in cloudflare/.

Each connection is a lightweight request scope, never a local SQLite database.
Interactive SQLite transactions are deliberately unsupported: use batch().
"""
from __future__ import annotations

import asyncio
import functools
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name('.env'))
IntegrityError = sqlite3.IntegrityError


class D1Error(RuntimeError):
    pass


class Conflict(D1Error):
    """The database changed since the operation read its inputs; nothing committed."""


def retry_conflicts(fn):
    @functools.wraps(fn)
    async def wrapped(*args, **kwargs):
        for attempt in range(6):
            try:
                return await fn(*args, **kwargs)
            except Conflict:
                if attempt == 5:
                    raise
                await asyncio.sleep(0.02 * (attempt + 1))
    return wrapped


class Row:
    def __init__(self, values):
        self._values = dict(values)
        # Telegram document IDs exceed JavaScript's safe integer range. D1
        # stores these columns as TEXT; expose Python integers to the bot.
        for key in ('emoji_id', 'doc_id'):
            if self._values.get(key) is not None:
                self._values[key] = int(self._values[key])

    def keys(self):
        return self._values.keys()

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return tuple(self._values.values())[key]
        return self._values[key]

    def __iter__(self):
        return iter(self._values.values())

    def __len__(self):
        return len(self._values)


class Cursor:
    def __init__(self, result):
        self.rows = [Row(row) for row in result.get('results', [])]
        self.rowcount = result.get('meta', {}).get('changes', 0)
        self.lastrowid = result.get('meta', {}).get('last_row_id')
        self.pos = 0

    async def fetchone(self):
        if self.pos >= len(self.rows):
            return None
        row = self.rows[self.pos]
        self.pos += 1
        return row

    async def fetchall(self):
        rows = self.rows[self.pos:]
        self.pos = len(self.rows)
        return rows

    async def close(self):
        pass


class Query:
    def __init__(self, coro):
        self.coro = coro

    def __await__(self):
        return self.coro.__await__()

    async def __aenter__(self):
        return await self.coro

    async def __aexit__(self, *args):
        return False


def statement(sql, params=(), changes=None):
    result = {'sql': sql, 'params': [
        str(p) if isinstance(p, int) and abs(p) > 2**53 - 1 else p
        for p in params
    ]}
    if changes is not None:
        result['changes'] = changes
    return result


class Connection:
    def __init__(self):
        self.version = None
        self.session = None
        self.row_factory = Row

    async def __aenter__(self):
        self.url = os.getenv('D1_WORKER_URL', '').rstrip('/')
        self.token = os.getenv('D1_WORKER_TOKEN', '')
        parsed = urlsplit(self.url)
        local = parsed.hostname in ('localhost', '127.0.0.1', '::1')
        if not self.url or not self.token:
            raise D1Error('Set D1_WORKER_URL and D1_WORKER_TOKEN in .env; see D1_SETUP.md.')
        if parsed.scheme != 'https' and not (local and parsed.scheme == 'http'):
            raise D1Error('D1_WORKER_URL must use HTTPS (except localhost development).')
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        return self

    async def __aexit__(self, *args):
        await self.session.close()

    async def request(self, payload):
        # Never retry network failures: a lost response may follow a committed
        # wallet mutation. Only explicit, rolled-back conflicts are retried.
        try:
            async with self.session.post(self.url + '/query', json=payload,
                    headers={'Authorization': 'Bearer ' + self.token},
                    allow_redirects=False) as response:
                data = await response.json(content_type=None)
                if response.status == 409:
                    raise Conflict('Database changed; please try again.')
                if response.status == 422:
                    raise IntegrityError(data.get('error', 'D1 constraint failed'))
                if response.status != 200 or not data.get('success'):
                    raise D1Error(f'D1 request failed (HTTP {response.status}). Check Worker configuration/logs.')
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise D1Error('D1 response unavailable. Check the operation status before repeating a wallet change.') from exc

    def execute(self, sql, params=()):
        return Query(self._execute(sql, params))

    async def _execute(self, sql, params):
        if sql.strip().split()[0].upper() in ('BEGIN', 'COMMIT', 'ROLLBACK', 'SAVEPOINT'):
            raise D1Error('D1 requires atomic batch(), not interactive transactions.')
        return (await self.batch([statement(sql, params)]))[0]

    async def batch(self, statements, *, guarded=False):
        if not statements:
            return []
        data = await self.request({'statements': statements,
                                   'version': self.version if guarded else None})
        if self.version is None:
            self.version = data['version']
        return [Cursor(result) for result in data['results']]

    async def executemany(self, sql, parameters):
        # Bulk inventory/emoji imports may exceed the Worker's batch limit.
        rows = list(parameters)
        for offset in range(0, len(rows), 40):
            await self.batch([statement(sql, p) for p in rows[offset:offset + 40]])

    async def commit(self):
        # execute() and batch() have already committed atomically on D1.
        pass


def connect(*args, **kwargs):
    """Legacy path arguments are ignored; there is no local database fallback."""
    return Connection()
