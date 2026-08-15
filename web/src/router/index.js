import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/login', name: 'Login', component: () => import('@/views/Login.vue'), meta: { public: true } },
  {
    path: '/',
    component: () => import('@/layouts/MainLayout.vue'),
    children: [
      { path: '', name: 'Dashboard', component: () => import('@/views/Dashboard.vue'), meta: { title: '看板' } },
      { path: 'subscribe', name: 'Subscribe', component: () => import('@/views/Subscribe.vue'), meta: { title: '订阅' } },
      { path: 'search', name: 'Search', component: () => import('@/views/Search.vue'), meta: { title: '搜索' } },
      { path: 'rank', name: 'Rank', component: () => import('@/views/Rank.vue'), meta: { title: '榜单' } },
      { path: 'hot', name: 'Hot', component: () => import('@/views/Hot.vue'), meta: { title: '推荐' } },
      { path: 'brands', name: 'Brands', component: () => import('@/views/Brands.vue'), meta: { title: '厂牌' } },
      { path: 'actors', name: 'Actors', component: () => import('@/views/Actors.vue'), meta: { title: '演员' } },
      { path: 'task', name: 'Task', component: () => import('@/views/Task.vue'), meta: { title: '任务' } },
      { path: 'logs', name: 'Logs', component: () => import('@/views/Logs.vue'), meta: { title: '日志' } },
      { path: 'datasource', name: 'DataSource', component: () => import('@/views/DataSource.vue'), meta: { title: '数据源' } },
      { path: 'medialink', name: 'MediaLink', component: () => import('@/views/MediaLink.vue'), meta: { title: '硬链接' } },
      { path: 'watchdir', name: 'WatchDir', component: () => import('@/views/WatchDir.vue'), meta: { title: '监控目录' } },
      { path: 'config', name: 'Config', component: () => import('@/views/Config.vue'), meta: { title: '设置' } },
      { path: 'migrate', name: 'Migrate', component: () => import('@/views/Migrate.vue'), meta: { title: '数据迁移' } },
      { path: 'profile', name: 'Profile', component: () => import('@/views/Profile.vue'), meta: { title: '账户' } },
    ],
  },
  { path: '/:pathMatch(.*)*', name: 'NotFound', component: () => import('@/views/NotFound.vue'), meta: { public: true } },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  // 桌面滚的是布局里的 <main>，不是 window，vue-router 的返回值对它无效，
  // 得手动归零；移动端整页滚动才轮得到 window
  scrollBehavior() {
    document.querySelector('main')?.scrollTo({ top: 0 })
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
