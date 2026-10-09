// biome-ignore-all lint/suspicious/noConsole: command-line script that reports progress
// Send prompt.txt, stream the agent's work, then wait for the orchestrator to save the result.
import { readFileSync, writeFileSync } from 'node:fs'
import Anthropic from '@anthropic-ai/sdk'
import { Render } from '@renderinc/sdk'

const state = JSON.parse(readFileSync('session.json', 'utf8'))
const prompt = readFileSync(new URL('../prompt.txt', import.meta.url), 'utf8')
const client = new Anthropic()
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

const signal = AbortSignal.timeout(600_000)
const stream = await client.beta.sessions.events.stream(
  state.session_id,
  {},
  { signal }
)
await client.beta.sessions.events.send(state.session_id, {
  events: [{ type: 'user.message', content: [{ type: 'text', text: prompt }] }],
})
let sent = false
for await (const event of stream) {
  if (event.type === 'user.message') sent = true
  else if (event.type === 'agent.tool_use') console.log(`\n[${event.name}]`)
  else if (event.type === 'agent.message') {
    for (const block of event.content)
      if (block.type === 'text') process.stdout.write(block.text)
  } else if (event.type === 'session.error') {
    throw new Error(event.error?.message ?? 'session error')
  } else if (sent && event.type === 'session.status_idle') {
    // requires_action means a tool call is waiting on the worker in the sandbox.
    if (event.stop_reason?.type === 'requires_action') continue
    if (event.stop_reason?.type !== 'end_turn')
      throw new Error(`Session stopped: ${event.stop_reason?.type}`)
    break
  }
}
console.log(
  '\nTurn completed. Waiting for the orchestrator to save the sandbox...'
)

// After WORKER_MAX_IDLE, the orchestrator saves /workspace as a new claude-session-<ID> snapshot.
const sandboxes = new Render().experimental.sandboxes
const name = `claude-session-${state.session_id}`
for (let i = 0; i < 60; i++) {
  const page = await sandboxes.snapshots.list({
    sandboxGroupId: state.sandbox_group_id,
    status: ['available'],
    limit: 100,
  })
  const saved = page.map((s) => s.snapshot).find((s) => s.name === name)
  if (saved) {
    writeFileSync(
      'session.json',
      JSON.stringify({ ...state, result_snapshot_id: saved.id }, null, 2)
    )
    console.log(`Saved result snapshot: ${saved.id}`)
    process.exit(0)
  }
  await sleep(5000)
}
throw new Error(
  "No result snapshot after five minutes. Check the orchestrator's logs."
)
