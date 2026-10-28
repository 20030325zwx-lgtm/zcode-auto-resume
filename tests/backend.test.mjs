import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createBackend } from '../src/backend.mjs';

test('native panels share the existing switch and preserve the HTTP control checks', async (t) => {
  const stateDir = await mkdtemp(join(tmpdir(), 'auto-resume-backend-'));
  t.after(() => rm(stateDir, {recursive:true, force:true}));
  let monitoring = false;
  let changes = 0;
  const server = createServer((req, res) => {
    if (req.method === 'POST') {
      assert.equal(req.headers.origin, `http://127.0.0.1:${server.address().port}`);
      assert.equal(req.headers['x-zcode-control'], '1');
      monitoring = req.url === '/api/start';
      changes++;
    }
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({monitoring, pid:123, sessions:[], attempts_today_total:7}));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => { server.closeAllConnections(); server.close(); });
  await writeFile(join(stateDir,'dashboard.port'), String(server.address().port));
  const launch = () => { throw new Error('Should reuse existing daemon'); };
  const first = createBackend('/unused', {stateDir, launch});
  const second = createBackend('/unused', {stateDir, launch});
  assert.equal((await first.status()).monitoring, false);
  assert.equal(changes, 0);
  await first.setMonitoring(true);
  assert.equal((await second.status()).monitoring, true);
  assert.equal((await second.status()).attempts_today_total, 7);
  await second.setMonitoring(false);
  assert.equal((await first.status()).monitoring, false);
  assert.equal(changes, 2);
});

test('opening without a daemon starts the page service but does not enable monitoring', async (t) => {
  const stateDir = await mkdtemp(join(tmpdir(), 'auto-resume-launch-'));
  t.after(() => rm(stateDir, {recursive:true, force:true}));
  let launches = 0;
  const server = createServer((req, res) => {
    assert.equal(req.url, '/api/status');
    assert.equal(req.method, 'GET');
    res.end(JSON.stringify({monitoring:false}));
  });
  t.after(() => { server.closeAllConnections(); server.close(); });
  const backend = createBackend('/unused', {stateDir, launch:async () => {
    launches++;
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    await writeFile(join(stateDir,'dashboard.port'), String(server.address().port));
  }});
  const results = await Promise.all([backend.status(), backend.status()]);
  assert.equal(launches, 1);
  assert.ok(results.every((result) => result.monitoring === false));
});

test('launch failures are returned instead of inventing a successful state', async (t) => {
  const stateDir = await mkdtemp(join(tmpdir(), 'auto-resume-error-'));
  t.after(() => rm(stateDir, {recursive:true, force:true}));
  const backend = createBackend('/unused', {stateDir, launch:async () => { throw new Error('Python unavailable'); }});
  await assert.rejects(backend.status(), /Python unavailable/);
});
