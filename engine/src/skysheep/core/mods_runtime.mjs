// SkySheep Mods Node.js 运行时（bootstrap）
//
// 由 mods_node.py 通过 `node <this-file>` 启动，stdin/stdout 收发 JSON-RPC：
//   Python -> Node: {"id":N,"method":"load|call|shutdown|ping","params":{...}}
//   Node -> Python: {"id":N,"result":{...}} 或 {"id":N,"error":{"message":"..."}}
//
// Mod 入口通过全局 `register(event, [opts], handler)` 注册中间件；
// handler 形如 `($, e, next) => any`：
//   $    mods API（state/clock/fs/http/process/model/mcp/session/...）
//   e    { event: string, payload: object }（可读写，改 payload 会影响下游）
//   next 调用下一个 handler；undefined 表示链尾
//   返回 undefined/null = 继续；其他 = 该事件的 verdict（最后一个非空返回值胜出）
//
// 无沙箱：Mod 在 Node.js 进程中运行，可 import 任何 Node 模块或 npm 包。

import { pathToFileURL } from 'node:url';
import { createInterface } from 'node:readline';
import { stdin, stdout } from 'node:process';
import { promises as fsp } from 'node:fs';
import { spawn as cpSpawn, exec as cpExec } from 'node:child_process';
import { promisify } from 'node:util';

const execAsync = promisify(cpExec);

// ---------- 模块状态 ----------
const handlers = new Map(); // event -> [{ handler, priority, name }]
let modState = {};
let sessionId = '';
let modId = '';
let modDir = '';

// ---------- 通信 ----------
function send(msg) {
  try {
    stdout.write(JSON.stringify(msg) + '\n');
  } catch (err) {
    // 通讯失败没有任何补救手段，只能记录
    console.error('[mods] 写出 stdout 失败:', err);
  }
}

// ---------- 反向调用：Node -> Python ----------
// Node 需要引擎能力（模型补全 / MCP 调用 / 工具注册 / 持久存储）时向 Python
// 发起请求；Python 处理后以 {type:'host_result', id, result|error} 回应。
// 超时统一 HOST_CALL_TIMEOUT_MS：超过就当失败，避免 handler 永远挂住。
const HOST_CALL_TIMEOUT_MS = 60_000;
let hostSeq = 0;
const hostPending = new Map();

function hostCall(method, params) {
  return new Promise((resolve, reject) => {
    const id = ++hostSeq;
    const timer = setTimeout(() => {
      if (hostPending.has(id)) {
        hostPending.delete(id);
        reject(new Error(`hostCall ${method} 超时（>${HOST_CALL_TIMEOUT_MS / 1000}s）`));
      }
    }, HOST_CALL_TIMEOUT_MS);
    hostPending.set(id, { resolve, reject, timer });
    send({ type: 'host', id, method, params: params || {} });
  });
}

function resolveHost(msg) {
  const id = msg.id;
  const entry = hostPending.get(id);
  if (!entry) return;
  hostPending.delete(id);
  clearTimeout(entry.timer);
  if (msg.error) entry.reject(new Error(String(msg.error)));
  else entry.resolve(msg.result);
}

globalThis.__sky_host_call__ = hostCall;

// ---------- 已注册的工具 handler（本地，不跨 JSON） ----------
const toolHandlers = new Map();  // name -> handler fn

