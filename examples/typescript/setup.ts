// Create a self-hosted environment and an agent. The Python equivalents are in GUIDE.md.
import Anthropic from "@anthropic-ai/sdk";

const client = new Anthropic();

const env = await client.beta.environments.create({
  name: "render-sandboxes",
  config: { type: "self_hosted" },
});
console.log(env.id); // env_...

const agent = await client.beta.agents.create({
  name: "render-sandbox-agent",
  model: "claude-opus-5-5",
  tools: [{ type: "agent_toolset_20260401" }],
});
console.log(agent.id); // agent_...
