import test from 'node:test'
import assert from 'node:assert/strict'
import { discardScope, rebuildRunReason } from '../src/utils/rebuild.js'

function ready() {
  return { state: { phase: 'ready', running: false, token: 'current', error: '', plan: {
    output_dir: '/media/library', missing_policy: 'discard', unrecoverable_count: 3,
    blocked_count: 0, blockers: [], expires_at: 200,
  } }, known: true, busy: false, readyToken: 'current', policy: 'discard',
  confirmDiscard: true, confirmation: '/media/library', jobRunning: false, inventoryRunning: false, now: 100000 }
}

test('discard confirmation scope ignores ordering but detects changed paths, count and output', () => {
  const plan = {output_dir:'/media/library', unrecoverable_count:2, unrecoverable_paths:['b.mp4','a.mp4']}
  assert.equal(discardScope(null), null)
  assert.equal(discardScope(plan), discardScope({...plan, unrecoverable_paths:['a.mp4','b.mp4']}))
  assert.notEqual(discardScope(plan), discardScope({...plan, unrecoverable_paths:['a.mp4','c.mp4']}))
  assert.notEqual(discardScope(plan), discardScope({...plan, unrecoverable_count:3}))
  assert.notEqual(discardScope(plan), discardScope({...plan, output_dir:'/other/library'}))
})

test('valid confirmed preview enables execution including pasted whitespace', () => {
  assert.equal(rebuildRunReason(ready()), '')
  assert.equal(rebuildRunReason({ ...ready(), confirmation: '  /media/library\n' }), '')
})

for (const phase of ['idle', 'failed', 'completed', 'partial', 'interrupted']) {
  test(`${phase} cannot reuse an old token`, () => {
    const args = ready(); args.state.phase = phase
    assert.match(rebuildRunReason(args), /检查/)
  })
}

test('policy changes and unconfirmed discard disable execution', () => {
  assert.match(rebuildRunReason({ ...ready(), policy: 'keep' }), /处理方式/)
  assert.match(rebuildRunReason({ ...ready(), confirmDiscard: false }), /勾选/)
  const args = ready(); args.state.plan.unrecoverable_count = 0; args.confirmDiscard = false
  assert.equal(rebuildRunReason(args), '')
})

test('running and expired previews or stale tokens disable execution', () => {
  const args = ready(); args.state.running = true; args.state.phase = 'preview'
  assert.match(rebuildRunReason(args), /正在自动检查/)
  assert.match(rebuildRunReason({ ...ready(), readyToken: 'old' }), /检查/)
  assert.match(rebuildRunReason({ ...ready(), now: 200000 }), /过期/)
})

test('directory scanning temporarily disables execution without discarding confirmation', () => {
  const args = ready(); args.inventoryRunning = true
  assert.match(rebuildRunReason(args), /目录扫描/)
  args.inventoryRunning = false
  assert.equal(rebuildRunReason(args), '')
  assert.equal(args.confirmation, '/media/library')
})

test('unknown state, blockers, busy requests, incorrect path and protection errors disable execution', () => {
  assert.match(rebuildRunReason({ ...ready(), known: false }), /状态/)
  assert.match(rebuildRunReason({ ...ready(), busy: true }), /提交/)
  assert.match(rebuildRunReason({ ...ready(), statusError: 'network' }), /状态/)
  assert.match(rebuildRunReason({ ...ready(), confirmation: '/wrong' }), /输出目录/)
  const args = ready(); args.state.plan.blocked_count = 1
  assert.match(rebuildRunReason(args), /阻止项/)
  args.state.protection_error = 'broken'
  assert.match(rebuildRunReason(args), /删除保护/)
})