// ---------- $ API ----------
// 说明：Mod 跑在 Node.js 进程内、无沙箱——fs/http/process 直接绑 Node 原生实现；
// 需要引擎能力的方法（model/mcp/store/settings/session.history）在 P1 阶段抛明确
// 错误，P4 再接入 JSON-RPC 桥（届时由 runtime 注入 hostCall 通道）。
const $ = Object.freeze({
  mod: Object.freeze({
    id: () => modId,
    dir: () => modDir,
  }),

  state: Object.freeze({
    get(key) { return modState[key]; },
    set(key, value) { modState[key] = value; },
    delete(key) { delete modState[key]; },
    all() { return { ...modState }; },
  }),

  clock: Object.freeze({
    now: () => Date.now(),
    sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
  }),

  env: Object.freeze({ ...process.env }),

  fs: Object.freeze({
    read: (p, enc = 'utf-8') => fsp.readFile(p, enc),
    write: (p, d) => fsp.writeFile(p, d, 'utf-8'),
    append: (p, d) => fsp.appendFile(p, d, 'utf-8'),
    list: (p) => fsp.readdir(p),
    stat: (p) => fsp.stat(p),
    remove: (p) => fsp.rm(p, { recursive: true, force: true }),
    mkdir: (p, opts = {}) => fsp.mkdir(p, { recursive: true, ...opts }),
    exists: async (p) => { try { await fsp.access(p); return true; } catch { return false; } },
  }),

  http: Object.freeze({
    fetch: (url, opts) => fetch(url, opts),
  }),

  process: Object.freeze({
    exec: (cmd, opts = {}) => execAsync(cmd, { encoding: 'utf-8', ...opts }),
    spawn: (cmd, args = [], opts = {}) => cpSpawn(cmd, args, { stdio: 'pipe', ...opts }),
  }),

  session: Object.freeze({
    id: () => sessionId,
  }),

  store: Object.freeze({
    // 持久键值（跨会话共享，落 mods/<id>/store.json，由 Python 侧原子写）
    get: (key) => hostCall('store.get', { key: String(key) }),
    set: (key, value) => hostCall('store.set', { key: String(key), value }),
    all: () => hostCall('store.all', {}),
    delete: (key) => hostCall('store.delete', { key: String(key) }),
  }),

  settings: Object.freeze({
    // 只读用户配置的子集（provider/model 等），不回传 api_key
    get: (key) => hostCall('settings.get', { key: String(key) }),
  }),

  model: Object.freeze({
    // 用当前会话的 provider 发起一次非流式补全；返回 {text, usage?}
    complete: (opts) => hostCall('model.complete', opts || {}),
    // 隔离上下文的一次补全（不复用会话历史）
    fork: (opts) => hostCall('model.fork', opts || {}),
  }),

  mcp: Object.freeze({
    list: () => hostCall('mcp.list', {}),
    call: (server, tool, args) => hostCall('mcp.call', {
      server: String(server), tool: String(tool), args: args || {},
    }),
  }),

  tool: Object.freeze({
    // 注册一个工具。spec: {name, description, schema, safety?}
    // handler: async (args, ctx) => string | {text} | any
    //   —— handler 留在 Node 进程内（函数不能过 JSON），Python 侧
    //   只在「模型调用这个工具」时通过 tool.invoke 反向回调。
    register: (spec, handler) => {
      const s = spec || {};
      const name = String(s.name || '').trim();
      if (!name) throw new Error('$.tool.register 需要 name');
      if (typeof handler !== 'function') {
        throw new Error(`$.tool.register('${name}', ...) 需要 handler 函数`);
      }
      toolHandlers.set(name, handler);
      return hostCall('tool.register', {
        name,
        description: String(s.description || ''),
        schema: s.schema || {},
        safety: String(s.safety || 'readonly'),
      });
    },
  }),

  command: Object.freeze({
    register: (spec) => hostCall('command.register', spec || {}),
  }),
});

globalThis.$ = $;

// ---------- register ----------
globalThis.register = function register(event, optsOrHandler, maybeHandler) {
  let opts = {};
  let handler;
  if (typeof optsOrHandler === 'function') {
    handler = optsOrHandler;
  } else {
    opts = optsOrHandler || {};
    handler = maybeHandler;
  }
  if (typeof event !== 'string' || !event) {
    throw new Error('register 需要事件名（字符串）');
  }
  if (typeof handler !== 'function') {
    throw new Error(`register('${event}', ...) 的 handler 必须是函数`);
  }
  if (!handlers.has(event)) handlers.set(event, []);
  const list = handlers.get(event);
  const entry = {
    handler,
    priority: Number(opts.priority) || 0,
    name: String(opts.name || ''),
  };
  if (entry.name) {
    const existing = list.findIndex((h) => h.name === entry.name);
    if (existing >= 0) list[existing] = entry;
    else list.push(entry);
  } else {
    list.push(entry);
  }
  list.sort((a, b) => b.priority - a.priority);
};

