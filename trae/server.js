#!/usr/bin/env node
/**
 * Trae DeepSeek 后端 (open-ai 内嵌) — OpenAI 兼容接口 (v2: 支持 function calling)
 * 复用 Trae 内置 aha 网络栈(TTNet)完成请求加密/签名, 调用 llm_utils_chat(function=chat_v3)
 *
 * 这是 open-ai 独立项目的一部分, 配置读取 ../config.json 的 providers.trae 段,
 * 不再依赖任何外部目录。由 start.bat / start_hidden.ps1 统一拉起。
 *
 * 端点:
 *   POST /v1/chat/completions   聊天补全 (支持流式 stream=true、tools/function calling)
 *   GET  /v1/models             模型列表
 */
'use strict';

const http = require('http');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { randomUUID } = require('crypto');

// ======================= 配置加载 (open-ai/config.json -> providers.trae) =======================
const OPENAI_CFG_PATH = path.join(__dirname, '..', 'config.json');
const openaiCfg = JSON.parse(fs.readFileSync(OPENAI_CFG_PATH, 'utf-8'));
const CFG = (openaiCfg.providers && openaiCfg.providers.trae) || {};

// ======================= 网络栈加载: 优先自带 lib, 回退本机 TRAE =======================
// 自带模式: open-ai/trae/lib/ 目录 (sscronet.dll + @aha-kit/net), 无需本机安装 TRAE
// trae_dir 模式: 从本机 TRAE SOLO CN 安装目录读取 (兼容旧配置)
const SELF_LIB = path.join(__dirname, 'lib');
const hasSelfLib = fs.existsSync(path.join(SELF_LIB, 'sscronet.dll'))
  && fs.existsSync(path.join(SELF_LIB, '@aha-kit', 'net', 'wrapper.js'))
  && fs.existsSync(path.join(SELF_LIB, '@aha-kit', 'net-win32-x64-msvc', 'index.win32-x64-msvc.node'));

const LIB_BASE = hasSelfLib ? SELF_LIB : (CFG.trae_dir || null);
if (!LIB_BASE) {
  console.error('[ERROR] 未找到网络栈依赖!');
  console.error('        方案A(推荐): 使用 open-ai/trae/lib/ 自带依赖 (sscronet.dll + @aha-kit/net)');
  console.error('        方案B: 安装 TRAE SOLO CN 并在 config.json providers.trae.trae_dir 填写安装目录');
  process.exit(1);
}
process.chdir(LIB_BASE); // sscronet.dll 依赖同目录 DLL

const ahaNet = require(path.join(LIB_BASE, '@aha-kit', 'net', 'wrapper.js'));

// ======================= 初始化网络栈 =======================
// ttnet_params 来自 config.json providers.trae.ttnet_params (与桌面原始稳定版一致)
// 提供合理默认值兜底, 避免 native 库因缺失必填字段而崩溃
const DEVICE_ID_CFG = CFG.device_id || CFG.headers?.['x-device-id'] || '';
const DEFAULT_TTNET_PARAMS = {
  domainHttpdns: 'x',
  domainNetlog: 'x',
  appId: '6eefa01c-1036-4c7e-9ca5-d891f63bfcd8',
  deviceId: DEVICE_ID_CFG,
  tncHostFirst: 'tnc3-bjlgy.zijieapi.com',
};
ahaNet.init({
  ttnetLibPath: path.join(LIB_BASE, 'sscronet.dll'),
  ttnetParams: CFG.ttnet_params || DEFAULT_TTNET_PARAMS,
  params: { storagePath: path.join(os.tmpdir(), 'trae-net-storage') },
});

const BASE_HDRS = { ...CFG.headers, 'accept': '*/*' };
const DEVICE_ID = CFG.device_id || CFG.headers?.['x-device-id'] || '';

// ======================= 多账号轮询 =======================
let ACCOUNTS = (CFG.accounts && CFG.accounts.length > 0)
  ? CFG.accounts
  : (CFG.headers?.['x-ide-token'] ? [{ name: 'default', token: CFG.headers['x-ide-token'] }] : []);
const SWITCH_EVERY = CFG.switchEvery || 10;
let globalCallCount = 0;

function pickAccount() {
  const valid = ACCOUNTS.filter(a => !a.invalid);
  if (valid.length === 0) {
    throw new Error('TRAE 账号池为空或全部已失效, 请在 账号管理.bat 中 [4] 重新连接 或添加账号');
  }
  const idx = Math.floor(globalCallCount / SWITCH_EVERY) % valid.length;
  globalCallCount++;
  const acc = valid[idx];
  return { idx, acc };
}

