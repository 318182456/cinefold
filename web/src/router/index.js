import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/login', name: 'Login', component: () => import('@/views/Login.vue'), meta: { public: true } },
  {
    path: '/',
    component: () => import('@/layouts/MainLayout.vue'),
    children: [
      { path: '', name: 'Dashboard', component: () => import('@/views/Dashboard.vue'), meta: { title: '看板' } },
      { path: 'downloads', name: 'Downloads', component: () => import('@/views/Downloads.vue'), meta: { title: '下载管理' } },
      { path: 'subscribe', name: 'Subscribe', component: () => import('@/views/Subscribe.vue'), meta: { title: '订阅' } },
      { path: 'subscription-rules', name: 'SubscriptionRules', component: () => import('@/views/SubscriptionRules.vue'), meta: { title: '条件订阅' } },
      { path: 'library', name: 'Library', component: () => import('@/views/Library.vue'), meta: { title: '媒体库' } },
      { path: 'library-insights', name: 'LibraryInsights', component: () => import('@/views/LibraryInsights.vue'), meta: { title: '收藏分析' } },
      { path: 'tool-connections', name: 'ToolConnections', component: () => import('@/views/ToolConnections.vue'), meta: { title: '工具接入' } },
      { path: 'search', name: 'Search', component: () => import('@/views/Search.vue'), meta: { title: '搜索' } },
      { path: 'rank', name: 'Rank', component: () => import('@/views/Rank.vue'), meta: { title: '榜单' } },
      { path: 'hot', name: 'Hot', component: () => import('@/views/Hot.vue'), meta: { title: '推荐' } },
      { path: 'brands', name: 'Brands', component: () => import('@/views/Brands.vue'), meta: { title: '厂牌' } },
      { path: 'actors', name: 'Actors', component: () => import('@/views/Actors.vue'), meta: { title: '演员' } },
      { path: 'task', name: 'Task', component: () => import('@/views/Task.vue'), meta: { title: '任务' } },
      { path: 'logs', name: 'Logs', component: () => import('@/views/Logs.vue'), meta: { title: '日志' } },
      { path: 'datasource', name: 'DataSource', component: () => import('@/views/DataSource.vue'), meta: { title: '数据源' } },
      { path: 'medialink', name: 'MediaLink', redirect: to => ({ name: 'Scrape', query: { ...to.query, tab: 'links' }, hash: to.hash }) },
      { path: 'watchdir', name: 'WatchDir', redirect: to => ({ name: 'Scrape', query: { ...to.query, tab: 'watch' }, hash: to.hash }) },
      { path: 'scrape', name: 'Scrape', component: () => import('@/views/Scrape.vue'), meta: { title: '入库管理' } },
      { path: 'config', name: 'Config', component: () => import('@/views/Config.vue'), meta: { title: '设置' } },
      { path: 'migrate', redirect: { name: 'Config' } },
      { path: 'maintenance', name: 'Maintenance', component: () => import('@/views/Maintenance.vue'), meta: { title: '维护' } },
      { path: 'profile', name: 'Profile', component: () => import('@/views/Profile.vue'), meta: { title: '账户' } },
    ],
  },
  { path: '/:pathMatch(.*)*', name: 'NotFound', component: () => import('@/views/NotFound.vue'), meta: { public: true } },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  // 路由内容在各自的局部容器滚动；页面外壳始终固定。
  scrollBehavior() {
    document.querySelectorAll('.page-panel-content').forEach(element => element.scrollTo({ top: 0 }))
    return { top: 0 }
  },
})

router.beforeEach((to) => {
  if (to.meta.public) return true
  if (!localStorage.getItem('token')) {
    return { name: 'Login', query: { redirect: to.fullPath } }
  }
  return true
})

export default router
