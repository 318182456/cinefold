import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { getConfig, saveConfig } from '@/api'

// 隐私模式三态，按 header 上开关的循环顺序排列
export const IMAGE_MODES = ['VISIBLE', 'BLUR', 'INVISIBLE']

export const useConfigStore = defineStore('config', () => {
  const config = ref({})
  const loaded = ref(false)
  // 未加载完成前按最保守的模式渲染，避免图片闪现
  const imageMode = ref('BLUR')

  // 图片上要挂的 class，三处图片（封面、灯箱、演员头像）共用一份判断
  const imageClass = computed(() => {
    if (imageMode.value === 'BLUR') return 'img-blur'
    if (imageMode.value === 'INVISIBLE') return 'img-hidden'
    return ''
  })

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

  // 与设置页的「图片模式」是同一个配置项，改这里等于改那里。
  // 先落到本地再写后端：遮挡要立刻生效，等一个来回图早就被人看见了
  async function setImageMode(mode) {
    if (!IMAGE_MODES.includes(mode)) return
    const previous = imageMode.value
    imageMode.value = mode
    config.value = { ...config.value, image_mode: mode }
    try {
      await saveConfig({ image_mode: mode })
    } catch (err) {
      // 存不下就退回原值，免得界面显示的和实际配置对不上
      imageMode.value = previous
      config.value = { ...config.value, image_mode: previous }
      throw err
    }
  }

  /** 按 VISIBLE → BLUR → INVISIBLE → VISIBLE 循环 */
  function cycleImageMode() {
    const next = IMAGE_MODES[(IMAGE_MODES.indexOf(imageMode.value) + 1) % IMAGE_MODES.length]
    return setImageMode(next)
  }

  return { config, loaded, imageMode, imageClass, load, setImageMode, cycleImageMode }
})
