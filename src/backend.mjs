import { readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { setTimeout as delay } from 'node:timers/promises';

// All panels share the existing monitor daemon and its daily retry counters.
export function createBackend(pluginRoot, options = {}) {
  const stateDir = options.stateDir ?? join(homedir(), '.zcode', 'auto-resume');
  const launch = options.launch ?? (() => promisify(execFile)(
    'python3', [join(pluginRoot, 'monitor.py'), 'start'], { timeout: 5000 },
  ));
  let starting;

  async function origin() {
    const port = Number((await readFile(join(stateDir, 'dashboard.port'), 'utf8')).trim());
    if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('监控端口无效');
    return `http://127.0.0.1:${port}`;
  }

  async function request(path, method = 'GET') {
    const base = await origin();
    const response = await fetch(base + path, {
      method,
      headers: method === 'POST' ? { Origin: base, 'X-ZCode-Control': '1' } : {},
      signal: AbortSignal.timeout(70000),
      redirect: 'error',
    });
    if (!response.ok) throw new Error(`监控服务返回 HTTP ${response.status}`);
    const data = await response.json();
    if (typeof data.monitoring !== 'boolean') throw new Error('监控服务响应格式不正确');
    return data;
  }

  async function status() {
    try {
      return await request('/api/status');
    } catch (originalError) {
      if (!starting) {
        starting = (async () => {
          await launch();
          for (let attempt = 0; attempt < 10; attempt++) {
            await delay(150);
            try { return await request('/api/status'); } catch { /* daemon starting */ }
          }
          throw new Error(`无法连接监控服务：${originalError.message}`);
        })().finally(() => { starting = undefined; });
      }
      return starting;
    }
  }

  return {
    status,
    async setMonitoring(enabled) {
      await status();
      return request(enabled ? '/api/start' : '/api/stop', 'POST');
    },
  };
}
