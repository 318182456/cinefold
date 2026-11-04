<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { checkVersion, getVersion } from '@/api'
import { useConfigStore } from '@/stores/config'
import AppLogo from '@/components/AppLogo.vue'
import AgentPanel from '@/components/AgentPanel.vue'
import PrivacyToggle from '@/components/PrivacyToggle.vue'
import CommandMenu from '@/components/CommandMenu.vue'
import AppIcon from '@/components/AppIcon.vue'

const route = useRoute()
const router = useRouter()
const configStore = useConfigStore()

const menuOpen = ref(false)
const menuButton = ref(null)
const sidebar = ref(null)
const version = __APP_VERSION__
const backendVersion = ref('')
// 检测不到新版本时（镜像私有、无网络）保持为空，不显示红点
const update = ref(null)

const destinations = [
  {
    title: '收藏管理',
    items: [
      { name: 'Library', label: '媒体库与片单', icon: 'M4 4h16v16H4zM4 9h16M9 9v11' },
      { name: 'LibraryInsights', label: '收藏分析与体检', icon: 'M4 20h16M7 16v-4m5 4V8m5 8V4' },
      { name: 'SubscriptionRules', label: '条件订阅与方案', icon: 'M4 6h16M7 12h10M10 18h4' },
      { name: 'ToolConnections', label: '工具接入', icon: 'M8 7h8v10H8zM5 10H3m18 4h-2M10 5V3m4 18v-2' },
    ],
  },
  {
    title: '资源',
    items: [
      { name: 'Dashboard', label: '看板', icon: 'M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6' },
      { name: 'Downloads', label: '下载管理', icon: 'M12 3v12m-5-5l5 5 5-5M5 17v4h14v-4' },
      { name: 'Subscribe', label: '订阅', icon: 'M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9' },
      { name: 'Search', label: '搜索', icon: 'M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z' },
      { name: 'Resources', label: '资源库', icon: 'M4 4h16v16H4zM8 8h8M8 12h8M8 16h4' },
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
      { name: 'WatchDir', label: '监控目录', icon: 'M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z' },
      { name: 'Scrape', label: '入库管理', icon: 'M7 4v16M17 4v16M3 8h4m10 0h4M3 12h18M3 16h4m10 0h4M4 20h16a1 1 0 001-1V5a1 1 0 00-1-1H4a1 1 0 00-1 1v14a1 1 0 001 1z' },
      { name: 'Config', label: '设置', icon: 'M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z' },
      { name: 'Profile', label: '账户', icon: 'M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z' },
    ],
  },
]

const allDestinations = destinations.flatMap(group => group.items)
function workspace(name, label, pages = []) {
  return { ...allDestinations.find(item => item.name === name), label,
    pages: pages.map(([name, label]) => ({ name, label })) }
}
const groups = [{ title: '工作区', items: [
  workspace('Dashboard', '看板'),
  workspace('Library', '媒体库', [['Library', '影片与片单'], ['LibraryInsights', '分析与播放日历']]),
  workspace('Subscribe', '订阅与下载', [['Subscribe', '订阅列表'], ['Downloads', '下载管理与历史'], ['SubscriptionRules', '条件与方案']]),
  workspace('Search', '发现', [['Search', '搜索'], ['Resources', '资源库'], ['Rank', '榜单'], ['Hot', '推荐'], ['Actors', '演员'], ['Brands', '厂牌']]),
  workspace('Scrape', '入库管理'),
  workspace('Logs', '运行记录', [['Logs', '日志'], ['Task', '任务'], ['DeletionHistory', '删除历史']]),
  workspace('Config', '设置', [['Config', '系统配置'], ['ToolConnections', '工具接入'], ['DataSource', '数据源'], ['Maintenance', '维护'], ['Profile', '账户']]),
] }]
const workspaces = groups[0].items
const navigationGroups = [
  { title: '工作区', items: workspaces.slice(0, 3) },
  { title: '探索与整理', items: workspaces.slice(3, 5) },
  { title: '管理', items: workspaces.slice(5) },
]
const activeWorkspace = computed(() => workspaces.find(item => item.name === route.name || item.pages.some(page => page.name === route.name)))
const mobileDestinations = [
  { name: 'Dashboard', label: '总览', icon: 'grid', workspace: 'Dashboard' },
  { name: 'Library', label: '媒体库', icon: 'library', workspace: 'Library' },
  { name: 'Subscribe', label: '订阅', icon: 'bookmark', workspace: 'Subscribe' },
  { name: 'Search', label: '发现', icon: 'search', workspace: 'Search' },
  { name: 'Task', label: '任务', icon: 'task', workspace: 'Logs' },
]

