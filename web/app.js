const token = document.querySelector('meta[name="deepcodex-token"]').content;
const $ = (selector) => document.querySelector(selector);
const state = { seq: 0, connected: false, busy: false, assistantNode: null, pendingApproval: null };

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', 'X-DeepCodex-Token': token, ...(options.headers || {}) },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
  return data;
}

function setStatus(label, mode = '') {
  const pill = $('#statusPill');
  pill.className = `status-pill ${mode}`;
  pill.querySelector('b').textContent = label;
}

function syncControls() {
  $('#promptInput').disabled = !state.connected || state.busy;
  $('#sendButton').disabled = !state.connected || state.busy;
  $('#stopButton').disabled = !state.busy;
  $('#connectButton').disabled = state.busy;
  if (state.busy) setStatus('正在工作', 'busy');
  else if (state.connected) setStatus('已连接', 'connected');
  else setStatus('尚未连接');
}

function toast(message) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.remove('hidden');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.add('hidden'), 3200);
}

function showKeyModal() {
  $('#keyError').textContent = '';
  $('#keyInput').value = '';
  $('#keyModal').classList.remove('hidden');
  setTimeout(() => $('#keyInput').focus(), 50);
}

function addMessage(role, text = '', error = false) {
  $('#emptyState').classList.add('hidden');
  const wrapper = document.createElement('article');
  wrapper.className = `message ${role}${error ? ' error' : ''}`;
  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = role === 'assistant' ? 'DS' : role === 'user' ? '你' : '!';
  const body = document.createElement('div');
  body.className = 'message-body';
  const name = document.createElement('b');
  name.textContent = role === 'assistant' ? 'DeepSeek' : role === 'user' ? '你' : '系统';
  const content = document.createElement('div');
  content.className = 'content';
  content.textContent = text;
  body.append(name, content);
  wrapper.append(avatar, body);
  $('#messages').append(wrapper);
  $('#chatPanel').scrollTop = $('#chatPanel').scrollHeight;
  return content;
}

function logActivity(line) {
  const node = $('#activityContent');
  node.textContent += `\n${line}`;
  $('#activityPanel').scrollTop = $('#activityPanel').scrollHeight;
}

function handleNotification(method, params) {
  if (method === 'item/agentMessage/delta') {
    if (!state.assistantNode) state.assistantNode = addMessage('assistant');
    state.assistantNode.textContent += params.delta || '';
    $('#chatPanel').scrollTop = $('#chatPanel').scrollHeight;
  } else if (method === 'turn/diff/updated') {
    $('#diffContent').textContent = params.diff || '本轮还没有文件变更。';
    $('#diffDot').classList.toggle('visible', Boolean(params.diff));
  } else if (method === 'turn/completed') {
    state.busy = false;
    state.assistantNode = null;
    syncControls();
    logActivity(`turn completed: ${params.turn?.status || 'completed'}`);
  } else if (method === 'item/started') {
    const item = params.item || {};
    logActivity(`${item.type || 'item'} started${item.command ? `: ${item.command}` : ''}`);
  } else if (method === 'error') {
    const detail = params.error?.message || JSON.stringify(params.error || params);
    if (params.willRetry) logActivity(`连接波动，Codex 正在自动重试：${detail}`);
    else addMessage('system', detail, true);
  } else if (['warning', 'configWarning', 'deprecationNotice'].includes(method)) {
    logActivity(`${method}: ${JSON.stringify(params)}`);
  }
}

function showApproval(event) {
  state.pendingApproval = event;
  const isCommand = event.method === 'item/commandExecution/requestApproval';
  $('#approvalTitle').textContent = isCommand ? '允许执行这条命令？' : '允许这次文件修改？';
  $('#approvalReason').textContent = event.params.reason || '该操作超出了当前自动权限范围。';
  $('#approvalDetail').textContent = isCommand
    ? `${event.params.cwd || ''}\n\n${event.params.command || '（未提供命令文本）'}`
    : '请先在“本轮 Diff”标签页查看当前修改。';
  const available = Array.isArray(event.params.availableDecisions)
    ? event.params.availableDecisions.filter((decision) => typeof decision === 'string')
    : [];
  $('#approveSessionButton').classList.toggle(
    'hidden',
    isCommand && available.length > 0 && !available.includes('acceptForSession'),
  );
  $('#approvalModal').classList.remove('hidden');
}

async function resolveApproval(decision) {
  if (!state.pendingApproval) return;
  const requestId = state.pendingApproval.requestId;
  try {
    await api('/api/approval', { method: 'POST', body: JSON.stringify({ requestId, decision }) });
    $('#approvalModal').classList.add('hidden');
    state.pendingApproval = null;
    logActivity(`approval ${requestId}: ${decision}`);
  } catch (error) { toast(error.message); }
}

