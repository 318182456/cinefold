import { nextTick } from 'vue'

// 统计入口定位明细，由最近的局部滚动容器展示目标。
export async function revealDetails(id) {
  await nextTick()
  document.getElementById(id)?.scrollIntoView({
    behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth',
    block: 'start',
  })
}
