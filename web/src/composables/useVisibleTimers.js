import { onUnmounted } from 'vue'

// Hidden pages retain one pending refresh, never a backlog of requests.
export function useVisibleTimers() {
  const timers = new Map()
  let disposed = false
  function invoke(id) {
    const item = timers.get(id)
    if (!item || disposed) return
    if (document.hidden) { item.pending = true; return }
    if (item.executing) return
    item.pending = false
    if (!item.repeat) timers.delete(id)
    item.executing = true
    Promise.resolve().then(() => item.fn(...item.args)).catch(error => console.error(error))
      .finally(() => { item.executing = false })
  }
  function schedule(fn, delay, repeat, args) {
    const item = { fn, args, repeat, pending: false, executing: false }
    const id = repeat ? window.setInterval(() => invoke(id), delay) : window.setTimeout(() => invoke(id), delay)
    timers.set(id, item)
    return id
  }
  function clear(id) {
    window.clearTimeout(id); window.clearInterval(id); timers.delete(id)
  }
  function visible() {
    if (!document.hidden) for (const [id, item] of timers) if (item.pending) invoke(id)
  }
  document.addEventListener('visibilitychange', visible)
  onUnmounted(() => {
    disposed = true
    document.removeEventListener('visibilitychange', visible)
    for (const id of timers.keys()) clear(id)
  })
  return {
    setTimeout: (fn, delay, ...args) => schedule(fn, delay, false, args),
    setInterval: (fn, delay, ...args) => schedule(fn, delay, true, args),
    clearTimeout: clear, clearInterval: clear,
  }
}
