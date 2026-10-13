# Claude Managed Agents on Render Sandboxes

Run a Claude Managed Agent's tool calls in Render Sandboxes, one fresh sandbox per session.
The orchestrator comes in TypeScript and Python. Both versions run the same session flow.

- **Tutorial:** [Run Claude Managed Agents in Render Sandboxes](https://render.com/docs/sandboxes-claude-managed-agents)
  deploys the orchestrator, has an agent fix a program, and verifies the result. The `starter/` directory holds its helpers.
- **Reference:** [GUIDE.md](GUIDE.md) covers staging files, persistence, network rules, settings, security, and troubleshooting.
- **Deploy (TypeScript):** use [Deploy to Render](https://render.com/deploy?repo=https://github.com/render-examples/render-managed-agents-sandbox/tree/deploy-typescript). This branch puts the TypeScript Blueprint at the repository root for the button.
- **Deploy (Python):** use [Deploy to Render](https://render.com/deploy?repo=https://github.com/render-examples/render-managed-agents-sandbox) from the default branch.

```
orchestrator-ts/      TypeScript orchestrator and its Blueprint
orchestrator/         Python orchestrator (Render background worker)
sandbox/              bootstrap script that runs inside each sandbox (shared by both)
scripts/              Snapshot builder and session helpers
starter/              the tutorial's Python and TypeScript helpers, task fixture, and verifier
examples/typescript/  create an environment and agent, and run a first session
```
