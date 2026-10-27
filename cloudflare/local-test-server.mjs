// Isolated, in-memory D1 runtime for Python integration tests. Never uses a
// Cloudflare account or the production database ID in wrangler.toml.
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';
import { readFile } from 'node:fs/promises';
const mf = new Miniflare(convertV4MiniflareOptions({
  modules: true,
  script: await readFile(new URL('./worker.js', import.meta.url), 'utf8'),
  compatibilityDate: '2026-10-13',
  d1Databases: ['DB'],
  bindings: {D1_WORKER_TOKEN: 'local-test-only'},
  host: '127.0.0.1', port: 8799,
}));
console.log(`READY ${await mf.ready}`);
process.on('SIGTERM', async () => { await mf.dispose(); process.exit(0); });
process.on('SIGINT', async () => { await mf.dispose(); process.exit(0); });
