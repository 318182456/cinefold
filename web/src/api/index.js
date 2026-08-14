import axios from 'axios'
import router from '@/router'

const http = axios.create({
  baseURL: '/api/v1',
  timeout: 60000,
})

// 请求：自动带 token
http.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

// 响应：拆掉统一响应体外壳，401 跳登录
http.interceptors.response.use(
  (response) => {
    const body = response.data
    if (body && typeof body === 'object' && 'code' in body) {
      if (body.code === 200) return body.data
      // 业务错误交给调用方 catch，同时把 message 带出去
      return Promise.reject(new Error(body.message || '请求失败'))
    }
    return body
  },
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem('token')
      if (router.currentRoute.value.name !== 'Login') {
        router.push({ name: 'Login' })
      }
      return Promise.reject(new Error('登录已过期'))
    }
    const detail = error.response?.data?.message || error.message
    return Promise.reject(new Error(detail || '网络异常'))
  },
)

export default http

// ---------------------------------------------------------------- 认证
export const login = (username, password) => http.post('/login', { username, password })
export const getAuthMethods = () => http.get('/auth/methods')
export const getOidcRedirectUri = () => http.get('/auth/oidc/redirect-uri')
export const testOidc = (issuer = '') => http.post('/auth/oidc/test', { issuer })
// SSO 要整页跳转，不能走 axios
export const oidcLoginUrl = (next = '/') =>
  `/api/v1/auth/oidc/login?next=${encodeURIComponent(next)}`

export const passkeyRegisterBegin = () => http.post('/auth/passkey/register/begin')
export const passkeyRegisterFinish = (credential, label = '') =>
  http.post('/auth/passkey/register/finish', { credential, label })
export const passkeyLoginBegin = (username = '') =>
  http.post('/auth/passkey/login/begin', { username })
export const passkeyLoginFinish = (credential) =>
  http.post('/auth/passkey/login/finish', { credential })
export const listPasskeys = () => http.get('/auth/passkey/list')
export const deletePasskey = (credentialId) =>
  http.delete(`/auth/passkey/${encodeURIComponent(credentialId)}`)
export const getProfile = () => http.get('/profile')
export const updateProfile = (data) => http.post('/profile', data)
export const getLongToken = () => http.get('/user/token')
export const resetLongToken = () => http.post('/user/token')

// ---------------------------------------------------------------- 看板
export const getDashboard = () => http.get('/dashboard')
export const getVersion = () => http.get('/version')
export const checkVersion = (refresh = false) =>
  http.get('/version/check', { params: { refresh } })
export const getUpgradeStatus = () => http.get('/upgrade/status')
export const startUpgrade = (version = '') =>
  http.post('/upgrade', null, { params: { version } })
export const rollbackUpgrade = () => http.post('/upgrade/rollback')

// ---------------------------------------------------------------- 番号
export const listCodes = (params) => http.get('/codes/list', { params })
export const searchCodes = (keyword) => http.get('/search', { params: { keyword } })
export const searchTorrents = (code) => http.get('/torrents', { params: { code } })
export const subscribeCode = (code) => http.post('/codes/sub', { code })
export const cancelCode = (code) => http.post('/codes/cancel', { code })
// 多选批量操作，一次请求走完，不在前端循环
export const subscribeCodesBatch = (codes) => http.post('/codes/sub/batch', { codes })
export const cancelCodesBatch = (codes) => http.post('/codes/cancel/batch', { codes })
export const downloadCode = (payload) => http.post('/codes/download', payload)
export const downloadAll = () => http.post('/codes/download/all')
export const getReleaseToday = () => http.get('/codes/release_today')
export const getRecommend = (limit = 15, page = 1) =>
  http.get('/codes/recommend', { params: { limit, page } })
export const translateTitles = () => http.post('/codes/translate')

