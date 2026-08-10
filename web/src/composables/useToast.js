import { ref } from 'vue'

const toasts = ref([])
let seq = 0

export function useToast() {
  const push = (message, type = 'info', duration = 3000) => {
    const id = ++seq
    toasts.value.push({ id, message, type })
    setTimeout(() => {
      toasts.value = toasts.value.filter((t) => t.id !== id)
    }, duration)
  }

  return {
    toasts,
    success: (msg) => push(msg, 'success'),
    error: (msg) => push(msg, 'error', 4500),
    info: (msg) => push(msg, 'info'),
  }
}
