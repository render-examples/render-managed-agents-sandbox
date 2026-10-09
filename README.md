# Claude Managed Agents on Render Sandboxes

Run a Claude Managed Agent's tool calls in Render Sandboxes, one fresh sandbox per session.
The orchestrator comes in TypeScript and Python. Both versions behave the same and pass the same tests.

- **Tutorial:** [Run Claude Managed Agents in Render Sandboxes](https://render.com/docs/sandboxes-claude-managed-agents)
  deploys the orchestrator, has an agent fix a program, and verifies the result. The `starter/` directory holds its helpers.
- **Reference:** [GUIDE.md](GUIDE.md) covers staging files, persistence, network rules, settings, security, and troubleshooting.
- **Deploy (TypeScript):** use [Deploy to Render](https://render.com/deploy?repo=https://github.com/render-examples/render-managed-agents-sandbox/tree/deploy-typescript). This branch puts the TypeScript Blueprint at the repository root for the button.
- **Deploy (Python):** use [Deploy to Render](https://render.com/deploy?repo=https://github.com/render-examples/render-managed-agents-sandbox) from the default branch.
- **Test (TypeScript):** `cd orchestrator-ts && npm test`.
- **Test (Python):** `pytest tests/test_orchestrator.py`, then `pytest -m live tests/test_render_live.py`
  with Render credentials, then `python scripts/e2e.py` with Anthropic credentials.
- **Real-session checks:** `python scripts/validate.py` runs real Claude sessions through the Python
  orchestrator. Set `ORCHESTRATOR=ts` to run them through the TypeScript one.

```
orchestrator-ts/      TypeScript orchestrator, its Blueprint, and offline tests
orchestrator/         Python orchestrator (Render background worker)
sandbox/              bootstrap script that runs inside each sandbox (shared by both)
scripts/              Python snapshot builder, end-to-end test, and real-session checks
starter/              the tutorial's Python and TypeScript helpers, task fixture, and verifier
examples/typescript/  create an environment and agent, and run a first session
tests/                Python offline tests and live Render tests
```
