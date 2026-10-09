/* SkySheep PWA service worker：只缓存静态壳，绝不碰动态请求。
 *
 * 提供路径是 /sw.js（server/app.py 显式路由，文件本体在 static/ 下）——脚本
 * 在站点根，默认 scope 就是 /，罩得住主页面；若挂在 /static/ 下，scope 只有
 * /static/，主页面 / 不受控，SW 等于白装。
 *
 * 缓存策略：仅 SHELL_URLS 白名单内的 /static/ 静态文件走 cache-first，其余
 * 请求（页面导航、/ws、/health、/preview、非 GET……）一律不 respondWith，
 * 浏览器直接走网络——动态请求永不入缓存。WebSocket 握手本就不经过 SW 的
 * fetch 事件，与「网络直连」天然一致。
 *
 * 失效方式（双保险）：改了任何前端文件，正常流程递增 SW_VERSION（缓存名带
 * 版本号，activate 时旧缓存整体删除）；即便漏递增，fetch 走 stale-while-
 * revalidate——先回缓存、后台拉新写回，下一次打开就能拿到新壳，不会无限期
 * 吃旧文件。
 */
"use strict";

const SW_VERSION = "skysheep-shell-v13";
const CACHE_NAME = "skysheep-shell-" + SW_VERSION;

// 缓存白名单：手写静态壳 + vendor 第三方库，一个不多。全部是确定的文件路径，
// 不含任何查询串 / 动态段；vendor 库（mermaid 3.3MB 等）前端是按需 loadLib
// 注入的，预缓存后手机端首次渲染图表 / 开终端就不再等局域网下载。
const SHELL_URLS = [
  "/static/index.html",
  "/static/app.css",
  "/static/app.js",
  "/static/app-tools.js",
  "/static/app-projects.js",
  "/static/app-schedule.js",
  "/static/app-providers.js",
  "/static/app-whitelist.js",
  "/static/app-skills.js",
  "/static/app-memmap.js",
  "/static/vendor/xterm.css",
  "/static/vendor/xterm.js",
  "/static/vendor/xterm-fit.js",
  "/static/vendor/highlight.min.js",
  "/static/vendor/qrcode.min.js",
  "/static/vendor/mermaid.min.js"
];

self.addEventListener("install", function (event) {
  event.waitUntil((async function () {
    const cache = await caches.open(CACHE_NAME);
    // 逐个 add 且失败吞掉：旧安装包缺某个文件时不让整个 install 失败；
    // cache:"reload" 绕过 HTTP 缓存直取网络，预缓存不吃浏览器缓存的旧副本。
    await Promise.all(SHELL_URLS.map(function (u) {
      return cache.add(new Request(u, { cache: "reload" })).catch(function () {});
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", function (event) {
  event.waitUntil((async function () {
    const keys = await caches.keys();
    await Promise.all(keys
      .filter(function (k) { return k.indexOf("skysheep-shell-") === 0 && k !== CACHE_NAME; })
      .map(function (k) { return caches.delete(k); }));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", function (event) {
  const req = event.request;
  // 非 GET（含一切写操作）与跨域请求一律放行
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  // 页面导航 network-only：主界面永远取服务端最新首帧（首帧外观注入在 HTML 里）
  if (req.mode === "navigate") return;
  // 白名单之外（/ws、/health、/preview、/manifest…）一律放行，绝不缓存动态请求
  if (SHELL_URLS.indexOf(url.pathname) === -1) return;
  // 白名单文件带查询串的请求直接放行不缓存：缓存键按完整 URL（含查询串），
  // 一旦有调用方把令牌拼进静态文件 URL，敏感参数就会被持久化进磁盘缓存——
  // 「白名单缓存键永不含参数」由机制保证，不靠调用方约定
  if (url.search) return;
  event.respondWith((async function () {
    const cache = await caches.open(CACHE_NAME);
    const cached = await cache.match(req);
    if (cached) {
      // stale-while-revalidate：先回缓存，后台拉新写回——SW_VERSION 漏递增
      // 时下一轮打开也能拿到新壳，不再无限期吃旧文件
      fetch(req).then(function (resp) {
        if (resp && resp.ok) cache.put(req, resp.clone());
      }).catch(function () {});
      return cached;
    }
    try {
      const resp = await fetch(req);
      if (resp && resp.ok) {
        cache.put(req, resp.clone());
      }
      return resp;
    } catch (e) {
      // 白名单静态文件离线且未缓存：明确 504，不静默给空成功
      return new Response("", { status: 504, statusText: "offline" });
    }
  })());
});