// ======================= 账号健康检查 (启动 + 每小时) =======================
const CREDITS_URL = 'https://api.trae.cn/trae/api/v2/pay/ide_user_ent_usage';
const GET_TOKEN_URL = 'https://api.trae.cn/cloudide/api/v3/common/GetUserToken';
const CHECK_INTERVAL = 3600 * 1000;
const HEALTH_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0';
let healthCheckRunning = false;

async function verifyAccount(acc) {
  try {
    const resp = await fetch(CREDITS_URL, {
      method: 'POST',
      headers: {
        'Authorization': `Cloud-IDE-JWT ${acc.token}`,
        'x-device-id': DEVICE_ID,
        'Content-Type': 'application/json',
        'User-Agent': 'TRAE SOLO CN/2.3.70844',
      },
      body: JSON.stringify({ require_usage: true, req_source: 1 }),
    });
    return resp.status === 200;
  } catch (e) {
    return false;
  }
}

async function renewByCookie(acc) {
  if (!acc.cookie) return null;
  try {
    const resp = await fetch(GET_TOKEN_URL, {
      method: 'POST',
      headers: {
        'Cookie': acc.cookie,
        'User-Agent': HEALTH_UA,
        'Content-Type': 'application/json',
        'Origin': 'https://www.trae.cn',
        'Referer': 'https://www.trae.cn/',
      },
      body: '{}',
    });
    const j = await resp.json().catch(() => ({}));
    const tok = j && j.Result && j.Result.Token;
    if (!tok) return null;
    const ok = await verifyAccount({ token: tok });
    return ok ? tok : null;
  } catch (e) {
    return null;
  }
}

// 从 config.json 重新加载账号池 (外部 login_trae.py 新增的账号无需重启即可纳入)
function reloadAccountsFromConfig() {
  try {
    const cfg = JSON.parse(fs.readFileSync(OPENAI_CFG_PATH, 'utf-8'));
    const t = (cfg.providers && cfg.providers.trae) || {};
    const list = (t.accounts && t.accounts.length > 0)
      ? t.accounts
      : (t.headers?.['x-ide-token'] ? [{ name: 'default', token: t.headers['x-ide-token'] }] : []);
    ACCOUNTS = list;
    console.log(`[健康检查] 已从 config.json 加载账号池: ${ACCOUNTS.length} 个账号`);
  } catch (e) {
    console.error('[健康检查] 重新加载 config.json 失败, 沿用内存账号池:', e.message);
  }
}

// 只把续期成功的 token 同步回 config.json; 绝不删除任何账号 (健壮性修复)
function persistAccounts(updatedTokens) {
  try {
    const cfg = JSON.parse(fs.readFileSync(OPENAI_CFG_PATH, 'utf-8'));
    const t = (cfg.providers && cfg.providers.trae) || {};
    const list = (t.accounts || []).slice();
    for (const c of list) {
      if (updatedTokens && updatedTokens[c.uid]) {
        c.token = updatedTokens[c.uid];
      }
    }
    t.accounts = list;
    cfg.providers.trae = t;
    fs.writeFileSync(OPENAI_CFG_PATH, JSON.stringify(cfg, null, 2), 'utf-8');
    console.log(`[健康检查] 已同步续期 token 到 config.json (${Object.keys(updatedTokens || {}).length} 个)`);
  } catch (e) {
    console.error('[健康检查] config.json 写回失败:', e.message);
  }
}

async function healthCheck(reason) {
  if (healthCheckRunning) return;
  healthCheckRunning = true;
  try {
    reloadAccountsFromConfig();
    console.log(`[健康检查] ${reason} — 当前 ${ACCOUNTS.length} 个账号, 逐账号验证...`);
    const updatedTokens = {};
    for (const acc of ACCOUNTS) {
      let ok = await verifyAccount(acc);
      let note = '有效';
      if (!ok) {
        const newTok = await renewByCookie(acc);
        if (newTok) {
          acc.token = newTok;
          updatedTokens[acc.uid] = newTok;
          ok = true;
          note = 'token 失效 → 已用 cookie 续期复活';
          acc.invalid = false;
        } else {
          // 关键健壮性修复: 验证失败只标记[已失效], 绝不删除账号 / 不写回清空 config
          note = 'token 失效(标记[已失效], 保留账号)';
          acc.invalid = true;
        }
      } else {
        acc.invalid = false;
      }
      console.log(`  ${ok ? '✓' : '✗'} ${acc.name || acc.uid} (uid=${acc.uid}) — ${note}`);
    }
    const invalidCount = ACCOUNTS.filter(a => a.invalid).length;
    if (Object.keys(updatedTokens).length > 0) {
      persistAccounts(updatedTokens); // 只同步续期后的 token, 不删除任何账号
    }
    console.log(`[健康检查] ${ACCOUNTS.length - invalidCount} 个有效, ${invalidCount} 个已失效(保留, 界面显示[已失效])`);
  } finally {
    healthCheckRunning = false;
  }
}

