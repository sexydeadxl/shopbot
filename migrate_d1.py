"""Offline, WAL-aware SQLite export and read-only verification against D1.

No upload happens here. Import the SQL using the Wrangler command in D1_SETUP.md.
"""
import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def ident(name):
    return '"' + name.replace('"', '""') + '"'


def normalize_schema(sql):
    # Telegram emoji/document identifiers must cross a JavaScript Worker exactly.
    return re.sub(r'\b(emoji_id|doc_id)\s+INTEGER\b', r'\1 TEXT', sql, flags=re.I)


def row_digest(rows, columns):
    hashes = []
    for row in rows:
        values = []
        for name, value in zip(columns, row):
            if name in ('emoji_id', 'doc_id') and value is not None:
                value = str(value)
            if isinstance(value, bytes):
                value = {'blob': value.hex()}
            # JSON/JS can serialize 1.0 as 1. Normalize without changing data.
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            values.append(value)
        hashes.append(hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest())
    return hashlib.sha256('\n'.join(sorted(hashes)).encode()).hexdigest()


def export(source, output_dir):
    source, output_dir = Path(source).resolve(), Path(output_dir).resolve()
    if not source.is_file():
        raise ValueError(f'Source database not found: {source}')
    # Refuse overwrite: every export is a separate recoverable snapshot.
    output_dir.mkdir(parents=True, exist_ok=False)
    snapshot_path = output_dir / 'wallet_bot.snapshot.db'
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(snapshot_path)) as snapshot:
            src.backup(snapshot)  # includes committed wallet_bot.db-wal data
    with closing(sqlite3.connect(snapshot_path)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite integrity check failed; export stopped.')
        objects = db.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
        tables = [name for kind, name, _ in objects if kind == 'table']
        manifest = {'created_at': datetime.now(timezone.utc).isoformat(), 'tables': {}}
        for name in tables:
            columns = [r[1] for r in db.execute(f'PRAGMA table_info({ident(name)})')]
            rows = db.execute(f'SELECT * FROM {ident(name)}').fetchall()
            manifest['tables'][name] = {'count': len(rows), 'columns': columns, 'sha256': row_digest(rows, columns)}
        with (output_dir / 'wallet_bot.sql').open('w', encoding='utf-8', newline='\n') as out:
            out.write('-- Import into an EMPTY D1 database before starting the bot.\nPRAGMA defer_foreign_keys=ON;\n')
            for line in db.iterdump():
                if line in ('BEGIN TRANSACTION;', 'COMMIT;'):
                    continue
                if line.startswith('CREATE TABLE'):
                    line = normalize_schema(line)
                if len(line.encode('utf-8')) > 100_000:
                    raise ValueError('A row exceeds the D1 SQL import statement limit (100 KB).')
                out.write(line + '\n')
        (output_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        # Validate the exact SQL locally, including text IDs, indexes and sequences.
        with closing(sqlite3.connect(':memory:')) as check:
            check.executescript((output_dir / 'wallet_bot.sql').read_text(encoding='utf-8'))
            for name, info in manifest['tables'].items():
                rows = check.execute(f'SELECT * FROM {ident(name)}').fetchall()
                if len(rows) != info['count'] or row_digest(rows, info['columns']) != info['sha256']:
                    raise ValueError(f'Export validation failed: {name}')
    print(f'Export validated: {len(tables)} tables, {sum(t["count"] for t in manifest["tables"].values())} rows.')
    print(f'SQL: {output_dir / "wallet_bot.sql"}')
    print(f'Snapshot: {snapshot_path}')
    print('Keep the bot stopped until import and verification finish.')
    return manifest


async def verify(manifest_path):
    from d1_database import connect, statement
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    failures = []
    async with connect() as db:
        for name, info in manifest['tables'].items():
            columns = ','.join(ident(c) for c in info['columns'])
            rows = []
            offset = 0
            while True:
                page = await (await db.execute(f'SELECT {columns} FROM {ident(name)} ORDER BY {columns} LIMIT 500 OFFSET ?', (offset,))).fetchall()
                rows.extend(tuple(row) for row in page)
                offset += len(page)
                if len(page) < 500:
                    break
            good = len(rows) == info['count'] and row_digest(rows, info['columns']) == info['sha256']
            print(f'{name}: {len(rows)} rows — {"OK" if good else "MISMATCH"}')
            if not good:
                failures.append(name)
        await db.batch([statement('SELECT 1')], guarded=True)
    if failures:
        raise ValueError('Imported data differs: ' + ', '.join(failures))
    print('Every table matches the SQLite snapshot. You can start the bot.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    exp = commands.add_parser('export')
    exp.add_argument('--source', default='wallet_bot.db')
    exp.add_argument('--output-dir', default='d1-migration')
    check = commands.add_parser('verify')
    check.add_argument('--manifest', default='d1-migration/manifest.json')
    args = parser.parse_args()
    if args.command == 'export':
        export(args.source, args.output_dir)
    else:
        asyncio.run(verify(args.manifest))


if __name__ == '__main__':
    main()
