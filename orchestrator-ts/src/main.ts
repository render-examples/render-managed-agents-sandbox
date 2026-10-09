// Entry point for the TypeScript orchestrator. Mirrors orchestrator/main.py.
//
// Run it anywhere with outbound HTTPS. On Render, deploy it as a background worker
// (see render.yaml). It holds the environment key and the Render API key; the
// sandboxes it creates hold neither.
import Anthropic from "@anthropic-ai/sdk";
import { Render } from "@renderinc/sdk";
import { settingsFromEnv } from "./config.js";
import { log } from "./log.js";
import { Orchestrator } from "./orchestrator.js";

async function main() {
  const settings = settingsFromEnv();
  // Polls block for at most a second, so a short timeout keeps one stalled connection from
  // freezing the queue for the SDK's default 10 minutes. The poller inherits it.
  const anthropic = new Anthropic({
    apiKey: null,
    authToken: settings.environmentKey,
    baseURL: settings.anthropicBaseUrl,
    timeout: 60_000,
  });
  const orch = new Orchestrator(settings, anthropic, new Render().experimental.sandboxes);
  let exitCode = 0;
  const stop = () => void orch.shutdown().then(() => process.exit(exitCode));
  process.once("SIGINT", stop);
  process.once("SIGTERM", stop);
  try {
    await orch.run();
  } catch (e) {
    if (e instanceof Anthropic.AuthenticationError || e instanceof Anthropic.PermissionDeniedError) {
      log.error(`Anthropic rejected the environment key (${e.status}). Check ANTHROPIC_ENVIRONMENT_KEY ` +
        "and ANTHROPIC_ENVIRONMENT_ID, or generate a new key in the Console.");
    } else if (!(e instanceof Error && e.name === "AbortError")) {
      log.error(`orchestrator stopped: ${(e as Error).stack ?? e}`);
    }
    exitCode = 1;
  }
  await orch.shutdown();
  process.exit(exitCode);
}

await main();
