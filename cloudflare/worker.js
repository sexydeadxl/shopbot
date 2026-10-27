// All application writes go through this endpoint. Optimistic version checks
// allow Python to validate inputs before committing dependent writes in one D1
// transaction. Do not edit the database with other tools while the bot runs.
const internalSchema = [
  'CREATE TABLE IF NOT EXISTS _d1_version (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)',
  'INSERT OR IGNORE INTO _d1_version VALUES(1,0)',
  'CREATE TABLE IF NOT EXISTS _d1_assert (value INTEGER CONSTRAINT d1_assertion CHECK(value=1))',
];

export async function execute(db, body) {
  const { statements, version } = body;
  if (!Array.isArray(statements) || !statements.length || statements.length > 400)
    throw new Error('Invalid batch size');
  await db.batch(internalSchema.map(sql => db.prepare(sql)));
  const batch = [];
  // A failed CHECK rolls the entire batch back, including previous mutations.
  if (version !== null && version !== undefined) {
    if (!Number.isSafeInteger(version)) throw new Error('Invalid version');
    batch.push(db.prepare('INSERT INTO _d1_assert SELECT 0 WHERE (SELECT version FROM _d1_version WHERE id=1) != ?').bind(version));
  }
  batch.push(db.prepare('SELECT version FROM _d1_version WHERE id=1'));
  const versionIndex = batch.length - 1;
  const positions = [];
  let writes = false;
  for (const s of statements) {
    if (typeof s.sql !== 'string' || !Array.isArray(s.params)) throw new Error('Invalid statement');
    const head = s.sql.trim().split(/\s+/)[0].toUpperCase();
    if (!['SELECT', 'PRAGMA', 'INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER', 'DROP', 'WITH'].includes(head))
      throw new Error('Unsupported statement');
    // A WITH can contain a write; conservatively count it as a mutation.
    writes ||= !['SELECT', 'PRAGMA'].includes(head);
    positions.push(batch.length);
    batch.push(db.prepare(s.sql).bind(...s.params));
    if (s.changes !== undefined) {
      if (!Number.isSafeInteger(s.changes) || s.changes < 0) throw new Error('Invalid change count');
      batch.push(db.prepare('INSERT INTO _d1_assert SELECT 0 WHERE changes() != ?').bind(s.changes));
    }
  }
  if (writes) batch.push(db.prepare('UPDATE _d1_version SET version=version+1 WHERE id=1'));
  const results = await db.batch(batch);
  return { success: true, version: results[versionIndex].results[0].version,
    results: positions.map(i => results[i]) };
}

export default {
  async fetch(request, env) {
    if (!env.D1_WORKER_TOKEN || request.headers.get('Authorization') !== `Bearer ${env.D1_WORKER_TOKEN}`)
      return Response.json({error: 'Unauthorized'}, {status: 401});
    if (request.method !== 'POST' || new URL(request.url).pathname !== '/query')
      return Response.json({error: 'Not found'}, {status: 404});
    try {
      const body = await request.json();
      // first-primary also makes reads current when D1 read replication is enabled.
      return Response.json(await execute(env.DB.withSession('first-primary'), body));
    } catch (error) {
      const message = String(error.message);
      if (message.includes('d1_assertion'))
        return Response.json({error: 'Concurrent change; transaction rolled back'}, {status: 409});
      if (/UNIQUE constraint|NOT NULL constraint|FOREIGN KEY constraint/.test(message))
        return Response.json({error: 'Database constraint failed'}, {status: 422});
      // Do not log bound parameters, supplier credentials or inventory content.
      return Response.json({error: 'Database request failed'}, {status: 500});
    }
  }
};
