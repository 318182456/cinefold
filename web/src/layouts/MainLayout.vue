<script setup>
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { checkVersion, getVersion } from '@/api'
import { useConfigStore } from '@/stores/config'
import AppLogo from '@/components/AppLogo.vue'
import AgentPanel from '@/components/AgentPanel.vue'

const route = useRoute()
const router = useRouter()
const configStore = useConfigStore()

const menuOpen = ref(false)
const version = ref('')
// 检测不到新版本时（镜像私有、无网络）保持为空，不显示红点
const update = ref(null)

const groups = [
  {
    title: '资源',
    items: [
      { name: 'Dashboard', label: '看板', icon: 'M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6' },
      { name: 'Subscribe', label: '订阅', icon: 'M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9' },
      { name: 'Search', label: '搜索', icon: 'M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z' },
    ],
  },
  {
    title: '发现',
    items: [
      { name: 'Rank', label: '榜单', icon: 'M16 8v8m-4-5v5m-4-2v2m-2 4h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z' },
      { name: 'Hot', label: '推荐', icon: 'M4.318 6.318a4.5 4.5 0 000 6.364L12 20.364l7.682-7.682a4.5 4.5 0 00-6.364-6.364L12 7.636l-1.318-1.318a4.5 4.5 0 00-6.364 0z' },
      { name: 'Brands', label: '厂牌', icon: 'M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4' },
      { name: 'Actors', label: '演员', icon: 'M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197M13 7a4 4 0 11-8 0 4 4 0 018 0z' },
    ],
  },
  {
    title: '系统',
    items: [
      { name: 'Task', label: '任务', icon: 'M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4' },
      { name: 'Logs', label: '日志', icon: 'M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z' },
      { name: 'DataSource', label: '数据源', icon: 'M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9' },
      { name: 'MediaLink', label: '硬链接', icon: 'M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1' },
      { name: 'WatchDir', label: '监控目录', icon: 'M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z' },
      { name: 'Config', label: '设置', icon: 'M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z' },
      { name: 'Migrate', label: '数据迁移', icon: 'M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4' },
      { name: 'Profile', label: '账户', icon: 'M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z' },
    ],
  },
]

// 移动端整页可滚后，抽屉打开时背景还能跟着滑。锁住 body 的滚动，
// 关掉再放开。桌面 body 本来就不滚，这条无影响
watch(menuOpen, (open) => {
  document.body.classList.toggle('overflow-hidden', open)
})
onUnmounted(() => document.body.classList.remove('overflow-hidden'))

function logout() {
  localStorage.removeItem('token')
  router.push({ name: 'Login' })
}

onMounted(async () => {
  configStore.load()
  try {
    const data = await getVersion()
    version.value = data?.version || ''
  } catch {
    // 版本获取失败不影响使用
  }
  try {
    const data = await checkVersion()
    if (data?.has_update) update.value = data
  } catch {
    // 检测失败就当没有新版本，不打扰使用
  }
})
</script>

<template>
  <div class="lg:flex lg:h-full lg:overflow-hidden">
    <!-- 移动端遮罩 -->
    <div
      v-if="menuOpen"
      class="fixed inset-0 z-30 bg-black/60 lg:hidden"
      @click="menuOpen = false"
    />

    <!-- 侧边栏 -->
    <aside
      class="fixed inset-y-0 left-0 z-40 flex w-60 flex-col border-r border-gray-800 bg-gray-900 transition-transform lg:static lg:translate-x-0"
      :class="menuOpen ? 'translate-x-0' : '-translate-x-full'"
    >
      <div
        class="flex min-h-14 items-center gap-2 border-b border-gray-800 px-5 pt-[env(safe-area-inset-top)] lg:pt-0"
      >
        <AppLogo :size="28" />
        <span class="font-semibold tracking-tight">cinefold</span>
      </div>

      <nav class="flex-1 space-y-5 overflow-y-auto p-3">
        <div v-for="group in groups" :key="group.title">
          <p class="px-2 pb-1.5 text-[11px] font-medium uppercase tracking-wider text-gray-600">
            {{ group.title }}
          </p>
          <RouterLink
            v-for="item in group.items"
            :key="item.name"
            :to="{ name: item.name }"
            class="flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition-colors"
            :class="
              route.name === item.name
                ? 'bg-brand/10 text-brand'
                : 'text-gray-400 hover:bg-gray-800 hover:text-gray-200'
            "
            @click="menuOpen = false"
          >
            <svg class="h-4 w-4 shrink-0" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" :d="item.icon" />
            </svg>
            {{ item.label }}
            <span
              v-if="update && item.name === 'Config'"
              class="ml-auto h-1.5 w-1.5 rounded-full bg-red-500"
              :title="`有新版本 v${update.latest}`"
            />
          </RouterLink>
        </div>
      </nav>

      <div class="border-t border-gray-800 p-3 pb-[calc(0.75rem+env(safe-area-inset-bottom))]">
        <p v-if="version" class="flex items-center gap-1.5 px-2 pb-2 text-[11px] text-gray-600">
          <span>v{{ version }}</span>
          <template v-if="update">
            <span class="h-1.5 w-1.5 shrink-0 rounded-full bg-red-500" />
            <span class="text-gray-400">可更新 v{{ update.latest }}</span>
          </template>
        </p>
        <button class="btn-ghost w-full text-xs" @click="logout">退出登录</button>
      </div>
    </aside>

    <!-- 主区 -->
    <div class="flex min-w-0 flex-1 flex-col">
      <!-- 移动端整页滚动，头部改成 sticky 才能跟着留在顶上；桌面外壳不滚，
           sticky 无副作用。h 用 min-h + 顶部安全区内边距：装成 PWA 后状态栏
           是半透明的，内容会顶到它下面，不留这段就被系统时间盖住 -->
      <header
        class="sticky top-0 z-20 flex min-h-14 shrink-0 items-center gap-3 border-b border-gray-800 bg-gray-950/90 px-4 pt-[env(safe-area-inset-top)] backdrop-blur lg:px-6"
      >
        <button class="rounded-lg p-1.5 text-gray-400 hover:bg-gray-800 lg:hidden" @click="menuOpen = true">
          <svg class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <path stroke-linecap="round" d="M4 6h16M4 12h16M4 18h16" />
          </svg>
        </button>
        <h1 class="text-sm font-medium text-gray-300">{{ route.meta.title || '' }}</h1>
      </header>

      <!-- 滚动容器只在桌面生效，移动端交给 body 滚。
           pb 留出底部安全区，免得内容被 iPhone 的 home indicator 压住 -->
      <main class="flex-1 p-4 pb-[calc(1rem+env(safe-area-inset-bottom))] lg:min-h-0 lg:overflow-y-auto lg:p-6">
        <RouterView v-slot="{ Component }">
          <Transition
            enter-active-class="transition duration-150"
            enter-from-class="opacity-0"
            mode="out-in"
          >
            <component :is="Component" />
          </Transition>
        </RouterView>
      </main>
    </div>

    <!-- 全站可用的悬浮助手 -->
    <AgentPanel />
  </div>
</template>
