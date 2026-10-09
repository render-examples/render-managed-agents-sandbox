// biome-ignore-all lint/suspicious/noConsole: command-line script that reports progress
// Create an agent (unless AGENT_ID is set) and a session that starts from the staged snapshot.
import { readFileSync, writeFileSync } from 'node:fs'
import Anthropic from '@anthropic-ai/sdk'

const environmentId = process.env.ANTHROPIC_ENVIRONMENT_ID
if (!environmentId) throw new Error('Set ANTHROPIC_ENVIRONMENT_ID first.')
const state = JSON.parse(readFileSync('session.json', 'utf8'))
const client = new Anthropic()

let agentId = process.env.AGENT_ID
const createdAgent = !agentId
if (!agentId) {
  const agent = await client.beta.agents.create({
    name: 'render-sandbox-tutorial',
    model: 'claude-opus-5-5',
    tools: [{ type: 'agent_toolset_20260401' }],
  })
  agentId = agent.id
}
// The orchestrator reads render_snapshot_id when it claims the session.
const session = await client.beta.sessions.create({
  agent: agentId,
  environment_id: environmentId,
  metadata: { render_snapshot_id: state.staged_snapshot_id },
})
writeFileSync(
  'session.json',
  JSON.stringify(
    {
      ...state,
      agent_id: agentId,
      created_agent: createdAgent,
      session_id: session.id,
    },
    null,
    2
  )
)
console.log(`Created session ${session.id}. Saved session.json.`)