// ======================= 模型映射 =======================
function resolveModel(model) {
  if (!model) return CFG.default_model;
  return CFG.models[model] || model;
}

// ======================= 消息转换 (OpenAI -> Trae) =======================
function convertMessages(messages) {
  return (messages || []).map(m => {
    let content = m.content;
    if (typeof content === 'string') {
      content = [{ type: 'text', text: content }];
    } else if (Array.isArray(content)) {
      content = content
        .filter(c => c && typeof c === 'object')
        .map(c => ({ type: c.type || 'text', text: c.text || '' }));
    }
    const msg = { role: m.role || 'user', content };
    if (Array.isArray(m.tool_calls) && m.tool_calls.length > 0) {
      msg.tool_calls = m.tool_calls.map((tc, i) => {
        const fn = tc.function || {};
        let args = fn.arguments;
        if (typeof args !== 'string') args = JSON.stringify(args ?? {});
        return {
          index: i,
          id: tc.id || '',
          type: tc.type || 'function',
          function_call: { name: fn.name || '', arguments: args },
        };
      });
    }
    if (m.tool_call_id) msg.tool_call_id = m.tool_call_id;
    return msg;
  });
}

// ======================= tools 转换 (OpenAI -> Trae) =======================
function convertTools(tools) {
  if (!Array.isArray(tools) || tools.length === 0) return undefined;
  return tools.map(t => {
    const fn = t.function || t;
    const out = {
      type: 'function',
      function: {
        name: fn.name,
        description: fn.description || '',
      },
    };
    if (fn.parameters !== undefined) {
      out.function.parameters = typeof fn.parameters === 'string' ? fn.parameters : JSON.stringify(fn.parameters);
    }
    return out;
  });
}

function convertToolChoice(toolChoice, hasTools) {
  if (!hasTools) return undefined;
  return 'required';
}

// ======================= Work 积分通道 (会话上下文) =======================
const hex = (n) => [...Array(n)].map(() => '0123456789abcdef'.split('')[Math.floor(Math.random() * 16)]).join('');

function extractUserInput(messages) {
  for (let i = (messages || []).length - 1; i >= 0; i--) {
    const m = messages[i];
    if (!m || m.role !== 'user') continue;
    const c = m.content;
    if (typeof c === 'string') return c;
    if (Array.isArray(c)) {
      const texts = c.filter(x => x && x.type === 'text' && x.text).map(x => x.text);
      if (texts.length) return texts.join('\n');
    }
  }
  return '';
}

