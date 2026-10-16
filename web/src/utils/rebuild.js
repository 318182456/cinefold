export const READY_PHASES = ['ready', 'preview_ready', 'previewed']

export function discardScope(plan) {
  if (!plan) return null
  return JSON.stringify([plan.output_dir, plan.unrecoverable_count || 0, [...(plan.unrecoverable_paths || [])].sort()])
}

export function rebuildRunReason({ state, known, busy, statusError, readyToken, policy, confirmDiscard, confirmation, jobRunning, inventoryRunning, now = Date.now() }) {
  if (busy) return '正在提交，请稍候。'
  if (statusError || !known) return '尚未取得最新任务状态，请刷新状态。'
  if (state.running) return ['preview', 'previewing', 'planning'].includes(state.phase)
    ? '正在自动检查范围，完成后才能确认执行。' : '重建任务正在运行，请等待完成。'
  if (state.protection_error) return '删除保护记录异常，请先处理提示中的错误。'
  if (state.error || !READY_PHASES.includes(state.phase)) return '等待自动检查；检查失败时可点击重新检查范围。'
  const plan = state.plan
  if (!plan?.output_dir || !state.token || readyToken !== state.token) return '正在准备范围检查，请稍候。'
  if (plan.expires_at && now >= plan.expires_at * 1000) return '检查结果已过期，请重新检查范围。'
  if ((plan.missing_policy || 'block') !== policy) return '缺失源处理方式已改变，正在等待自动检查。'
  if (plan.blocked_count || plan.blockers?.length) return '检查存在阻止项，请先处理或更改缺失源处理方式。'
  if (jobRunning) return '当前有刮削任务正在运行，完成后即可确认。'
  if (inventoryRunning) return '目录扫描正在运行，完成后即可确认，无需重新输入目录。'
  if (policy === 'discard' && plan.unrecoverable_count && !confirmDiscard) return '请勾选确认不保留无法恢复的视频。'
  if ((confirmation || '').trim() !== plan.output_dir) return '请输入上方完整的播放输出目录。'
  return ''
}
