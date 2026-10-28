import test from 'node:test';
import assert from 'node:assert/strict';
import { cp, mkdtemp, readFile, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { Client } from '@modelcontextprotocol/client';
import { StdioClientTransport } from '@modelcontextprotocol/client/stdio';

test('installed package starts outside checkout and serves its native panel', async (t) => {
  const stage = await mkdtemp(join(tmpdir(), 'auto-resume-package-'));
  t.after(() => rm(stage, {recursive:true, force:true}));
  await cp(new URL('../plugin/', import.meta.url), stage, {recursive:true});
  const manifest = JSON.parse(await readFile(join(stage,'.zcode-plugin/plugin.json'),'utf8'));
  const transport = new StdioClientTransport({command:process.execPath, args:[join(stage,'dist/server.mjs')], cwd:stage, stderr:'pipe'});
  const client = new Client({name:'package-test', version:'1.0.0'});
  t.after(() => client.close());
  await client.connect(transport);
  const {tools} = await client.listTools();
  const control = tools.find((tool) => tool.name === 'set_monitoring');
  assert.deepEqual(control._meta.ui.visibility, ['app']);
  assert.equal(tools.find((tool) => tool.name === 'get_monitor_status')._meta.ui.surface, 'monitor');
  const uri = manifest.ui.surfaces[0].resourceUri;
  const resource = await client.readResource({uri});
  assert.equal(resource.contents[0].mimeType, 'text/html;profile=mcp-app');
  assert.match(resource.contents[0].text, /开始检测/);
  assert.doesNotMatch(resource.contents[0].text, /<script[^>]+src=/);
  assert.ok(Buffer.byteLength(resource.contents[0].text) < 4*1024*1024);
  assert.equal(manifest.version, JSON.parse(await readFile(new URL('../package.json', import.meta.url),'utf8')).version);
});