// ---------------------------------------------------------------- 数据源
export const listDataSources = () => http.get('/datasources')
export const createDataSource = (payload) => http.post('/datasources', payload)
export const updateDataSource = (key, payload) => http.put(`/datasources/${key}`, payload)
export const deleteDataSource = (key) => http.delete(`/datasources/${key}`)
export const restoreDataSource = (key) => http.post(`/datasources/${key}/restore`)
export const checkDataSource = (key) => http.post(`/datasources/${key}/check`)
export const checkAllDataSources = () => http.post('/datasources/check')

// ---------------------------------------------------------------- 硬链接
export const listMediaLinks = (params) => http.get('/medialinks', { params })
// check_missing=false 时跳过全表磁盘探测，只回总数与番号数，秒级返回。
// 带探测那次库大时会超过全局 60s，单独放宽超时，免得白等一场又超时
export const getMediaLinkStats = (checkMissing = true) =>
  http.get('/medialinks/stats', {
    params: { check_missing: checkMissing },
    ...(checkMissing ? { timeout: 300000 } : {}),
  })
export const registerMediaLink = (payload) => http.post('/medialinks/register', payload)
export const previewMediaLinkDelete = (payload) =>
  http.post('/medialinks/preview', payload)
export const deleteMediaLink = (payload) => http.post('/medialinks/delete', payload)
// 只删库里的记录，不碰文件
export const dropMediaLinkRecord = (linkPath) =>
  http.delete('/medialinks/record', { params: { link_path: linkPath } })
export const pruneMediaLinks = () => http.post('/medialinks/prune')
// 下载侧已删、媒体库侧仍在的关联。一轮要拉下载器全量种子清单再逐条探测文件，
// 与 stats 那次一个量级，同样放宽超时
export const listMediaLinkOrphans = (params) =>
  http.get('/medialinks/orphans', { params, timeout: 300000 })
// 批量联动删除。逐条删种 + 删文件，选得多时很慢，超时放宽
export const batchDeleteMediaLinks = (payload) =>
  http.post('/medialinks/batch-delete', payload, { timeout: 600000 })
// 批量只删记录，不碰文件
export const batchDropMediaLinkRecords = (linkPaths) =>
  http.post('/medialinks/batch-record', { link_paths: linkPaths })
// 从 History 反推重建缺失的关联记录。要扫整个媒体库，放宽超时
export const recoverMediaLinks = (dryRun = true) =>
  http.post('/medialinks/recover', null, {
    params: { dry_run: dryRun },
    timeout: 600000,
  })

// ---------------------------------------------------------------- 监控目录
export const listWatchDirs = () => http.get('/watchdirs')
export const createWatchDir = (payload) => http.post('/watchdirs', payload)
export const updateWatchDir = (id, payload) => http.put(`/watchdirs/${id}`, payload)
export const deleteWatchDir = (id) => http.delete(`/watchdirs/${id}`)
// dry_run 默认为真，只报会做什么
// background=true 时后台执行，立刻返回，进度走 getWatchDirProgress
export const syncWatchDir = (id, dryRun = true, background = false) =>
  http.post(`/watchdirs/${id}/sync`, null, {
    params: { dry_run: dryRun, background },
  })
export const syncAllWatchDirs = (dryRun = true, background = false) =>
  http.post('/watchdirs/sync', null, {
    params: { dry_run: dryRun, background },
  })
export const getWatchDirProgress = (watchId = 0) =>
  http.get('/watchdirs/progress', { params: { watch_id: watchId } })
// 给还没登记种子的关联补查下载器
export const backfillWatchDirTorrents = (watchId = 0) =>
  http.post('/watchdirs/backfill', null, { params: { watch_id: watchId } })
// 把刮削输出目录里已存在但没登记的影片纳入管理。
// dry_run 默认为真 —— 登记结果是反向删除的依据，先让用户核对配对
export const adoptScrapeDir = (dryRun = true) =>
  http.post('/watchdirs/adopt-scrape', null, { params: { dry_run: dryRun } })
// 扣留中的删除：文件已消失但还在宽限期内观察
export const listWatchDirHolds = (watchId = 0) =>
  http.get('/watchdirs/holds', { params: { watch_id: watchId } })
