# Move the bot to Cloudflare D1

The Python bot continues running on your VPS. Its application data now lives in
D1, accessed through a private, token-protected Cloudflare Worker. Both `bot.py`
and `main.py` use the new database layer; run only your usual entry point.

Your screenshot's database is already configured in `cloudflare/wrangler.toml`:

- Name: `shopbot`
- ID: `718f2a6d-9628-41f1-bae9-9ee39b2d864b`
- Worker binding: `DB`

No remote deployment or data import has been performed by these code changes.

## 1. Stop the old bot and export its data

Stop every bot process/service that writes to `wallet_bot.db`. Run the export on
the machine with the latest database. Keep `wallet_bot.db`, `wallet_bot.db-wal`,
and `wallet_bot.db-shm` together; do not delete the WAL file before exporting.

From the project directory:

```sh
python -m pip install -r requirements.txt
python migrate_d1.py export --source wallet_bot.db --output-dir d1-migration
```

This creates:

- `d1-migration/wallet_bot.snapshot.db`: a consistent SQLite backup, including
  committed WAL data.
- `d1-migration/wallet_bot.sql`: a D1-compatible schema and data export.
- `d1-migration/manifest.json`: counts and data hashes for every application table.

The export preserves table data, IDs, indexes, and AUTOINCREMENT sequences,
including supplier tables and tables unknown to the current code. Telegram emoji
IDs become TEXT in D1 so JavaScript cannot round them. Python converts them back
to integers when reading them.

The original database is not changed. An existing export directory is never
overwritten; choose another `--output-dir` for a later snapshot. Keep exports
private: they contain customer records and product inventory.

## 2. Import into the empty `shopbot` database

Install Node.js 22 or later on the machine used for deployment, then:

```sh
cd cloudflare
npm ci
npx wrangler login
npx wrangler d1 execute shopbot --remote --file=../d1-migration/wallet_bot.sql
```

Log in to the Cloudflare account shown in your screenshot. The target must be
empty before this first import. Import **before** starting the new bot, since
startup creates missing tables. Do not repeatedly import into a populated
database. If an import fails, inspect the error and target contents before retrying;
the script intentionally does not drop or overwrite remote tables.

For a different/new database, create it in Cloudflare's **Storage & databases →
D1 SQL Database → Create database**, then put its name and ID in
`cloudflare/wrangler.toml` before running the import.

## 3. Deploy the database Worker and set its secret

Still inside `cloudflare`:

```sh
npx wrangler deploy
npx wrangler secret put drastic
```

Enter a long random secret when prompted. You can generate one in another terminal:

```sh
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Save the same secret for the next step. Copy the HTTPS Worker URL printed by
deployment, for example `https://shoopbot-d1.your-subdomain.workers.dev`.
The Worker rejects requests until its secret is configured.

## 4. Configure Python, verify the imported data, then start

Return to the project directory. Create `.env` beside `bot.py` using `.env.example`:

```dotenv
D1_WORKER_URL=https://shoopbot-d1.your-subdomain.workers.dev
D1_WORKER_TOKEN=the-same-secret-you-entered-in-cloudflare
```

Use the Worker origin, without `/query`. Existing environment variables take
precedence over `.env`. Preserve your existing Telegram configuration and the
existing `.supplier-key` file or `SUPPLIER_ENCRYPTION_KEY`; generating a new
encryption key would prevent decryption of imported supplier credentials.

With the bot still stopped:

```sh
python migrate_d1.py verify --manifest d1-migration/manifest.json
python bot.py
```

Start only after verification says every table matches. If your deployment has
always used `main.py`, that entry point is also migrated: use `python main.py`
instead. Existing startup migrations add any missing application columns/tables.

## Runtime and backups

- D1 is the only live application database. Missing configuration causes an error;
  there is no fallback to the old `wallet_bot.db`.
- Wallet deductions, stock claims, refunds, UPI approvals, crypto credits,
  referrals, and supplier reservations use D1 atomic batches. Concurrent changes
  cause a rolled-back operation to retry with fresh reads.
- All application writers must use this Worker. Stop the bot before direct
  database edits/imports through the dashboard or Wrangler: concurrency checks
  track writes through the Worker, not arbitrary external SQL clients.
- Network failures are not blindly retried for writes because D1 may already have
  committed. Check order/deposit history before repeating an uncertain operation.
- The admin backup feature now creates its SQLite download from D1. It verifies
  that data did not change during export and retries rather than sending a mixed
  snapshot. Keep Cloudflare Time Travel available for recovery on busy databases.
- Product/category images, Telegram `.session` files, and the supplier encryption
  key remain files on the VPS. D1 holds application records, not these files.
- Keep the old database as an archive. Restarting the old SQLite bot after new D1
  writes would use stale balances and orders; export current D1 data before any
  deliberate rollback.

## Deploy the Telegram bot on Render

This repository includes a `render.yaml` Blueprint for a free Render **Web
Service**. The bot starts a lightweight `/healthz` endpoint on Render's `PORT`
while maintaining its long-lived Telegram connection.

1. Push the repository to a Git provider and create a Blueprint on Render from
   that repository.
2. Set `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and `TELEGRAM_BOT_TOKEN`.
3. Set `D1_WORKER_URL` to the deployed Worker origin (without `/query`) and
   `D1_WORKER_TOKEN` to the matching Cloudflare secret.
4. If supplier records already exist, set `SUPPLIER_ENCRYPTION_KEY` to the
   existing Fernet key. Do not generate a new key or supplier credentials in
   D1 will become unreadable.

The service uses `bot.py` as its entry point. Render's filesystem is ephemeral;
application data must remain in D1. The Telegram session is recreated when
needed, while images are packaged with the repository. Do not commit `.env`,
`.supplier-key`, or any other secret.

## Local verification for developers

No Cloudflare account is needed. In one terminal:

```sh
cd cloudflare
npm ci
npm run test:server
```

In another terminal, from the project directory:

```sh
python -m unittest discover -s tests -v
```

Tests use an isolated local D1 instance on `127.0.0.1:8799` and synthetic wallet
data. They override the D1 environment settings so production credentials cannot
be used. Stop the local test server with Ctrl+C when finished.

Reference: [Cloudflare's import/export guide](https://developers.cloudflare.com/d1/best-practices/import-export-data/),
[Worker access to D1](https://developers.cloudflare.com/d1/tutorials/build-an-api-to-access-d1/),
and [atomic D1 batches](https://developers.cloudflare.com/d1/worker-api/d1-database/#batch).