function handleEvent(event) {
  if (event.type === 'connected') {
    state.connected = true; state.busy = false;
    $('#workspaceInput').value = event.workspace;
    syncControls();
    logActivity(`thread started: ${event.threadId}`);
    toast('项目已连接，DeepSeek V4 Flash 已就绪');
  } else if (event.type === 'status') {
    setStatus(event.message, 'busy');
  } else if (event.type === 'user_message') {
    addMessage('user', event.text);
    state.assistantNode = addMessage('assistant');
  } else if (event.type === 'turn_started') {
    logActivity(`turn started: ${event.turnId}`);
  } else if (event.type === 'notification') {
    handleNotification(event.method, event.params || {});
  } else if (event.type === 'server_request') {
    showApproval(event);
  } else if (['fatal', 'turn_error', 'closed'].includes(event.type)) {
    addMessage('system', event.message || '未知错误', true);
    state.busy = false;
    if (event.type !== 'turn_error') state.connected = false;
    syncControls();
  } else if (event.type === 'log') {
    logActivity(event.message || '');
  }
}

async function poll() {
  try {
    const data = await api(`/api/events?after=${state.seq}`);
    for (const event of data.events) {
      state.seq = Math.max(state.seq, event.seq);
      handleEvent(event);
    }
  } catch (error) { console.debug(error); }
  setTimeout(poll, 400);
}

document.querySelectorAll('.tab').forEach((button) => {
  button.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((tab) => tab.classList.remove('active'));
    document.querySelectorAll('.panel').forEach((panel) => panel.classList.remove('active'));
    button.classList.add('active');
    $(`#${button.dataset.tab}Panel`).classList.add('active');
  });
});

$('#keyButton').addEventListener('click', showKeyModal);
$('#keyCancel').addEventListener('click', () => $('#keyModal').classList.add('hidden'));
$('#keyForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = $('#keyInput');
  $('#keyError').textContent = '';
  try {
    await api('/api/key', { method: 'POST', body: JSON.stringify({ apiKey: input.value }) });
    input.value = '';
    $('#keyModal').classList.add('hidden');
    toast('API Key 已安全保存');
  } catch (error) { $('#keyError').textContent = error.message; }
});

$('#browseButton').addEventListener('click', async () => {
  try {
    const data = await api('/api/pick-workspace', { method: 'POST', body: JSON.stringify({ initial: $('#workspaceInput').value }) });
    if (data.path) $('#workspaceInput').value = data.path;
  } catch (error) { toast(error.message); }
});

$('#connectButton').addEventListener('click', async () => {
  try {
    state.busy = true; syncControls();
    await api('/api/connect', { method: 'POST', body: JSON.stringify({ workspace: $('#workspaceInput').value }) });
  } catch (error) { state.busy = false; syncControls(); toast(error.message); }
});

async function sendPrompt() {
  const input = $('#promptInput');
  const text = input.value.trim();
  if (!text || state.busy) return;
  input.value = '';
  try {
    state.busy = true; syncControls();
    await api('/api/turn', { method: 'POST', body: JSON.stringify({ text }) });
  } catch (error) { state.busy = false; syncControls(); toast(error.message); }
}

$('#sendButton').addEventListener('click', sendPrompt);
$('#promptInput').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) { event.preventDefault(); sendPrompt(); }
});
$('#stopButton').addEventListener('click', () => api('/api/interrupt', { method: 'POST', body: '{}' }).catch((error) => toast(error.message)));
$('#declineButton').addEventListener('click', () => resolveApproval('decline'));
$('#approveButton').addEventListener('click', () => resolveApproval('accept'));
$('#approveSessionButton').addEventListener('click', () => resolveApproval('acceptForSession'));
$('#exitButton').addEventListener('click', async () => {
  try { await api('/api/shutdown', { method: 'POST', body: '{}' }); } finally {
    document.body.innerHTML = '<main style="display:grid;place-items:center;min-height:100vh;color:#8b949e">DeepCodex 已安全退出，可以关闭此页面。</main>';
  }
});

(async function init() {
  try {
    const current = await api('/api/status');
    state.seq = current.lastSeq || 0;
    state.connected = current.connected;
    state.busy = current.busy;
    $('#workspaceInput').value = current.workspace;
    syncControls();
    if (!current.keyAvailable) showKeyModal();
    poll();
  } catch (error) { toast(error.message); }
})();
