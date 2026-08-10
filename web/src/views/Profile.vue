<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { getProfile, updateProfile, getLongToken, resetLongToken } from '@/api'
import { useToast } from '@/composables/useToast'

const toast = useToast()
const router = useRouter()

const username = ref('')
const newUsername = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const longToken = ref('')
const tokenVisible = ref(false)
const saving = ref(false)

async function load() {
  try {
    const data = await getProfile()
    username.value = data.username
    newUsername.value = data.username
  } catch (err) {
    toast.error(err.message)
  }
}

async function save() {
  if (newPassword.value && newPassword.value !== confirmPassword.value) {
    toast.error('两次输入的密码不一致')
    return
  }
  if (!newPassword.value && newUsername.value === username.value) {
    toast.info('没有需要保存的修改')
    return
  }

  saving.value = true
  try {
    const payload = {}
    if (newUsername.value !== username.value) payload.username = newUsername.value
    if (newPassword.value) payload.password = newPassword.value

    const data = await updateProfile(payload)
    // 改了用户名时后端会签发新 token
    if (data?.token) localStorage.setItem('token', data.token)

    toast.success('修改成功')
    newPassword.value = ''
    confirmPassword.value = ''
    await load()
  } catch (err) {
    toast.error(err.message)
  } finally {
    saving.value = false
  }
}

async function showToken() {
  try {
    const data = await getLongToken()
    longToken.value = data.token
    tokenVisible.value = true
  } catch (err) {
    toast.error(err.message)
  }
}

async function resetToken() {
  try {
    const data = await resetLongToken()
    longToken.value = data.token
    tokenVisible.value = true
    toast.success('token 已重置，旧 token 立即失效')
  } catch (err) {
    toast.error(err.message)
  }
}

function copyToken() {
  navigator.clipboard
    ?.writeText(longToken.value)
    .then(() => toast.success('已复制'))
    .catch(() => toast.error('复制失败，请手动选择'))
}

function logout() {
  localStorage.removeItem('token')
  router.push({ name: 'Login' })
}

onMounted(load)
</script>

<template>
  <div class="max-w-xl space-y-4">
    <div class="card space-y-4">
      <p class="text-sm font-medium text-gray-300">账户信息</p>

      <div>
        <label class="label">用户名</label>
        <input v-model.trim="newUsername" class="input" />
      </div>
      <div>
        <label class="label">新密码</label>
        <input v-model="newPassword" type="password" class="input" placeholder="留空则不修改" />
      </div>
      <div>
        <label class="label">确认新密码</label>
        <input v-model="confirmPassword" type="password" class="input" />
      </div>

      <button class="btn-primary" :disabled="saving" @click="save">
        {{ saving ? '保存中…' : '保存修改' }}
      </button>
    </div>

    <div class="card space-y-3">
      <p class="text-sm font-medium text-gray-300">长期 Token</p>
      <p class="text-xs text-gray-500">
        供外部调用 API 使用，不会过期。泄露后请及时重置。
      </p>

      <div v-if="tokenVisible" class="flex gap-2">
        <input :value="longToken" readonly class="input font-mono text-xs" />
        <button class="btn-ghost shrink-0" @click="copyToken">复制</button>
      </div>

      <div class="flex gap-2">
        <button class="btn-ghost text-xs" @click="showToken">查看</button>
        <button class="btn-danger text-xs" @click="resetToken">重置</button>
      </div>
    </div>

    <div class="card">
      <button class="btn-ghost w-full" @click="logout">退出登录</button>
    </div>
  </div>
</template>
