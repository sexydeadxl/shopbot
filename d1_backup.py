"""Consistent D1 -> SQLite backups for the existing admin backup feature."""
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from d1_database import connect, statement, retry_conflicts
from migrate_d1 import ident


@retry_conflicts
async def snapshot(directory):
    fd, filename = tempfile.mkstemp(prefix='wallet_bot_d1_backup_', suffix='.db', dir=directory)
    os.close(fd)
    try:
        async with connect() as db:
            schema = await (await db.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '_cf_%' AND name NOT LIKE '_d1_%' ORDER BY type,name")).fetchall()
            with closing(sqlite3.connect(filename)) as local:
                tables = [r for r in schema if r['type'] == 'table']
                for table in tables:
                    local.execute(table['sql'])
                    name, offset = ident(table['name']), 0
                    columns = await (await db.execute(f'PRAGMA table_info({name})')).fetchall()
                    names = ','.join(ident(c['name']) for c in columns)
                    while True:
                        rows = await (await db.execute(f'SELECT {names} FROM {name} ORDER BY {names} LIMIT 500 OFFSET ?', (offset,))).fetchall()
                        if rows:
                            local.executemany(f'INSERT INTO {name}({names}) VALUES({",".join("?" for _ in columns)})', [tuple(row) for row in rows])
                        offset += len(rows)
                        if len(rows) < 500:
                            break
                # Preserve AUTOINCREMENT high-water marks, even for empty tables.
                if any('AUTOINCREMENT' in t['sql'].upper() for t in tables):
                    sequences = await (await db.execute('SELECT name,seq FROM sqlite_sequence')).fetchall()
                    local.execute('DELETE FROM sqlite_sequence')
                    local.executemany('INSERT INTO sqlite_sequence VALUES(?,?)', [tuple(r) for r in sequences])
                for obj in schema:
                    if obj['type'] != 'table':
                        local.execute(obj['sql'])
                # If any application write occurred during pagination, discard
                # this snapshot and retry; never send an inconsistent backup.
                await db.batch([statement('SELECT 1')], guarded=True)
                local.commit()
        return filename
    except BaseException:
        Path(filename).unlink(missing_ok=True)
        raise
