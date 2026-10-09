// Start a session on the self-hosted environment and print Claude's reply.
// Usage: AGENT_ID=agent_... ANTHROPIC_ENVIRONMENT_ID=env_... npx tsx first-session.ts ["your task"]
import Anthropic from "@anthropic-ai/sdk";

const client = new Anthropic();
const task = process.argv[2] ?? "Run uname -a and head -1 /etc/os-release, then tell me what you see.";

const session = await client.beta.sessions.create({
  agent: process.env.AGENT_ID!,
  environment_id: process.env.ANTHROPIC_ENVIRONMENT_ID!,
});

const stream = await client.beta.sessions.events.stream(session.id);
await client.beta.sessions.events.send(session.id, {
  events: [{ type: "user.message", content: [{ type: "text", text: task }] }],
});

for await (const event of stream) {
  if (event.type === "agent.message") {
    for (const block of event.content) {
      if (block.type === "text") process.stdout.write(block.text);
    }
  } else if (event.type === "session.status_idle") {
    // requires_action means a tool call is waiting on your worker; keep listening.
    if (event.stop_reason?.type !== "requires_action") break;
  } else if (event.type === "session.error") {
    console.error(`\n[Error: ${event.error?.message ?? "unknown"}]`);
    break;
  }
}
console.log();
