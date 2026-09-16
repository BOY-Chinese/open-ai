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
const net = require('net');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { randomUUID } = require('crypto');

// IPC 常量 (Broker 托管模式; 详细协议见 ../ipc.py 头注释)
const PIPE_NAME = '\\\\.\\pipe\\open-ai.broker';

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
  // 每次请求都从 config.json 实时读取账号启用状态，确保 GUI 切换立即生效
  try {
    const cfg = JSON.parse(fs.readFileSync(OPENAI_CFG_PATH, 'utf-8'));
    const t = (cfg.providers && cfg.providers.trae) || {};
    const fresh = (t.accounts && t.accounts.length > 0) ? t.accounts : [];
    // 合并内存中的 invalid 状态（由健康检查运行时设置）
    for (const a of ACCOUNTS) {
      const match = fresh.find(f => f.uid === a.uid || f.token === a.token);
      if (match) match.invalid = !!a.invalid;
    }
    ACCOUNTS = fresh.length > 0 ? fresh : ACCOUNTS;
  } catch (e) {
    // 读取失败则沿用内存 ACCOUNTS
  }
  const valid = ACCOUNTS.filter(a => !a.invalid && a.enabled !== false);
  if (valid.length === 0) {
    throw new Error('TRAE 账号池为空或全部已失效, 请在 open-ai 桌面端「账号管理」页重新连接或添加账号');
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

// ======================= 动态模型 (启动/每日刷新, 失败回退配置表) =======================
// 调用上游 get_detail_param 实时拉取 Trae 模型列表, 缓存到内存。
// 每天至多刷新一次(默认), 拉取失败回退 config.json 的 providers.trae.models。
const MODEL_REFRESH_INTERVAL = (CFG.modelRefreshInterval || 86400) * 1000; // 秒→ms
let DYNAMIC_MODELS = [];        // 最近一次拉到的上游 config_name 数组
let DYNAMIC_LAST_OK = 0;        // 上次成功拉取时间戳(ms)
let dynamicRefreshing = false;
let DYNAMIC_INIT = null;        // 首次拉取的 promise (供 /v1/models 等待)

function hasDynamicModels() {
  return DYNAMIC_MODELS.length > 0;
}

async function fetchUpstreamModels() {
  // 用任一账号通过 ahaNet 调 get_detail_param, 返回 config_name 数组; 失败抛错。
  const valid = ACCOUNTS.filter(a => !a.invalid);
  if (valid.length === 0) throw new Error('TRAE 账号池为空, 无法拉取模型列表');
  const acc = valid[0];
  const body = {
    function: CFG.function || 'chat_v3',
    config_names: null,
    need_prompt: false,
    current_config_info: null,
    poly_prompt: true,
    mode_type: null,
    agent_type: null,
  };
  const resp = await ahaNet.fetch('https://trae-api-cn.mchost.guru/api/ide/v1/get_detail_param', {
    method: 'POST',
    headers: {
      ...BASE_HDRS,
      // 与 llm_utils_chat 同口径: 三头同 token + X-Uid (缺 X-Uid 时部分账号拿不到全量模型)
      'authorization': `Cloud-IDE-JWT ${acc.token}`,
      'x-cloudide-token': acc.token,
      'x-ide-token': acc.token,
      'x-uid': acc.uid || '',
      'accept': 'application/json',
      'content-type': 'application/json',
      'referer': 'https://trae-api-cn.mchost.guru/',
    },
    body: JSON.stringify(body),
  });
  if (resp.status !== 200) {
    throw new Error(`get_detail_param HTTP ${resp.status}`);
  }
  const data = await resp.json().catch(() => ({}));
  const list = (data && data.config_info_list) || [];
  const names = list
    .map(c => c && c.config_name)
    .filter(n => typeof n === 'string' && n.length > 0);
  if (names.length === 0) throw new Error('get_detail_param 返回空模型列表');
  return names;
}

// 拉取并更新缓存; 返回是否成功。force=true 忽略每日间隔强制刷新。
async function refreshModels(force) {
  if (dynamicRefreshing) return DYNAMIC_MODELS.length > 0;
  const now = Date.now();
  if (!force && DYNAMIC_LAST_OK && (now - DYNAMIC_LAST_OK) < MODEL_REFRESH_INTERVAL) {
    return DYNAMIC_MODELS.length > 0; // 未到期, 用缓存
  }
  dynamicRefreshing = true;
  try {
    const names = await fetchUpstreamModels();
    DYNAMIC_MODELS = names;
    DYNAMIC_LAST_OK = now;
    console.log(`[动态模型] 更新成功: ${names.length} 个 (${names.slice(0, 8).join(', ')}${names.length > 8 ? '...' : ''})`);
    return true;
  } catch (e) {
    console.error(`[动态模型] 拉取失败: ${e.message} (回退配置表, 保留上次缓存)`);
    return DYNAMIC_MODELS.length > 0;
  } finally {
    dynamicRefreshing = false;
  }
}

// ======================= 模型映射 =======================
// 上游 v3 协议 (2026-09 实测): 模型名带模式后缀 "__dev"/"__max" 时, 必须拆开传:
//   config_name = 基础配置名 (如 glm-5.3-flash)          ← get_detail_param 返回的名字
//   model_name  = 完整变体名 (如 glm-5.3-flash__dev)
// 整串塞进 config_name 会被上游拒之门外: "the param is invalid"。
// (J/K 双向实测: model_name=__max 可用; 整串 config_name=__max/__dev 一律 4001)
function resolveModelNames(model) {
  if (!model) model = CFG.default_model || '';
  // 剥通道前缀: 网关(normalize_model)通常已剥, 这里兜底处理直连 18787 的调用
  // (custom-local: 旧客户端格式 / tr- 规范前缀 / trae- 旧前缀, 循环剥)
  let low = String(model).toLowerCase();
  while (low.startsWith('custom-local:') || low.startsWith('tr-') || low.startsWith('trae-')) {
    model = model.slice(low.startsWith('custom-local:') ? 13 : (low.startsWith('trae-') ? 5 : 3));
    low = String(model).toLowerCase();
  }
  if (!model) model = CFG.default_model || '';
  // 动态模型表优先(上游 config_name)
  if (DYNAMIC_MODELS.includes(model)) return { configName: model, modelName: model };
  // config.json 显式别名 (值可以是基础名或 __dev/__max 变体名)
  if (CFG.models && CFG.models[model]) model = CFG.models[model];
  // 拆模式后缀: "__dev" / "__max" → config_name + model_name
  const m = model.match(/^(.+)__(dev|max)$/);
  if (m) return { configName: m[1], modelName: model };
  return { configName: model, modelName: model };
}

// 兼容旧调用点: 只取完整模型名 (变体名)
function resolveModel(model) {
  return resolveModelNames(model).modelName;
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
// 上游 (2026-09) 起「不带模式变体的基础名」一律拒绝:
//   config_name=glm-5.3-flash 直呼 → event:error "the model is unknown"
//   必须显式给 model_name=<config>__dev (或 __max), 缺变体即失败。
// 因此: 用户没指定变体时补默认 __dev; 万一该配置没有 dev 变体, 首字节前自动
// 回退成「不传 model_name」再试一次 (老协议形态), 保证两种情况都能出字。
const DEFAULT_VARIANT = '__dev';
const RETRYABLE_MODEL_ERR = /model is unknown|param is invalid/i;

function hasModelVariant(name) {
  return /__(dev|max)$/i.test(String(name || ''));
}

// traeChat: 变体兜底 + 一次性回退
async function traeChat(body, handlers) {
  const { configName, modelName } = resolveModelNames(body.model);
  const userGaveVariant = hasModelVariant(modelName);
  const firstModelName = userGaveVariant ? modelName : `${modelName}${DEFAULT_VARIANT}`;

  // 首字节前先不把 error/done 透给调用方 (可能是可回退的模型名错误)
  let emitted = false;
  let deferredErr = null;
  let deferredDone = null;
  // 排队状态: 记录是否排过队 + 最后一次位次 (供流结束时的 queue_timeout 判定)
  let queueSeen = false;
  let lastPosition = null;
  const buffered = {
    onOutput: (o) => { emitted = true; handlers.onOutput(o); },
    onUsage: (u) => handlers.onUsage && handlers.onUsage(u),
    onDone: (fr) => { deferredDone = fr; },
    onError: (msg) => { deferredErr = msg; },
    // 排队事件只透传「位次」, 不传 queue_id 等内部标识; 由 HTTP 层决定是否提示
    onQueue: (q) => {
      queueSeen = true;
      if (q && q.phase === 'wait' && Number.isFinite(q.position)) lastPosition = q.position;
      if (handlers.onQueue) handlers.onQueue(q);
    },
    signal: handlers.signal,
  };

  const r1 = await traeChatOnce(body, configName, firstModelName, buffered);
  const errText = String(r1.error || deferredErr || '');
  if (!emitted && !userGaveVariant && RETRYABLE_MODEL_ERR.test(errText)) {
    console.warn(`[trae-warn] ${configName}${DEFAULT_VARIANT} 被上游拒绝(${errText.slice(0, 60)}), 回退为不带 model_name 重试`);
    return await traeChatOnce(body, configName, null, handlers);
  }
  // 排队空转: 全程只排队、一个 token 都没出 → 明确标 queue_timeout, 不留「无信息空回复」
  if (queueSeen && !emitted && !deferredErr) {
    console.warn(`[trae-warn] ${configName} 排队期结束但无输出 (最后位次=${lastPosition}), 标记 queue_timeout`);
    if (handlers.onDone) handlers.onDone('queue_timeout');
    return { error: null, queueTimeout: true, lastPosition };
  }
  // 无回退: 把缓冲的 error/done 按原语义补报给调用方
  if (deferredErr) handlers.onError && handlers.onError(deferredErr);
  if (deferredDone) handlers.onDone && handlers.onDone(deferredDone);
  return r1;
}

async function traeChatOnce(body, configName, modelName, { onOutput, onUsage, onDone, onError, onQueue, signal }) {
  const chatFunction = CFG.function || 'chat_v3';
  const reqBody = {
    function: chatFunction,
    config_name: configName,
    messages: convertMessages(body.messages),
  };
  // 变体名必须独立成字段; 基础名直传 config_name 会被上游判为 unknown model
  if (modelName) reqBody.model_name = modelName;
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

  // 上游偶发 TTNet/Cronet 网络错误 (code=3/-21 网络切换, code=8/-101 连接重置)
  // 与 429/5xx 都是瞬时的: 重试并轮换账号, 避免把抖动当成「模型不可用」抛给客户端。
  // 只在首字节之前重试 (fetch 阶段), 不会重复已产生的输出。
  // 每次尝试都挂到合并信号上: 内部 ac (看门狗) + 调用方 signal (客户端断开),
  // 任一触发都中止上游 —— AbortSignal.any 需要 Node 20+, 本项目 runtime 满足。
  const ac = new AbortController();
  const upstreamSignal = signal ? AbortSignal.any([signal, ac.signal]) : ac.signal;
  const MAX_ATTEMPTS = 3;
  let resp = null;
  let lastFetchErr = null;
  for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
    const acc = pickAccount().acc;
    try {
      resp = await ahaNet.fetch('https://trae-api-cn.mchost.guru/api/agent/v3/llm_utils_chat', {
        method: 'POST',
        headers: {
          ...BASE_HDRS,
          // 上游 v3 要求三头同 token (Cloud-IDE-JWT / X-Cloudide-Token / X-Ide-Token)
          // 以及账号 uid (X-Uid); 缺失时部分模型会报 param invalid / 鉴权失败
          'authorization': `Cloud-IDE-JWT ${acc.token}`,
          'x-cloudide-token': acc.token,
          'x-ide-token': acc.token,
          'x-uid': acc.uid || '',
          'content-type': 'application/json',
          'x-request-id': 'req_' + randomUUID(),
          'x-trae-request-id': randomUUID(),
          'referer': 'https://trae-api-cn.mchost.guru/api/agent/v3/llm_utils_chat',
        },
        body: JSON.stringify(reqBody),
        signal: upstreamSignal,
      });
    } catch (e) {
      lastFetchErr = e;
      resp = null;
      // 看门狗已触发: 不再重试, 直接把失败抛给上层
      if (watchdogTripped) break;
      if (process.env.TRAE_PROXY_DEBUG) {
        console.warn(`[trae-warn] 第 ${attempt + 1}/${MAX_ATTEMPTS} 次请求网络异常: ${e && e.message}`);
      }
    }
    if (resp && (resp.status === 429 || resp.status >= 500)) {
      lastFetchErr = new Error(`HTTP ${resp.status}`);
      resp = null;
    }
    if (resp) break;
    if (attempt < MAX_ATTEMPTS - 1) {
      await new Promise(r => setTimeout(r, 600 * (attempt + 1)));   // 退避后再试(换账号)
    }
  }
  if (!resp) {
    if (watchdogTripped) {
      throw new Error(`Trae 上游${watchdogTripped} (连接已中止)`);
    }
    const msg = (lastFetchErr && lastFetchErr.message) || '上游不可达';
    console.error(`[trae-err] 重试 ${MAX_ATTEMPTS} 次仍失败: ${msg}`);
    throw new Error(`Trae API 请求失败: ${msg}`);
  }

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

  // 无数据看门狗: 防上游/TTNet 挂死导致的「无限等待」。
  // 注意与心跳的区别 —— 心跳是发给客户端的保活(会重置网关的读超时),
  // 看门狗只看「上游是否给过字节」: 建连后 60s 内零字节, 或读流中途 120s
  // 无新字节, 就 abort 上游连接并抛错, 让网关/客户端立刻收到失败,
  // 而不是被自己的心跳喂成无限等待。(排队场景不受影响: 上游每 ~1.16s
  // 就有 request_wait_in_queue 事件, 计时器不断被喂, 不会误杀)
  let watchdogTripped = null;
  const armWatchdog = (ms, label) => {
    return setTimeout(() => {
      if (watchdogTripped) return;
      watchdogTripped = `${label} ${Math.round(ms / 1000)}s 无上游数据`;
      console.error(`[trae-err] 看门狗触发: ${watchdogTripped} → abort 上游连接`);
      try { ac.abort(); } catch { /* ignore */ }
    }, ms);
  };
  let wd = armWatchdog(60000, '首字节');
  const FEED = () => {
    clearTimeout(wd);
    wd = armWatchdog(120000, '读流中断');
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      FEED();
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
        } else if (currentEvent === 'queue_begin') {
          // 排队开始: 此时只有 queue_id, 还拿不到位次 → 不对外发提示
          try {
            const q = JSON.parse(data);
            if (onQueue) onQueue({ phase: 'begin', queueId: q.queue_id || '', requestUuid: q.request_uuid || '' });
          } catch { /* ignore */ }
        } else if (currentEvent === 'request_wait_in_queue') {
          // 排队等待: 带实时位次 position (实测上游每 ~1.16s 推一条, 位次递但不匀速)
          try {
            const q = JSON.parse(data);
            // 注意 Number(null) === 0 且 isFinite 为真, 必须排除 null/空串
            const raw = q.position;
            const pos = (raw === null || raw === undefined || raw === '') ? NaN : Number(raw);
            if (onQueue && Number.isFinite(pos) && pos > 0) {
              onQueue({
                phase: 'wait',
                position: pos,
                message: q.message || '',
                queueId: q.queue_id || '',
                requestUuid: q.request_uuid || '',
              });
            }
          } catch { /* ignore */ }
        } else if (currentEvent === 'progress_notice') {
          // 保活事件 ("Processing_<ts>" / ";Processing"): 仅用于心跳, 不产生内容
        }
        currentEvent = null;
      }
    }
  }
  } catch (e) {
    // 看门狗 abort / 上游读流异常: 明确抛错, 让客户端立刻失败而非无限等待
    if (watchdogTripped) {
      lastError = `Trae 上游${watchdogTripped} (连接已中止)`;
    } else if (e && e.name === 'AbortError') {
      lastError = 'Trae 上游连接被中止 (AbortError)';
    } else {
      throw e;
    }
  } finally {
    clearTimeout(wd);
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
    // 优先返回动态拉取的上游模型, 失败/未拉取时回退 config.json 表
    // 关键: 首次拉取尚未完成时, 等待其结束 (否则会把 config 静态兜底表当成真实列表返回,
    //       网关同步后就只剩十几个模型, 且要等 24h 才修正 —— glm/kimi/qwen 会凭空消失)
    if (!hasDynamicModels()) {
      if (!DYNAMIC_INIT) DYNAMIC_INIT = refreshModels(true).catch(() => {});
      await DYNAMIC_INIT;
    }
    const makeEntry = (id) => ({ id, object: 'model', created: 0, owned_by: 'trae-proxy' });
    let data;
    if (hasDynamicModels()) {
      data = DYNAMIC_MODELS.map(makeEntry);
    } else {
      const fromCfg = Object.entries(CFG.models || {}).map(([id]) => makeEntry(id));
      // 补上 default_model 与反向别名, 确保 /v1/models 完整
      const seen = new Set(fromCfg.map(e => e.id));
      for (const [alias, name] of Object.entries(CFG.models || {})) {
        if (!seen.has(name)) { fromCfg.push(makeEntry(name)); seen.add(name); }
      }
      if (CFG.default_model && !seen.has(CFG.default_model)) {
        fromCfg.push(makeEntry(CFG.default_model));
      }
      data = fromCfg;
    }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ object: 'list', data }));
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
      // 客户端断开信号: 传给 traeChat → 与看门狗合并中止上游 (下面 res.on('close') 触发)
      const clientAbort = new AbortController();

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

        // 排队提示: 只报位次, 禁止百分比/ETA —— 实测位次推进不匀速
        // (112→105 用了 85s, 之后卡住 115s 没动), 算 ETA 会骗人, 不如只报第 N 位。
        // 位次**每次变化**都更新 (首条之后仍在 token 之前): 数字在变小 = 链路活着,
        // 只是上游人多 —— 这正是提示层的初衷: 用户能看出「不是网关挂了」。
        // 这些 content 块同时喂饱网关 httpx 与 DSH idleWatchdog 的空闲超时,
        // 排队十几分钟也不会被任何一层判死。
        let lastQueuePos = null;
        let sawContent = false;
        const onQueueHint = ({ phase, position }) => {
          if (sawContent || phase !== 'wait') return;
          if (position === lastQueuePos) return;      // 位次没变就不刷屏
          lastQueuePos = position;
          send(openaiChunk(model, id, created,
               { content: `⌛ 排队中，当前第 ${position} 位…\n` }, null));
        };

        // 防御性保活: 每 15s 一条 SSE 注释行 (以 ':' 开头, OpenAI 客户端会忽略)。
        // 不是为了修现有静默 (Trae 排队期本就每 ~1.16s 有事件), 而是覆盖
        // 「非排队类的首 token 前长静默」, 防上游改推流节奏后静默超时被掐断。
        const hb = setInterval(() => {
          if (finished) return;
          try { res.write(': keep-alive\n\n'); } catch { /* ignore */ }
        }, 15000);

        // 用户断开即中止上游: 客户端等不及关掉后, 不让上游队列里还挂着一个
        // 空占位次的请求 (既浪费我们的额度, 又推高后面所有人的位次)。
        // 通过 clientAbort 信号传给 traeChatOnce, 与看门狗合并监听。
        res.on('close', () => {
          if (!finished) {
            console.log(`[trae] 客户端已断开, 中止上游请求 (排队位次=${lastQueuePos ?? '无'})`);
            try { clientAbort.abort(); } catch { /* ignore */ }
          }
        });

        try {
          await traeChat(body, {
            onOutput: ({ response, reasoning_content, tool_calls }) => {
              if (response || reasoning_content ||
                  (tool_calls && tool_calls.length > 0)) sawContent = true;
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
            onQueue: onQueueHint,
            onDone: (fr) => endStream(fr),
            onError: () => endStream('stop'),
            signal: clientAbort.signal,
          });
          if (merger.calls.size > 0) endStream('tool_calls');
          else endStream('stop');
        } catch (e) {
          endStream('stop');
        } finally {
          clearInterval(hb);
        }
      } else {
        let full = '';
        let reasoning = '';
        let usage = null;
        let finishReason = 'stop';
        let traeError = null;
        let lastQueuePos = null;
        const merger = new ToolCallStreamMerger();

        // 非流式同样: 客户端断开就中止上游, 不空占队列位次
        res.on('close', () => {
          console.log(`[trae] 客户端已断开(非流式), 中止上游请求 (排队位次=${lastQueuePos ?? '无'})`);
          try { clientAbort.abort(); } catch { /* ignore */ }
        });

        try {
          await traeChat(body, {
            onOutput: ({ response, reasoning_content, tool_calls }) => {
              if (response) full += response;
              if (reasoning_content) reasoning += reasoning_content;
              if (tool_calls && tool_calls.length > 0) merger.feed(tool_calls);
            },
            onUsage: (u) => { usage = { prompt_tokens: u.prompt_tokens, completion_tokens: u.completion_tokens, total_tokens: u.total_tokens }; },
            onQueue: ({ phase, position }) => {
              if (phase === 'wait' && Number.isFinite(position) && position > 0) {
                lastQueuePos = position;
              }
            },
            onDone: (fr) => { finishReason = fr || 'stop'; },
            onError: (msg) => { traeError = msg; },
            signal: clientAbort.signal,
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
        // 排队空转: 非流式也能看出「排过队且一个字没出」, 给出可读说明而不只是空回复
        if (!traeError && !full && !reasoning && toolCalls.length === 0 &&
            lastQueuePos !== null) {
          message.content = `⌛ 排队中，当前第 ${lastQueuePos} 位…（本次未等到模型返回，请稍后重试）`;
          finishReason = 'queue_timeout';
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

  // ============ 账号管理接口 (供 open-ai 桌面端「账号管理」页显示 [已失效] / 重新连接) ============
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

// ======================= Broker 托管模式: IPC 心跳 + 优雅退出 =======================
// 由 Broker (app_runtime.py, 进程名 open-ai-daemon.exe) 启动时通过
// OPEN_AI_FROM_BROKER=1 启用: 连接 \\.\pipe\open-ai.broker, 10s 一跳,
// 收到 shutdown 指令后停止接受新连接并退出。管道不可用时静默独立运行。
const HEARTBEAT_MS = 10000;
let hbConn = null;
let hbStopped = false;

function pipeSend(obj) {
  if (!hbConn) return false;
  try { hbConn.write(Buffer.from(obj)); return true; }
  catch (_) { try { hbConn.destroy(); } catch (_) {} hbConn = null; return false; }
}

function pipeRecv() {
  if (!hbConn) return null;
  if (hbConn._rbuf === undefined) hbConn._rbuf = Buffer.alloc(0);
  if (hbConn._rbuf.length < 4) return null;
  const len = hbConn._rbuf.readUInt32LE(0);
  if (len > 65536) { try { hbConn.destroy(); } catch (_) {} hbConn = null; return null; }
  if (hbConn._rbuf.length < 4 + len) return null;
  const payload = hbConn._rbuf.slice(4, 4 + len);
  hbConn._rbuf = hbConn._rbuf.slice(4 + len);
  try { return JSON.parse(payload.toString('utf-8')); } catch (_) { return null; }
}

function connectBroker() {
  // Windows 命名管道: 直接传字符串路径 (不能用 { pipe: ... } 选项形式)
  hbConn = net.connect(PIPE_NAME, () => {
    pipeSend(frame({ type: 'hello', role: 'trae', pid: process.pid, port: PORT }));
  });
  hbConn.on('data', (chunk) => {
    if (hbConn && hbConn._rbuf !== undefined) {
      hbConn._rbuf = Buffer.concat([hbConn._rbuf, chunk]);
    }
    for (;;) {
      const msg = pipeRecv();
      if (!msg) break;
      if (msg.type === 'shutdown') {
        console.log('[ipc] 收到 Broker 关闭指令:', msg.reason || '');
        gracefulExit('broker-shutdown');
      } else if (msg.type === 'welcome') {
        console.log('[ipc] 已向 Broker 注册 (trae)');
      }
    }
  });
  hbConn.on('error', () => { try { hbConn.destroy(); } catch (_) {} hbConn = null; });
  hbConn.on('close', () => { hbConn = null; });
}

function frame(obj) {
  const payload = Buffer.from(JSON.stringify(obj), 'utf-8');
  const head = Buffer.alloc(4);
  head.writeUInt32LE(payload.length, 0);
  return Buffer.concat([head, payload]);
}

function gracefulExit(reason) {
  hbStopped = true;
  try { server.close(() => process.exit(0)); } catch (_) { process.exit(0); }
  // 兜底: server.close 等待在途连接, 5s 强退
  setTimeout(() => process.exit(0), 5000).unref();
}

server.listen(PORT, HOST, () => {
  console.log(`Trae DeepSeek Proxy API (open-ai 内嵌) 已启动: http://${HOST}:${PORT}`);
  console.log(`  POST /v1/chat/completions  (OpenAI 兼容, 流式 + tools/function calling)`);
  console.log(`  GET  /v1/models`);
  console.log(`默认模型: ${CFG.default_model}`);
  if (ACCOUNTS.length === 0) {
    console.warn('[警告] 账号池为空! 请先在 open-ai 桌面端「账号管理」页添加 TRAE 账号');
  }
  setTimeout(() => healthCheck('启动检查'), 1000);
  setInterval(() => healthCheck('每小时定时检查'), CHECK_INTERVAL);
  // 动态模型: 启动时强制拉取一次, 之后每 MODEL_REFRESH_INTERVAL(默认1天) 刷新
  if (!DYNAMIC_INIT) DYNAMIC_INIT = refreshModels(true).catch(() => {});
  setInterval(() => { refreshModels(false).catch(() => {}); }, MODEL_REFRESH_INTERVAL);
  console.log(`[动态模型] 每日自动刷新已启用 (每 ${MODEL_REFRESH_INTERVAL / 1000 / 3600} 小时)`);
  // ---- Broker 托管 ----
  if (process.env.OPEN_AI_FROM_BROKER === '1') {
    setInterval(() => {
      try {
        if (hbStopped) return;
        if (!hbConn) connectBroker();
        else pipeSend(frame({ type: 'heartbeat', role: 'trae', pid: process.pid, ts: Date.now() / 1000 }));
      } catch (e) {
        console.error('[ipc] 心跳异常 (不影响服务):', e.message);
        try { if (hbConn) hbConn.destroy(); } catch (_) {}
        hbConn = null;
      }
    }, HEARTBEAT_MS);
    console.log('[ipc] Broker 托管模式已启用 (心跳 %ds)', HEARTBEAT_MS / 1000);
  }
});
