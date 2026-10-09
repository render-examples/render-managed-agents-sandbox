// biome-ignore-all lint/suspicious/noConsole: command-line script that reports progress
// Wait up to two minutes for the sandbox in SANDBOX_ID to reach running.
import { Render } from '@renderinc/sdk'

const sandboxId = process.env.SANDBOX_ID
if (!sandboxId) throw new Error('Set SANDBOX_ID first.')
const sandboxes = new Render().experimental.sandboxes
for (let i = 0; i < 60; i++) {
  const { status } = await sandboxes.get(sandboxId)
  if (status === 'running') {
    console.log('Sandbox is running.')
    process.exit(0)
  }
  if (status === 'errored' || status === 'terminated')
    throw new Error(`${sandboxId} is ${status}.`)
  await new Promise((r) => setTimeout(r, 2000))
}
throw new Error(`${sandboxId} did not reach running. Stop it before retrying.`)
