/* cinefold service worker
 *
 * 只为「装得上、离线能开壳」服务，不做数据缓存：
 * 任务进度、日志、订阅状态全是强实时的，缓存 API 响应只会让人看到过期数据，
 * 还得再花力气做失效。所以这里 API 一律不拦截，直接走网络。
 *
 * 缓存的只有 /assets/ 下带内容哈希的构建产物和几个图标。哈希文件名即版本，
 * 改了内容就换名字，所以可以放心 cache-first。
 *
 * 版本号跟着构建走：每次发版换掉 CACHE，activate 时清掉旧的。
 */
const CACHE = 'cinefold-v1'

// 装机时预取的最小集合。index.html 不预缓存 —— 它必须每次都拿新的，
// 否则热更新后浏览器会按旧清单去加载已经不存在的 js
const PRECACHE = ['/icon.svg', '/icon-maskable.svg', '/apple-touch-icon.png']

self.addEventListener('install', (event) => {
  // 新 SW 装完立刻进入 waiting -> 由 activate 里的 clients.claim 接管，
  // 免得用户要关掉所有标签页才能用上新版本
  self.skipWaiting()
  event.waitUntil(
    caches.open(CACHE).then((cache) =>
      // 单个图标取不到不该让整次安装失败
      Promise.allSettled(PRECACHE.map((url) => cache.add(url)))
    )
  )
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  )
})

self.addEventListener('fetch', (event) => {
  const { request } = event

  // 只管 GET。POST/PUT 之类既不该缓存，Cache API 也存不了
  if (request.method !== 'GET') return

  const url = new URL(request.url)

  // 跨域的图片代理、外部封面等交给浏览器默认处理
  if (url.origin !== self.location.origin) return

  // 接口和图片缓存目录直连网络。/pic/ 已经由 nginx 发了 30 天缓存头，
  // 浏览器自己的 HTTP 缓存够用，再套一层 Cache API 只是重复占空间
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/pic/')) return

  // 导航请求（地址栏直接打开、PWA 冷启动）：网络优先。
  // 拿不到网络说明离线，回落到缓存里的入口页，至少能出个壳而不是浏览器的恐龙页
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then((response) => {
          // 只缓存正常响应。502（后台还在启动）缓存下来会让离线时看到假的错误页
          if (response.ok) {
            const copy = response.clone()
            caches.open(CACHE).then((cache) => cache.put('/index.html', copy))
          }
          return response
        })
        .catch(() => caches.match('/index.html'))
    )
    return
  }

  // 其余同源静态资源：cache-first。命中直接给，没命中取回来并存下
  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached
      return fetch(request).then((response) => {
        if (response.ok && response.type === 'basic') {
          const copy = response.clone()
          caches.open(CACHE).then((cache) => cache.put(request, copy))
        }
        return response
      })
    })
  )
})
