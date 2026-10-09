// biome-ignore-all lint/suspicious/noConsole: command-line script that reports progress
// Save the task files in a Render snapshot. create_session points the session at it, and the
// orchestrator starts the session's sandbox from it, so the agent finds the files in /workspace.
import { readFileSync, writeFileSync } from 'node:fs'
import { Render, SandboxSnapshotNotFoundError } from '@renderinc/sdk'

const sandboxes = new Render().experimental.sandboxes
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))
const options = {
  timeoutSeconds: 900,
  networkPolicy: { default: 'deny-all' as const },
}

// Start from the orchestrator's prepared snapshot, so ant is already installed.
const sandbox = await sandboxes
  .create({ ...options, snapshotName: 'claude-worker' })
  .catch((e) =>
    e instanceof SandboxSnapshotNotFoundError
      ? sandboxes.create(options)
      : Promise.reject(e)
  )
try {
  for (let i = 0; (await sandboxes.get(sandbox.id)).status !== 'running'; i++) {
    if (i >= 120) throw new Error(`Sandbox ${sandbox.id} did not start.`)
    await sleep(1000)
  }
  for (const name of ['orders.csv', 'total.mjs', 'test_total.mjs']) {
    await sandboxes.upload(
      sandbox.id,
      `/workspace/${name}`,
      readFileSync(new URL(`../fixture/${name}`, import.meta.url))
    )
  }
  for await (const event of await sandboxes.exec(
    sandbox.id,
    'mkdir -p /workspace/outputs'
  )) {
    if (event.type === 'exit' && event.exit_code !== 0)
      throw new Error('Could not create /workspace/outputs.')
  }
  let snapshot = await sandboxes.snapshots.create({
    sandboxId: sandbox.id,
    name: `claude-task-${Date.now()}`,
  })
  for (let i = 0; snapshot.status === 'creating'; i++) {
    if (i >= 150) throw new Error(`Snapshot ${snapshot.id} is still creating.`)
    await sleep(2000)
    snapshot = await sandboxes.snapshots.get({
      sandboxGroupId: snapshot.sandboxGroupId,
      snapshotId: snapshot.id,
    })
  }
  if (snapshot.status !== 'available')
    throw new Error(`Snapshot ${snapshot.id} is ${snapshot.status}.`)
  writeFileSync(
    'session.json',
    JSON.stringify(
      {
        sandbox_group_id: snapshot.sandboxGroupId,
        staged_snapshot_id: snapshot.id,
      },
      null,
      2
    )
  )
  console.log(`Staged the task in snapshot ${snapshot.id}.`)
} finally {
  await sandboxes.terminate(sandbox.id)
}
