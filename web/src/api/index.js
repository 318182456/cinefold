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

// 图片走后端代理，绕过防盗链
export const proxyImage = (url) =>
  url ? `/api/v1/image-proxy?url=${encodeURIComponent(url)}` : ''
