// biome-ignore-all lint/suspicious/noConsole: command-line script that reports progress
// Delete the session, its snapshots, the staged task snapshot, and the agent if create_session made it.
import { readFileSync } from 'node:fs'
import Anthropic from '@anthropic-ai/sdk'
import { Render } from '@renderinc/sdk'

const state = JSON.parse(readFileSync('session.json', 'utf8'))
const client = new Anthropic()
const sandboxes = new Render().experimental.sandboxes
const failures: string[] = []
const attempt = async (what: string, run: () => Promise<unknown>) => {
  try {
    await run()
    console.log(`Deleted ${what}.`)
  } catch (e) {
    failures.push(`${what}: ${(e as Error).message}`)
  }
}

if (state.session_id) {
  await attempt(`session ${state.session_id}`, () =>
    client.beta.sessions.delete(state.session_id)
  )
}
if (state.sandbox_group_id) {
  const group = state.sandbox_group_id
  // Delete the staged snapshot by ID, then every snapshot saved for this session.
  // The list is newest first, 100 at a time, so read every page.
  const ids = new Set<string>(
    state.staged_snapshot_id ? [state.staged_snapshot_id] : []
  )
  for (let cursor: string | undefined; ; ) {
    const page = await sandboxes.snapshots.list({
      sandboxGroupId: group,
      cursor,
      limit: 100,
    })
    for (const { snapshot } of page) {
      if (snapshot.name === `claude-session-${state.session_id}`)
        ids.add(snapshot.id)
    }
    if (page.length < 100) break
    cursor = page[page.length - 1].cursor
  }
  for (const snapshotId of ids) {
    await attempt(`snapshot ${snapshotId}`, () =>
      sandboxes.snapshots.delete({ sandboxGroupId: group, snapshotId })
    )
  }
}
if (state.created_agent) {
  await attempt(`agent ${state.agent_id} (archived)`, () =>
    client.beta.agents.archive(state.agent_id)
  )
}
if (failures.length) {
  console.error(`Retry these, then run cleanup again:\n${failures.join('\n')}`)
  process.exit(1)
}
