import { computed, ref } from 'vue'
import { cancelCodesBatch, subscribeCodesBatch } from '@/api'
import { useToast } from '@/composables/useToast'

/**
 * 番号卡片列表的多选。
 *
 * @param visible 当前页可见的番号数组（ref/computed），全选按钮只作用于这一页
 * @param onDone  批量操作成功后的回调，一般是重新加载列表
 */
export function useCodeSelection(visible, onDone) {
  const toast = useToast()
  const active = ref(false)
  const selected = ref(new Set())
  const busy = ref(false)

  const codes = computed(() => visible.value.map((i) => i.code))
  const count = computed(() => selected.value.size)
  // 翻页后选中项可能都不在当前页，全选态只看当前页
  const allSelected = computed(
    () => codes.value.length > 0 && codes.value.every((c) => selected.value.has(c)),
  )

  function isSelected(code) {
    return selected.value.has(code)
  }

  function toggle(code) {
    // Set 原地改动不触发响应式，得换新实例
    const next = new Set(selected.value)
    next.has(code) ? next.delete(code) : next.add(code)
    selected.value = next
  }

  function toggleAll() {
    const next = new Set(selected.value)
    if (allSelected.value) codes.value.forEach((c) => next.delete(c))
    else codes.value.forEach((c) => next.add(c))
    selected.value = next
  }

  function clear() {
    selected.value = new Set()
  }

  function enter() {
    active.value = true
  }

  function exit() {
    active.value = false
    clear()
  }

  // 响应拦截器只把 data 透出来，message 拿不到，提示在前端拼
  async function run(fn, tip) {
    if (!count.value || busy.value) return
    busy.value = true
    try {
      const data = await fn([...selected.value])
      toast.success(tip(data))
      clear()
      await onDone?.()
    } catch (err) {
      toast.error(err.message)
    } finally {
      busy.value = false
    }
  }

  const cancelSelected = () =>
    run(cancelCodesBatch, (d) => {
      const missing = d?.missing?.length || 0
      const done = d?.cancelled?.length || 0
      return missing ? `已取消订阅 ${done} 个，${missing} 个不在库中` : `已取消订阅 ${done} 个`
    })

  const subscribeSelected = () =>
    run(subscribeCodesBatch, (d) => {
      const filtered = d?.filtered?.length || 0
      const done = d?.subscribed?.length || 0
      return filtered ? `已订阅 ${done} 个，${filtered} 个被过滤规则拦下` : `已订阅 ${done} 个`
    })

  return {
    active,
    busy,
    count,
    allSelected,
    isSelected,
    toggle,
    toggleAll,
    clear,
    enter,
    exit,
    cancelSelected,
    subscribeSelected,
  }
}
