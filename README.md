# Claude Managed Agents on Render Sandboxes

Run a Claude Managed Agent's tool calls in Render Sandboxes, one fresh sandbox per session.
The orchestrator comes in TypeScript and Python. Both versions run the same session flow.

- **Tutorial:** [Run Claude Managed Agents in Render Sandboxes](https://render.com/docs/sandboxes-claude-managed-agents)
  deploys the orchestrator, has an agent fix a program, and verifies the result. The `starter/` directory holds its helpers.
- **Reference:** [GUIDE.md](GUIDE.md) covers staging files, persistence, network rules, settings, security, and troubleshooting.
- **Deploy (Python):** [Deploy to Render](https://render.com/deploy?repo=https://github.com/render-examples/render-managed-agents-sandbox&path=orchestrator/render.yaml)
  uses `orchestrator/render.yaml`.
- **Deploy (TypeScript):** [Deploy to Render](https://render.com/deploy?repo=https://github.com/render-examples/render-managed-agents-sandbox&path=orchestrator-ts/render.yaml)
  uses `orchestrator-ts/render.yaml`.
- If the **Blueprint Path** field on Render's page shows `render.yaml`, set it to the path above.
  The Blueprints live beside their code, and the repository root has no `render.yaml`.

```
orchestrator-ts/      TypeScript orchestrator and its Blueprint
orchestrator/         Python orchestrator and its Blueprint
sandbox/              bootstrap script that runs inside each sandbox (shared by both)
scripts/              Snapshot builder and session helpers
starter/              the tutorial's Python and TypeScript helpers, task fixture, and verifier
examples/typescript/  create an environment and agent, and run a first session
```