// ======================= Trae 聊天调用 =======================
async function traeChat(body, { onOutput, onUsage, onDone, onError, signal }) {
  const configName = resolveModel(body.model);
  const chatFunction = CFG.function || 'chat_v3';
  const reqBody = {
    function: chatFunction,
    config_name: configName,
    messages: convertMessages(body.messages),
  };
  if (CFG.use_work_credits !== false) {
    reqBody.session_id = '6a' + hex(21);
    reqBody.conversation_id = '6a' + hex(21);
    reqBody.user_input = extractUserInput(body.messages);
    reqBody.access_type = 1;
    reqBody.metadata = { is_remote_req: false };
    reqBody.request_seq = 1;
  }
  const tools = convertTools(body.tools);
  if (tools) reqBody.tools = tools;
  const toolChoice = convertToolChoice(body.tool_choice, !!tools);
  if (toolChoice) reqBody.tool_choice = toolChoice;

  const { idx: accIdx, acc } = pickAccount();

  if (process.env.TRAE_PROXY_DEBUG) {
    console.log(`[req] account[${accIdx}]=${acc.name} model=${configName} messages=${body.messages?.length} tools=${tools?.length ?? 0} tool_choice=${toolChoice || body.tool_choice || 'auto'}`);
  }

  const resp = await ahaNet.fetch('https://api5-normal.mchost.guru/api/agent/v3/llm_utils_chat', {
    method: 'POST',
    headers: {
      ...BASE_HDRS,
      'x-ide-token': acc.token,
      'content-type': 'application/json',
      'x-request-id': 'req_' + randomUUID(),
      'x-trae-request-id': randomUUID(),
      'referer': 'https://trae-api-cn.mchost.guru/api/agent/v3/llm_utils_chat',
    },
    body: JSON.stringify(reqBody),
    signal,
  });

  if (resp.status !== 200) {
    const errText = await resp.text();
    console.error(`[trae-err] HTTP ${resp.status}: ${errText.slice(0, 300)}`);
    throw new Error(`Trae API ${resp.status}: ${errText.slice(0, 500)}`);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  let currentEvent = null;
  let lastError = null;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let nlIdx;
    while ((nlIdx = buffer.indexOf('\n')) !== -1) {
      const line = buffer.slice(0, nlIdx).replace(/\r$/, '');
      buffer = buffer.slice(nlIdx + 1);

      if (line.startsWith('event:')) {
        currentEvent = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        const data = line.slice(5).trim();
        if (currentEvent === 'output') {
          try {
            const obj = JSON.parse(data);
            onOutput({
              response: obj.response || '',
              reasoning_content: obj.reasoning_content || null,
              tool_calls: obj.tool_calls || null,
            });
          } catch { /* ignore */ }
        } else if (currentEvent === 'token_usage') {
          try {
            const u = JSON.parse(data);
            onUsage(u);
          } catch { /* ignore */ }
        } else if (currentEvent === 'done') {
          try {
            const d = JSON.parse(data);
            if (d.finish_reason) onDone(d.finish_reason);
          } catch { /* ignore */ }
        } else if (currentEvent === 'error') {
          try {
            const e = JSON.parse(data);
            lastError = e.message || JSON.stringify(e);
            console.error('[trae-err] event:error:', lastError.slice(0, 300));
            onError(lastError);
          } catch { /* ignore */ }
        }
        currentEvent = null;
      }
    }
  }
  return { error: lastError };
}

// ======================= OpenAI SSE 格式 =======================
function openaiChunk(model, id, created, delta, finishReason) {
  return JSON.stringify({
    id, object: 'chat.completion.chunk', created, model,
    choices: [{ index: 0, delta, finish_reason: finishReason ?? null }],
  });
}

class ToolCallStreamMerger {
  constructor() {
    this.calls = new Map();
  }
  feed(traeCalls) {
    const deltas = [];
    for (const tc of traeCalls) {
      const idx = tc.index ?? 0;
      const fc = tc.function_call || tc.function || {};
      const state = this.calls.get(idx) || { id: null, type: 'function', name: '', args: '' };
      this.calls.set(idx, state);

      const delta = { index: idx };
      if (tc.id && !state.id) {
        state.id = tc.id;
        delta.id = tc.id;
        delta.type = tc.type || 'function';
      }
      const fnDelta = {};
      if (fc.name && !state.name) {
        state.name = fc.name;
        fnDelta.name = fc.name;
      }
      if (fc.arguments) {
        state.args += fc.arguments;
        fnDelta.arguments = fc.arguments;
      }
      if (Object.keys(fnDelta).length > 0) delta.function = fnDelta;
      if (Object.keys(delta).length > 1) deltas.push(delta);
    }
    return deltas;
  }
  getFinal() {
    const out = [];
    for (const [idx, s] of [...this.calls.entries()].sort((a, b) => a[0] - b[0])) {
      out.push({
        id: s.id || ('call_' + idx),
        type: s.type || 'function',
        function: { name: s.name, arguments: s.args },
      });
    }
    return out;
  }
}