// 抽屉打开时保持页面外壳锁定。
watch(menuOpen, async (open) => {
  document.body.classList.toggle('overflow-hidden', open)
  await nextTick()
  if (open) sidebar.value?.querySelector('a')?.focus()
  else if (window.innerWidth < 1024) menuButton.value?.focus()
})
watch(() => route.fullPath, () => { menuOpen.value = false })
function drawerKeys(event) {
  if (!menuOpen.value) return
  if (event.key === 'Escape') { menuOpen.value = false; return }
  if (event.key !== 'Tab') return
  const controls = [...sidebar.value.querySelectorAll('a,button')].filter(el => el.getClientRects().length)
  const target = event.shiftKey && document.activeElement === controls[0] ? controls.at(-1)
    : !event.shiftKey && document.activeElement === controls.at(-1) ? controls[0] : null
  if (target) { event.preventDefault(); target.focus() }
}
function onResize() { if (window.innerWidth >= 1024) menuOpen.value = false }
onMounted(() => window.addEventListener('resize', onResize))
onUnmounted(() => {
  document.body.classList.remove('overflow-hidden')
  window.removeEventListener('resize', onResize)
})

function logout() {
  localStorage.removeItem('token')
  router.push({ name: 'Login' })
}

onMounted(async () => {
  configStore.load()
  try {
    const data = await getVersion()
    backendVersion.value = data?.version || ''
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
  <div class="app-shell flex h-full min-h-0 overflow-hidden" @keydown="drawerKeys">
    <div v-if="menuOpen" class="fixed inset-0 z-30 bg-black/60 lg:hidden" aria-hidden="true" @click="menuOpen = false" />
    <aside ref="sidebar" class="app-sidebar fixed inset-y-0 left-0 z-40 flex w-56 flex-col transition-transform lg:static lg:translate-x-0" :class="menuOpen ? 'translate-x-0' : '-translate-x-full'" :role="menuOpen ? 'dialog' : undefined" :aria-modal="menuOpen ? true : undefined" aria-label="主导航">
      <RouterLink :to="{name:'Dashboard'}" class="sidebar-brand" @click="menuOpen = false">
        <AppLogo :size="32" />
        <div><span class="block text-base font-semibold tracking-tight text-gray-100">cinefold<span class="text-brand">.</span></span><span class="whitespace-nowrap text-[10px] tracking-[0.12em] text-gray-500">个人媒体工作区</span></div>
      </RouterLink>
      <button v-if="menuOpen" class="drawer-close icon-button lg:hidden" aria-label="关闭导航菜单" @click="menuOpen = false"><AppIcon name="close" /></button>
      <nav class="sidebar-navigation min-h-0 flex-1 overflow-y-auto" aria-label="工作区导航">
        <div v-for="group in navigationGroups" :key="group.title" class="nav-group">
          <p class="nav-group-label">{{ group.title }}</p>
          <RouterLink v-for="item in group.items" :key="item.name" :to="{name:item.name}" class="sidebar-link" :class="{'is-active': activeWorkspace?.name === item.name}" :aria-current="activeWorkspace?.name === item.name ? 'page' : undefined" @click="menuOpen = false">
            <svg class="h-[18px] w-[18px] shrink-0" fill="none" stroke="currentColor" stroke-width="1.75" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" :d="item.icon" /></svg>
            <span>{{ item.label === '看板' ? '总览' : item.label }}</span>
            <span v-if="update && item.name === 'Config'" class="ml-auto h-1.5 w-1.5 rounded-full bg-brand" :title="`有新版本 v${update.latest}`" />
          </RouterLink>
        </div>
      </nav>
      <div class="sidebar-footer">
        <RouterLink :to="{name:'Profile'}" class="sidebar-account"><span class="account-avatar"><AppIcon name="user" :size="17" /></span><span class="min-w-0 flex-1"><span class="block text-xs text-gray-200">我的工作区</span><span class="block text-[11px] text-gray-500">{{ version ? `cinefold v${version}` : '账户与安全' }}</span></span><AppIcon name="chevron" :size="14" /></RouterLink>
        <RouterLink v-if="backendVersion && backendVersion !== version" :to="{name:'Config',query:{group:'upgrade'}}" class="mt-2 block text-[11px] text-amber-400" :title="`当前界面 v${version}，服务 v${backendVersion}`">界面与服务版本不同 · 检查更新</RouterLink>
        <div class="mt-3 flex items-center justify-between"><RouterLink :to="{name:'Config',query:{group:'other'}}" class="text-[11px] text-gray-500 hover:text-gray-200">{{ update ? `可更新 v${update.latest}` : '设置与偏好' }}</RouterLink><button class="icon-button !h-8 !w-8" aria-label="退出登录" title="退出登录" @click="logout"><AppIcon name="logout" :size="16" /></button></div>
      </div>
    </aside>
    <div class="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
      <header class="app-header flex shrink-0 items-center gap-3">
        <button ref="menuButton" class="icon-button lg:hidden" aria-label="打开导航菜单" :aria-expanded="menuOpen" @click="menuOpen = true"><AppIcon name="menu" /></button>
        <div class="flex min-w-0 items-center gap-2 text-xs"><span class="hidden text-gray-500 sm:inline">{{ activeWorkspace?.label === '看板' ? '工作区' : activeWorkspace?.label || '工作区' }}</span><AppIcon name="chevron" :size="12" class="hidden text-gray-700 sm:block" /><h1 class="truncate text-sm font-medium text-gray-200">{{ route.name === 'Dashboard' ? '总览' : route.meta.title }}</h1></div>
        <div class="ml-auto flex shrink-0 items-center gap-2"><CommandMenu /><span class="header-divider" /><PrivacyToggle /></div>
      </header>
      <main class="workspace-main flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        <nav v-if="activeWorkspace?.pages.length > 1" class="workspace-tabs mobile-tabs flex shrink-0 overflow-x-auto" :aria-label="`${activeWorkspace.label}功能`">
          <RouterLink v-for="page in activeWorkspace.pages" :key="page.name" :to="{name:page.name}" :class="{'is-active':route.name === page.name}" :aria-current="route.name === page.name ? 'page' : undefined">{{ page.label }}</RouterLink>
        </nav>
        <div class="route-content min-h-0 min-w-0 flex-1 overflow-hidden">
          <RouterView v-slot="{Component}"><Transition enter-active-class="transition duration-150" enter-from-class="opacity-0" mode="out-in"><component :is="Component" /></Transition></RouterView>
        </div>
      </main>
      <nav class="mobile-bottom-nav shrink-0 lg:hidden" aria-label="常用页面">
        <RouterLink v-for="item in mobileDestinations" :key="item.name" :to="{name:item.name}" :class="{'is-active':activeWorkspace?.name === item.workspace}" :aria-current="activeWorkspace?.name === item.workspace ? 'page' : undefined"><AppIcon :name="item.icon" :size="19" /><span>{{ item.label }}</span></RouterLink>
      </nav>
    </div>
    <AgentPanel />
  </div>
</template>