// ---------- 中间件链 ----------
async function runChain(event, payload) {
  const list = handlers.get(event);
  if (!list || list.length === 0) {
    return { ok: true, verdict: null };
  }
  const e = { event, payload: payload || {} };
  let idx = 0;
  let verdict = null;

  async function callNext() {
    if (idx >= list.length) return null;
    const h = list[idx++];
    const r = await h.handler($, e, callNext);
    if (r !== undefined && r !== null) verdict = r;
    return r;
  }

  try {
    await callNext();
    return { ok: true, verdict, payload: e.payload };
  } catch (err) {
    return { ok: false, error: String((err && err.message) || err) };
  }
}

// ---------- JSON-RPC ----------
const rl = createInterface({ input: stdin, crlfDelay: Infinity });

rl.on('line', async (line) => {
  const text = line.trim();
  if (!text) return;
  let msg;
  try {
    msg = JSON.parse(text);
  } catch (err) {
    console.error('[mods] 无法解析 JSON-RPC:', err.message);
    return;
  }
  // 反向调用结果：resolve 挂起的 hostCall
  if (msg && msg.type === 'host_result') {
    resolveHost(msg);
    return;
  }

  const { id, method, params } = msg || {};

  if (method === 'ping') {
    send({ id, result: { ok: true, pid: process.pid } });
    return;
  }

  if (method === 'load') {
    try {
      const p = params || {};
      modId = String(p.modId || '');
      modDir = String(p.modDir || '');
      sessionId = String(p.sessionId || '');
      const entryPath = String(p.entry || '');
      if (!entryPath) throw new Error('load 需要 entry');
      await import(pathToFileURL(entryPath).href);
      const registered = [...handlers.keys()];
      send({ id, result: { ok: true, hooks: registered, pid: process.pid } });
    } catch (err) {
      send({ id, error: { message: String((err && err.stack) || err) } });
    }
    return;
  }

  // Python 请求调用一个已注册的 Mod 工具（模型通过 ToolRegistry 调到）
  if (method === 'invoke_tool') {
    try {
      const p = params || {};
      const name = String(p.name || '');
      const handler = toolHandlers.get(name);
      if (!handler) throw new Error('未注册的工具：' + name);
      const ctx = { sessionId: sessionId, modId: modId };
      const r = await handler(p.args || {}, ctx);
      let text;
      if (typeof r === 'string') text = r;
      else if (r && typeof r === 'object' && typeof r.text === 'string') text = r.text;
      else text = JSON.stringify(r);
      send({ id, result: { ok: true, text } });
    } catch (err) {
      send({ id, error: { message: String((err && err.message) || err) } });
    }
    return;
  }

  if (method === 'call') {
    try {
      const p = params || {};
      modState = p.state || {};
      sessionId = String(p.sessionId || '');
      const result = await runChain(String(p.event || ''), p.payload || {});
      send({ id, result: { ...result, state: modState } });
    } catch (err) {
      send({ id, error: { message: String((err && err.stack) || err) } });
    }
    return;
  }

  if (method === 'shutdown') {
    try { send({ id, result: { ok: true } }); } catch { /* 忽略 */ }
    setTimeout(() => process.exit(0), 20);
    return;
  }

  send({ id, error: { message: `未知方法: ${method}` } });
});

process.on('uncaughtException', (err) => {
  console.error('[mods] uncaughtException:', err);
});
process.on('unhandledRejection', (reason) => {
  console.error('[mods] unhandledRejection:', reason);
});