// ======================= HTTP 服务 =======================
const server = http.createServer(async (req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
  if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return; }

  const url = new URL(req.url, `http://${req.headers.host}`);

  if (req.method === 'GET' && url.pathname === '/v1/models') {
    const models = Object.entries(CFG.models || {}).map(([id, name]) => ({
      id, object: 'model', created: 0, owned_by: 'trae-proxy',
    }));
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ object: 'list', data: models }));
    return;
  }

  if (req.method === 'POST' && url.pathname === '/v1/chat/completions') {
    let raw = '';
    req.on('data', c => { raw += c; if (raw.length > 64 * 1024 * 1024) req.destroy(); });
    req.on('end', async () => {
      let body;
      try { body = JSON.parse(raw); }
      catch { res.writeHead(400, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ error: { message: 'Invalid JSON body' } })); return; }

      const model = resolveModel(body.model);
      const stream = body.stream !== false;
      const id = 'chatcmpl-' + randomUUID().replace(/-/g, '').slice(0, 24);
      const created = Math.floor(Date.now() / 1000);
      const send = (chunk) => res.write('data: ' + chunk + '\n\n');

      if (stream) {
        res.writeHead(200, {
          'Content-Type': 'text/event-stream; charset=utf-8',
          'Cache-Control': 'no-cache',
          'Connection': 'keep-alive',
          'X-Accel-Buffering': 'no',
        });

        send(openaiChunk(model, id, created, { role: 'assistant', content: '' }, null));

        const merger = new ToolCallStreamMerger();
        let finished = false;
        const endStream = (fr) => {
          if (finished) return;
          finished = true;
          send(openaiChunk(model, id, created, {}, fr || 'stop'));
          res.write('data: [DONE]\n\n');
          res.end();
        };

        try {
          await traeChat(body, {
            onOutput: ({ response, reasoning_content, tool_calls }) => {
              if (tool_calls && tool_calls.length > 0) {
                const deltas = merger.feed(tool_calls);
                for (const d of deltas) {
                  send(openaiChunk(model, id, created, { tool_calls: [d] }, null));
                }
              }
              if (response) send(openaiChunk(model, id, created, { content: response }, null));
              if (reasoning_content) send(openaiChunk(model, id, created, { reasoning_content }, null));
            },
            onUsage: () => {},
            onDone: (fr) => endStream(fr),
            onError: () => endStream('stop'),
          });
          if (merger.calls.size > 0) endStream('tool_calls');
          else endStream('stop');
        } catch (e) {
          endStream('stop');
        }
      } else {
        let full = '';
        let reasoning = '';
        let usage = null;
        let finishReason = 'stop';
        let traeError = null;
        const merger = new ToolCallStreamMerger();

        try {
          await traeChat(body, {
            onOutput: ({ response, reasoning_content, tool_calls }) => {
              if (response) full += response;
              if (reasoning_content) reasoning += reasoning_content;
              if (tool_calls && tool_calls.length > 0) merger.feed(tool_calls);
            },
            onUsage: (u) => { usage = { prompt_tokens: u.prompt_tokens, completion_tokens: u.completion_tokens, total_tokens: u.total_tokens }; },
            onDone: (fr) => { finishReason = fr || 'stop'; },
            onError: (msg) => { traeError = msg; },
          });
        } catch (e) {
          traeError = String(e.message || e);
        }

        const toolCalls = merger.getFinal();
        const message = { role: 'assistant', content: full || null, reasoning_content: reasoning || null };
        if (toolCalls.length > 0) {
          message.tool_calls = toolCalls;
          if (!full) message.content = null;
          finishReason = 'tool_calls';
        }
        const payload = {
          id, object: 'chat.completion', created, model,
          choices: [{ index: 0, message, finish_reason: traeError ? 'error' : finishReason }],
          usage: usage || { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
        };
        if (traeError) payload.error = traeError;
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(payload));
      }
    });
    return;
  }

  if (req.method === 'GET' && url.pathname === '/health') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ status: 'ok', time: new Date().toISOString() }));
    return;
  }

  // ============ 账号管理接口 (供 账号管理.bat 显示 [已失效] 与 [4] 重新连接) ============
  if (req.method === 'GET' && url.pathname === '/v1/admin/accounts') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      accounts: ACCOUNTS.map(a => ({ name: a.name, uid: a.uid, invalid: !!a.invalid })),
    }));
    return;
  }

  if (req.method === 'POST' && url.pathname === '/v1/admin/reconnect') {
    await healthCheck('用户手动重新连接');
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      accounts: ACCOUNTS.map(a => ({ name: a.name, uid: a.uid, invalid: !!a.invalid })),
    }));
    return;
  }

  res.writeHead(404, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ error: { message: 'Not Found' } }));
});

const PORT = CFG.port || 18787;
const HOST = CFG.listen || '127.0.0.1';
server.listen(PORT, HOST, () => {
  console.log(`Trae DeepSeek Proxy API (open-ai 内嵌) 已启动: http://${HOST}:${PORT}`);
  console.log(`  POST /v1/chat/completions  (OpenAI 兼容, 流式 + tools/function calling)`);
  console.log(`  GET  /v1/models`);
  console.log(`默认模型: ${CFG.default_model}`);
  if (ACCOUNTS.length === 0) {
    console.warn('[警告] 账号池为空! 请先运行 账号管理.bat -> [2] 添加 TRAE 账号');
  }
  setTimeout(() => healthCheck('启动检查'), 1000);
  setInterval(() => healthCheck('每小时定时检查'), CHECK_INTERVAL);
});
