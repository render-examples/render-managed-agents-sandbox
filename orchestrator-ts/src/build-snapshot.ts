// Build a sandbox snapshot with Anthropic's SDK preinstalled. Mirrors scripts/build_snapshot.py.
//
// The orchestrator works without it (the startup script installs the SDK in about six
// seconds), but a snapshot removes the install from every session start. Snapshots expire after three
// days, so render.yaml runs this daily as a cron job.
import { Render } from "@renderinc/sdk";
import { runCmd } from "./launcher.js";

const NAME = process.env.SANDBOX_SNAPSHOT_NAME || "claude-worker";
const SDK_VERSION = process.env.ANTHROPIC_SDK_VERSION || "1.12.1";
// Add your agent's toolchain here (apt packages, language runtimes, repos).
const EXTRA_SETUP = process.env.SNAPSHOT_EXTRA_SETUP ?? "";

const SETUP = `set -euo pipefail
python3 -m venv /opt/claude-runner
/opt/claude-runner/bin/pip install --quiet --disable-pip-version-check "anthropic==${SDK_VERSION}"
mkdir -p /workspace
${EXTRA_SETUP}
/opt/claude-runner/bin/python -c 'import anthropic; print("anthropic", anthropic.__version__)'`;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

const sandboxes = new Render().experimental.sandboxes;
const sb = await sandboxes.create({ timeoutSeconds: 900 });
try {
  for (let i = 0; ; i++) {
    const { status } = await sandboxes.get(sb.id);
    if (status === "running") break;
    if (status === "errored" || status === "terminated" || i > 200) throw new Error(`sandbox is ${status}`);
    await sleep(1500);
  }
  const [code, out] = await runCmd(sandboxes, sb.id, SETUP);
  console.log(out.trim());
  if (code !== 0) throw new Error(`setup failed with exit code ${code}`);
  let snap = await sandboxes.snapshots.create({ sandboxId: sb.id, name: NAME });
  for (let i = 0; snap.status === "creating"; i++) {
    if (i > 200) throw new Error("snapshot not available in time");
    await sleep(1500);
    snap = await sandboxes.snapshots.get({ sandboxGroupId: snap.sandboxGroupId, snapshotId: snap.id });
  }
  if (snap.status !== "available") throw new Error(`snapshot is ${snap.status}: ${snap.error ?? ""}`);
  console.log(`snapshot ${snap.id} name=${NAME} available, expires ${snap.expiresAt}`);
} finally {
  await sandboxes.terminate(sb.id);
}
