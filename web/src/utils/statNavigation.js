import { nextTick } from 'vue'

// 统计入口定位明细，兼容桌面的 main 滚动容器与移动端整页滚动。
export async function revealDetails(id) {
  await nextTick()
  document.getElementById(id)?.scrollIntoView({
    behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth',
    block: 'start',
  })
}
