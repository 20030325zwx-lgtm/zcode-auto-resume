const $ = (id) => document.getElementById(id);
const labels = { normal:'正常', running:'运行中', recent:'最近活跃', index_completed:'索引已完成', waiting:'等待', skipped:'跳过', ready:'待续跑', sent:'已确认送达', failed:'发送失败', handled:'已处理', cooldown:'冷却中', limit:'达上限' };
const time = (value) => value ? new Date(value * 1000).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}) : '尚未检查';
let latest;
let changing = false;
let pending = false;
let app;

function showError(message) {
  $('alert').textContent = message || '';
  $('alert').hidden = !message;
}

function row(title, detail, stamp, badge, failed = false) {
  const result = document.createElement('div'); result.className = 'row';
  const head = document.createElement('div'); head.className = 'row-head';
  const text = document.createElement('span'); text.className = 'row-title'; text.textContent = title;
  head.append(text);
  if (badge) { const tag = document.createElement('span'); tag.className = `badge${failed ? ' failed' : ''}`; tag.textContent = badge; head.append(tag); }
  result.append(head);
  if (detail) { const p = document.createElement('p'); p.textContent = detail; result.append(p); }
  if (stamp) { const date = document.createElement('time'); date.textContent = time(stamp); result.append(date); }
  return result;
}

function list(id, items, makeRow, empty) {
  const root = $(id); root.replaceChildren();
  if (!items.length) { const p = document.createElement('p'); p.className = 'empty'; p.textContent = empty; root.append(p); }
  else items.forEach((item) => root.append(makeRow(item)));
}

function render(data) {
  latest = data;
  const delayed = data.monitoring && data.checked_at && Date.now()/1000 - data.checked_at > (data.interval_seconds || 120)*2+30;
  $('status').textContent = data.monitoring ? (delayed ? '巡检已延迟' : '检测中') : '已停止';
  $('dot').className = `dot${data.monitoring ? ' active' : ''}`;
  $('toggle').textContent = data.monitoring ? '停止检测' : '开始检测';
  $('toggle').className = data.monitoring ? '' : 'primary';
  $('toggle').disabled = changing;
  $('checked').textContent = `上次检查 · ${time(data.checked_at)}`;
  $('count').textContent = (data.sessions || []).length;
  $('attempts').textContent = data.attempts_today_total || 0;
  showError(data.error);
  list('events', (data.events || []).slice(0,3), (item) => row(item.title, item.detail, item.at, item.type === 'sent' ? '已确认送达' : '发送失败', item.type !== 'sent'), '还没有续跑记录');
  list('sessions', data.sessions || [], (item) => row(item.title, `${item.reason} · 今日 ${item.attempts_today || 0} 次`, null, labels[item.decision] || item.decision, item.decision === 'failed'), '最近 20 分钟没有需要检查的会话');
  list('checks', (data.checks || []).slice(0,10), (item) => row(`检查 ${item.scanned} 个会话 · 送达 ${item.resumed} 个`, '', item.at), '还没有巡检记录');
}

async function request(action, enabled) {
  if (__MCP_APP__) {
    const result = await app.callServerTool({name:action === 'status' ? 'get_monitor_status' : 'set_monitoring', arguments:action === 'status' ? {} : {enabled}}, {timeout:90000});
    if (result.isError) throw new Error(result.content?.find((part) => part.type === 'text')?.text || '监控操作失败');
    return result.structuredContent;
  }
  const response = await fetch(action === 'status' ? '/api/status' : (enabled ? '/api/start' : '/api/stop'), {
    method:action === 'status' ? 'GET' : 'POST',
    headers:action === 'status' ? {} : {'X-ZCode-Control':'1'}, cache:'no-store',
  });
  if (!response.ok) throw new Error(`监控服务返回 HTTP ${response.status}`);
  return response.json();
}

async function refresh() {
  if (pending || changing) return;
  pending = true;
  try { render(await request('status')); }
  catch (error) { $('status').textContent = '连接失败'; $('dot').className = 'dot'; $('toggle').disabled = true; showError(error.message); }
  finally { pending = false; }
}

$('toggle').addEventListener('click', async () => {
  if (!latest || changing) return;
  changing = true; $('toggle').disabled = true;
  try {
    const data = await request('control', !latest.monitoring);
    render({...latest, monitoring:data.monitoring, error:null});
  } catch (error) { showError(error.message); }
  finally { changing = false; $('toggle').disabled = false; await refresh(); }
});
$('refresh').addEventListener('click', refresh);

async function init() {
  if (__MCP_APP__) {
    const { App } = await import('@modelcontextprotocol/ext-apps');
    app = new App({name:'zcode-auto-resume', version:'0.2.0'}, {});
    app.onhostcontextchanged = (context) => {
      if (context.theme) document.documentElement.dataset.theme = context.theme;
    };
    app.ontoolresult = (result) => {
      if (result.structuredContent?.sessions && !changing) render(result.structuredContent);
    };
    await app.connect();
    document.documentElement.dataset.theme = app.getHostContext()?.theme || 'light';
  }
  await refresh();
  setInterval(refresh, 5000);
}
init().catch((error) => { $('status').textContent = '连接失败'; showError(error.message); });
