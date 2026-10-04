/* MediaAuto Service Worker (PWA)
 * 策略: 网络优先(network-first), 失败回退缓存 → 离线可用 + 更新即时生效。
 *   - 实时状态类接口(/api/qbit/*) **永不缓存**: 见下方 NEVER_CACHE。
 *   - 其它 API(/api/*) 走网络优先, 但缓存失败/离线时回退(仅 GET; POST/写操作不缓存)。
 *   - 静态资源(css/js/html) 网络优先 + 缓存回退, 保证改完即发、断网可用。
 * 版本递增 → 升级时清旧缓存, 避免陈旧资源。
 */
const VERSION = 'mediaauto-v9';   // 2026-10-04: 详情页「改标题」的繁/港台译名(alt_titles)按需回填 —— 后端 _detail_payload 打开详情时若 alt_titles 为空则用轻量 /translations 拉一次补上(存量老片该列 100% 空); 升版本清旧 static 缓存, 让新 detail.js 生效; 2026-10-04: 强刷前端缓存 —— 「改标题」加 TMDB 变体快速选(繁/港台/英/原)等前端改动; 2026-10-03: 修「管理」页签图标(9 个 <line> 误写重复 y1); 2026-10-02: 热门榜平台地区语义修正 + 运行时缓存条数上限; 2026-09-21: 不再缓存 qbit 实时数据
const STATIC_CACHE = VERSION + '-static';
const RUNTIME_CACHE = VERSION + '-runtime';
// 启动即预缓存的核心壳(离线也能打开登录页)
const PRECACHE = ['/', '/css/app.css', '/manifest.webmanifest'];

// RUNTIME_CACHE 条数上限: 每个不同的 /api GET 都会被存一份(分页、搜索词都是新 key),
// 不设上限的话缓存会随使用时间无界膨胀(磁盘 + 缓存查找都变慢)。
// Cache API 不暴露"年龄", keys() 实测按插入序 → 从头(最旧)删到只剩上限。
const RUNTIME_MAX = 200;

async function trimRuntimeCache() {
  try {
    const c = await caches.open(RUNTIME_CACHE);
    const keys = await c.keys();
    if (keys.length <= RUNTIME_MAX) return;
    for (const k of keys.slice(0, keys.length - RUNTIME_MAX)) {
      await c.delete(k);
    }
  } catch (_) { /* 缓存不可用(隐私模式等)时忽略 */ }
}

// 实时状态类接口: 永不缓存(既不读也不写)。
// 缓存它们会在断网/瞬时报错时把"上一次的进度"当成当前值显示出来 —— 对下载进度这是误导。
// 宁可让前端拿到网络错误(它会显示错误横幅 + 自动重试), 也不要给一个看起来正常实则过期的数字。
const NEVER_CACHE = ['/api/qbit/'];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(STATIC_CACHE).then((c) => c.addAll(PRECACHE))
      .catch(() => {})   // 预缓存失败不阻塞激活
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => !k.startsWith(VERSION)).map((k) => caches.delete(k)))
    ).then(() => trimRuntimeCache())
    .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  // 只处理 GET(写操作/推送一律走网络, 不缓存不拦截)
  if(req.method !== 'GET') return;
  const url = new URL(req.url);
  // 只接管同源请求
  if(url.origin !== self.location.origin) return;

  const isAPI = url.pathname.startsWith('/api/');
  const neverCache = NEVER_CACHE.some((p) => url.pathname.startsWith(p));

  e.respondWith(
    (async () => {
      // 实时状态类接口: 直连, 不读也不写缓存(拿到什么就返回什么, 包括错误)
      if(neverCache) return fetch(req);

      if(isAPI) {
        try {
          const res = await fetch(req);
          if(res.ok) {
            const c = await caches.open(RUNTIME_CACHE);
            c.put(req, res.clone());
          }
          return res;
        } catch {
          const hit = await caches.match(req);
          if(hit) return hit;
          return new Response(JSON.stringify({detail:'offline'}), {status:503, headers:{'Content-Type':'application/json'}});
        }
      }
      // 静态资源: 网络优先, 失败回退缓存
      try {
        const res = await fetch(req);
        if(res.ok) {
          const c = await caches.open(STATIC_CACHE);
          c.put(req, res.clone());
        }
        return res;
      } catch {
        const hit = await caches.match(req) || await caches.match(req.url.replace(/^(.*\/)([^\/]+)$/, '$1'));
        if(hit) return hit;
        // 离线且无缓存: HTML 回退到预缓存首页
        const home = await caches.match('/');
        if(home && (req.mode === 'navigate')) return home;
        return new Response('offline', {status:503});
      }
    })()
  );
});
