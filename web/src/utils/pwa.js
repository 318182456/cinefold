/* Service Worker 的注册与更新。
 *
 * 开发环境不注册：vite dev 的模块是即时编译的，SW 一插手就会拿到过期的
 * 模块副本，改代码不生效，排查起来还很难看出是 SW 干的。
 */
export function registerSW() {
  if (!('serviceWorker' in navigator)) return
  if (import.meta.env.DEV) return

  // 等 load 之后再注册，别和首屏资源抢带宽
  window.addEventListener('load', async () => {
    try {
      const reg = await navigator.serviceWorker.register('/sw.js')

      // 装了新版 SW 时，旧页面还在跑旧代码。这里只在「已经有 controller」
      // （即不是首次安装）时提示，首次安装没有可换的东西，弹提示反而莫名其妙
      reg.addEventListener('updatefound', () => {
        const sw = reg.installing
        if (!sw) return
        sw.addEventListener('statechange', () => {
          if (sw.state === 'installed' && navigator.serviceWorker.controller) {
            window.dispatchEvent(new CustomEvent('sw:updated'))
          }
        })
      })
    } catch {
      // 注册失败不影响正常使用，页面照常跑
    }
  })

  // 新 SW 用 skipWaiting + claim 接管后会触发这个事件。
  // 不自动 reload —— 用户可能正在填表单，刷新会把输入弄丢
  let refreshing = false
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (refreshing) return
    refreshing = true
  })
}
