import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getConfig } from '@/api'

export const useConfigStore = defineStore('config', () => {
  const config = ref({})
  const loaded = ref(false)
  // 未加载完成前按最保守的模式渲染，避免图片闪现
  const imageMode = ref('BLUR')

  async function load(force = false) {
    if (loaded.value && !force) return config.value
    try {
      const data = await getConfig()
      config.value = data || {}
      imageMode.value = data?.image_mode || 'BLUR'
      loaded.value = true
    } catch {
      // 未登录或后端异常时保持默认值，不阻塞页面
    }
    return config.value
  }

  return { config, loaded, imageMode, load }
})
