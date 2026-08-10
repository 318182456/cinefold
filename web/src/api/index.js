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
export const getProfile = () => http.get('/profile')
export const updateProfile = (data) => http.post('/profile', data)
export const getLongToken = () => http.get('/user/token')
export const resetLongToken = () => http.post('/user/token')

// ---------------------------------------------------------------- 看板
export const getDashboard = () => http.get('/dashboard')
export const getVersion = () => http.get('/version')
export const checkVersion = (refresh = false) =>
  http.get('/version/check', { params: { refresh } })

// ---------------------------------------------------------------- 番号
export const listCodes = (params) => http.get('/codes/list', { params })
export const searchCodes = (keyword) => http.get('/search', { params: { keyword } })
export const searchTorrents = (code) => http.get('/torrents', { params: { code } })
export const subscribeCode = (code) => http.post('/codes/sub', { code })
export const cancelCode = (code) => http.post('/codes/cancel', { code })
export const downloadCode = (payload) => http.post('/codes/download', payload)
export const downloadAll = () => http.post('/codes/download/all')
export const getReleaseToday = () => http.get('/codes/release_today')
export const getRecommend = (limit = 20) => http.get('/codes/recommend', { params: { limit } })
export const translateTitles = () => http.post('/codes/translate')

// ---------------------------------------------------------------- 榜单
export const getRank = (rankType = '') => http.get('/rank', { params: { rank_type: rankType } })
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
