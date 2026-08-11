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
  <div class="flex min-h-screen items-center justify-center px-4">
    <div class="w-full max-w-sm">
      <div class="mb-8 flex flex-col items-center gap-3">
        <AppLogo :size="48" />
        <h1 class="text-xl font-semibold tracking-tight">byte-muse</h1>
      </div>

      <form class="card space-y-4" @submit.prevent="submit">
        <div>
          <label class="label">用户名</label>
          <input v-model.trim="username" class="input" autocomplete="username" />
        </div>
        <div>
          <label class="label">密码</label>
          <input v-model="password" type="password" class="input" autocomplete="current-password" />
        </div>
        <button type="submit" class="btn-primary w-full" :disabled="loading">
          {{ loading ? '登录中…' : '登入' }}
        </button>

        <!-- 其他登录方式 -->
        <template v-if="methods.oidc?.enabled || (methods.passkey?.enabled && passkeySupported)">
          <div class="flex items-center gap-3 py-1">
            <span class="h-px flex-1 bg-gray-800" />
            <span class="text-[11px] text-gray-600">或</span>
            <span class="h-px flex-1 bg-gray-800" />
          </div>

          <button
            v-if="methods.oidc?.enabled"
            type="button"
            class="btn-ghost w-full"
            @click="ssoLogin"
          >
            使用 {{ methods.oidc.display_name }} 登录
          </button>

          <button
            v-if="methods.passkey?.enabled && passkeySupported"
            type="button"
            class="btn-ghost w-full"
            :disabled="passkeyBusy"
            @click="passkeyLogin"
          >
            {{ passkeyBusy ? '等待验证…' : '使用 Passkey 登录' }}
          </button>
        </template>
      </form>

      <p class="mt-4 text-center text-xs text-gray-600">
        忘记密码？删除数据目录下的 byte-muse.db 中 user 表记录后重启，会重新生成账号
      </p>
    </div>
  </div>
</template>