export const cancelWatchDirHold = (linkPath) =>
  http.delete('/watchdirs/holds', { params: { link_path: linkPath } })

// ---------------------------------------------------------------- 榜单
export const getRank = (rankType = '', limit = 120) =>
  http.get('/rank', { params: { rank_type: rankType, limit } })
export const subscribeRank = () => http.post('/rank/subscribe')
export const getHot = () => http.get('/hot')
export const getBrands = () => http.get('/brands')
// 某厂牌的最新发布与预定发布
export const getBrandCodes = (brand, pastDays = 7, futureDays = 14) =>
  http.get('/brands/codes', {
    params: { brand, past_days: pastDays, future_days: futureDays },
  })

// ---------------------------------------------------------------- 演员
export const listActors = (params) => http.get('/actors', { params })
export const subscribeActor = (name, limitDate) =>
  http.post('/actors/sub', { name, limit_date: limitDate })
export const cancelActor = (name) => http.post('/actors/cancel', { name })
export const getActorRank = () => http.get('/actors/rank')
export const purgeMigratedActors = (dryRun = true) =>
  http.post('/actors/purge-migrated', { dry_run: dryRun })
export const getActorCodes = (name) => http.get('/actors/codes', { params: { name } })

// ---------------------------------------------------------------- 系统
export const getConfig = () => http.get('/config')
export const saveConfig = (config) => http.post('/config', { config })
export const getLogs = (lines = 300, keyword = '') =>
  http.get('/logs', { params: { lines, keyword } })
export const listCron = () => http.get('/cron')
export const runTask = (jobId) => http.post('/task', null, { params: { job_id: jobId } })
export const testConnection = (target) => http.get('/test', { params: { target } })
export const listPtSites = () => http.get('/ptsites')
export const getTelegramReceive = () => http.get('/telegram/receive')
export const setTelegramWebhook = (url = '') => http.post('/telegram/webhook', { url })
export const deleteTelegramWebhook = () => http.delete('/telegram/webhook')

// 图片走后端代理，绕过防盗链。
// 带上番号后，后端按 pics/<番号>/banner.jpg 命中本地缓存，不必回源。
export const proxyImage = (url, code = '', kind = 'banner') => {
  if (!url) return ''
  const params = new URLSearchParams({ url })
  if (code) {
    params.set('code', code)
    params.set('kind', kind)
  }
  return `/api/v1/image-proxy?${params}`
}

// 番号封面。库里存了 local_banner 时优先用它，后端直接读盘不出网。
export const codeCover = (item) => {
  if (!item) return ''
  const local = (item.local_banner || '').split(',')[0]
  if (local) return `/api/v1/image-local?path=${encodeURIComponent(local)}`
  return proxyImage(item.banner || item.poster, item.code)
}

// ---------------------------------------------------------------- 数据迁移
export const listMigrateDatabases = () => http.get('/migrate/databases')
export const getMigrateProgress = () => http.get('/migrate/progress')
export const testMigrateTarget = (payload) => http.post('/migrate/test', payload)
export const startMigrate = (payload) => http.post('/migrate/start', payload)
export const getImageCacheStats = () => http.get('/image-cache/stats')
export const bulkCancelSubscribe = (payload) =>
  http.post('/codes/cancel/bulk', payload)

// ---------------------------------------------------------------- AI 助手
export const getAgentStatus = () => http.get('/agent/status')
// 助手可能要连着调好几个工具，比默认 60s 更能等
export const askAgent = (question, history = []) =>
  http.post('/agent/chat', { question, history }, { timeout: 180000 })
// 确认执行助手提出的下载器操作。deleteFiles 由确认框上的复选框决定，
// 传 null 表示沿用助手提案里的动作
export const confirmAgentAction = (proposalId, deleteFiles = null) =>
  http.post('/agent/confirm', { proposal_id: proposalId, delete_files: deleteFiles })
export const cancelAgentAction = (proposalId) =>
  http.post('/agent/cancel', { proposal_id: proposalId })
