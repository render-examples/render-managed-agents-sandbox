# Claude Managed Agents on Render: starter

Follow the guide at https://render.com/docs/sandboxes-claude-managed-agents
(or the same route on the local website preview before publication).

These helpers run on your machine. Choose `typescript` or `python`; both do the same
thing and give the agent the same JavaScript task. The program in `fixture/` is broken
on purpose. The agent must repair it.

The orchestrator you deploy from this repository's Blueprint does the Render side: it
claims each session and runs Anthropic's SDK worker in a Render sandbox. These helpers
never talk to the orchestrator. They stage the task files, create the session, send the
task, wait for the result, and clean up.

Keep the Claude API key on your machine. The environment key belongs only on the
orchestrator. Keep `session.json` until cleanup succeeds.

| Helper | What it does |
| --- | --- |
| `stage_task` | Copies `fixture/` into a temporary sandbox, saves it as a snapshot, and stops the sandbox. Writes the snapshot ID to `session.json`. |
| `create_session` | Creates an agent (unless `AGENT_ID` is set) and a session whose `render_snapshot_id` metadata names that snapshot. Run it after `stage_task`: the orchestrator claims a session within seconds of its creation. |
| `run_task` | Sends `prompt.txt`, streams the agent's work, then waits for the orchestrator to save the finished sandbox as a new snapshot. |
| `wait_for_sandbox` | Waits up to two minutes for the verification sandbox in `SANDBOX_ID` to reach `running`. |
| `cleanup` | Deletes the session, the staged and saved snapshots, and the agent if `create_session` made it. |

The guide restores the original tests and input in a fresh sandbox, runs the tests,
verifies the JSON result with `verify.mjs`, and downloads the output. Agent text and a
completed turn alone do not establish success.
