<script setup>
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  getAuthMethods, login, oidcLoginUrl,
  passkeyLoginBegin, passkeyLoginFinish,
} from '@/api'
import { useToast } from '@/composables/useToast'
import { PasskeyCancelled, getCredential, isSupported } from '@/utils/webauthn'
import AppLogo from '@/components/AppLogo.vue'
import AppIcon from '@/components/AppIcon.vue'

const route = useRoute()
const router = useRouter()
const toast = useToast()

const username = ref('')
const password = ref('')
const loading = ref(false)
const passkeyBusy = ref(false)

const methods = ref({ password: true, oidc: { enabled: false }, passkey: { enabled: false } })
const passkeySupported = isSupported()

function gotoNext(fallback) {
  router.push(route.query.redirect || fallback || { name: 'Dashboard' })
}

async function submit() {
  if (!username.value || !password.value) {
    toast.error('请填写用户名和密码')
    return
  }
  loading.value = true
  try {
    const data = await login(username.value, password.value)
    localStorage.setItem('token', data.token)
    gotoNext()
  } catch (err) {
    toast.error(err.message)
  } finally {
    loading.value = false
  }
}

function ssoLogin() {
  // 整页跳到提供商，回来时带 token
  window.location.href = oidcLoginUrl(route.query.redirect || '/')
}

async function passkeyLogin() {
  passkeyBusy.value = true
  try {
    const { options } = await passkeyLoginBegin(username.value || '')
    const credential = await getCredential(options)
    const data = await passkeyLoginFinish(credential)
    localStorage.setItem('token', data.token)
    toast.success(`欢迎回来，${data.username}`)
    gotoNext()
  } catch (err) {
    // 用户主动取消不算错误
    if (!(err instanceof PasskeyCancelled)) toast.error(err.message)
  } finally {
    passkeyBusy.value = false
  }
}

/** SSO 回调把结果放在 query 上 */
function consumeSsoResult() {
  const { sso_token: token, sso_user: user, sso_error: error, next } = route.query
  if (error) {
    toast.error(String(error))
  } else if (token) {
    localStorage.setItem('token', String(token))
    toast.success(`欢迎回来，${user || ''}`)
    router.replace(typeof next === 'string' && next.startsWith('/') ? next : '/')
    return true
  }
  if (error) {
    // 清掉 query，刷新时不再重复提示
    router.replace({ name: 'Login' })
  }
  return false
}

onMounted(async () => {
  if (consumeSsoResult()) return
  try {
    methods.value = await getAuthMethods()
  } catch {
    // 拿不到就只显示密码登录
  }
})
</script>

<template>
  <div class="login-page flex h-full min-h-0 overflow-auto px-5 py-6">
    <div class="login-shell shrink-0">
      <aside class="login-story" aria-label="cinefold 媒体工作区">
        <div><div class="flex items-center gap-3"><AppLogo :size="34" /><span class="text-lg font-semibold tracking-tight">cinefold<span class="text-brand">.</span></span></div><h2>好影片，<br>值得<span>好好收藏。</span></h2><p class="mt-4 max-w-xs">发现、订阅、下载与入库。<br>让每一部想看的影片，都有自己的位置。</p></div>
        <div class="mt-8"><div class="login-feature"><AppIcon name="bookmark" :size="17" />从发现到订阅，持续追踪更新</div><div class="login-feature"><AppIcon name="library" :size="17" />统一管理媒体库与个人片单</div><div class="login-feature"><AppIcon name="task" :size="17" />自动整理，让收藏保持有序</div></div>
      </aside>
      <div class="login-form-panel">
        <div class="mb-8 flex items-center gap-2 lg:hidden"><AppLogo :size="32" /><span class="text-lg font-semibold">cinefold<span class="text-brand">.</span></span></div>
        <p class="section-eyebrow">WELCOME BACK</p><h1>欢迎回来</h1><p class="mt-2 text-xs text-gray-500">登录你的媒体工作区，继续发现与收藏。</p>
        <form class="space-y-5" @submit.prevent="submit">
          <div><label for="login-username" class="label">用户名</label><input id="login-username" v-model.trim="username" class="input" autocomplete="username" placeholder="输入用户名" /></div>
          <div><label for="login-password" class="label">密码</label><input id="login-password" v-model="password" type="password" class="input" autocomplete="current-password" placeholder="输入密码" /></div>
          <button type="submit" class="btn-primary w-full" :disabled="loading">{{ loading ? '登录中…' : '登录工作区' }}<AppIcon v-if="!loading" name="arrow" :size="16" /></button>
          <template v-if="methods.oidc?.enabled || (methods.passkey?.enabled && passkeySupported)">
            <div class="flex items-center gap-3 py-1"><span class="h-px flex-1 bg-gray-800" /><span class="text-[11px] text-gray-500">其他登录方式</span><span class="h-px flex-1 bg-gray-800" /></div>
            <button v-if="methods.oidc?.enabled" type="button" class="btn-ghost w-full" @click="ssoLogin">使用 {{ methods.oidc.display_name }} 登录</button>
            <button v-if="methods.passkey?.enabled && passkeySupported" type="button" class="btn-ghost w-full" :disabled="passkeyBusy" @click="passkeyLogin">{{ passkeyBusy ? '等待验证…' : '使用 Passkey 登录' }}</button>
          </template>
        </form>
        <p class="mt-7 border-t border-gray-800 pt-5 text-xs leading-relaxed text-gray-500">无法登录？请联系管理员检查账户或身份验证设置。</p>
      </div>
    </div>
  </div>
</template>
