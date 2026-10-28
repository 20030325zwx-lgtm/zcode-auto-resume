import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { McpServer } from '@modelcontextprotocol/server';
import { StdioServerTransport } from '@modelcontextprotocol/server/stdio';
import { registerAppResource, registerAppTool, RESOURCE_MIME_TYPE } from '@modelcontextprotocol/ext-apps/server';
import { z } from 'zod';
import { createBackend } from './backend.mjs';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const backend = createBackend(root);
const resourceUri = 'ui://zcode-auto-resume/panel.html';
const server = new McpServer({ name: 'zcode-auto-resume', version: '0.2.0' });
const wrap = (action) => async (args) => {
  try {
    const data = await action(args);
    return { content: [{ type: 'text', text: JSON.stringify(data) }], structuredContent: data };
  } catch (error) {
    return { isError: true, content: [{ type: 'text', text: error.message }] };
  }
};

registerAppTool(server, 'get_monitor_status', {
  title: '查看自动续跑',
  description: '打开自动续跑监控面板，查看检测状态和最近巡检记录。不会开启检测。',
  inputSchema: z.object({}),
  annotations: { readOnlyHint: true, openWorldHint: false },
  _meta: { ui: { resourceUri, surface: 'monitor', visibility: ['model', 'app'] } },
}, wrap(() => backend.status()));

registerAppTool(server, 'set_monitoring', {
  title: '切换自动续跑检测',
  description: '响应面板上的开始或停止按钮。所有会话面板共享同一个检测开关。',
  inputSchema: z.object({ enabled: z.boolean() }),
  annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true },
  _meta: { ui: { visibility: ['app'] } },
}, wrap(({ enabled }) => backend.setMonitoring(enabled)));

registerAppResource(server, 'monitor-panel', resourceUri, {}, async () => ({
  contents: [{ uri: resourceUri, mimeType: RESOURCE_MIME_TYPE,
    text: await readFile(join(root, 'dist/panel.html'), 'utf8') }],
}));

await server.connect(new StdioServerTransport());
