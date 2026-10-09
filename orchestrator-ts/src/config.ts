// Settings for the orchestrator, read from environment variables. Mirrors orchestrator/config.py.

// The worker needs Anthropic's API, and PyPI when the snapshot is missing and it installs the SDK.
export const DEFAULT_ALLOWED_DOMAINS = "api.anthropic.com,pypi.org,files.pythonhosted.org";

export type NetworkPolicy = "allow-all" | "allow-list" | "deny-all";
export type Plan = "starter" | "standard" | "pro";

export interface Settings {
  environmentId: string;
  environmentKey: string;
  anthropicBaseUrl?: string;
  sandboxTimeoutSeconds: number;
  maxConcurrentSessions: number;
  snapshotName?: string;
  networkPolicy: NetworkPolicy;
  allowedDomains: string[];
  sdkVersion: string;
  workerMaxIdle: string;
  keepSandboxes: boolean;
  forwardEnvironmentKey: boolean;
  readyTimeoutSeconds: number;
  sessionPersistence: boolean;
  persistenceKind: "filesystem" | "runtime";
  sandboxPlan: Plan;
}

const bool = (name: string) => ["1", "true", "yes", "on"].includes((process.env[name] ?? "").trim().toLowerCase());

export function settingsFromEnv(env = process.env): Settings {
  const missing = ["ANTHROPIC_ENVIRONMENT_ID", "ANTHROPIC_ENVIRONMENT_KEY", "RENDER_API_KEY", "RENDER_WORKSPACE_ID"]
    .filter((n) => !env[n]);
  if (missing.length) throw new Error(`missing required environment variables: ${missing.join(", ")}`);
  const policy = (env.SANDBOX_NETWORK_POLICY ?? "allow-list") as NetworkPolicy;
  if (!["allow-all", "allow-list", "deny-all"].includes(policy)) {
    throw new Error("SANDBOX_NETWORK_POLICY must be allow-all, allow-list, or deny-all");
  }
  const persistence = (env.SESSION_PERSISTENCE ?? "snapshot").toLowerCase();
  return {
    environmentId: env.ANTHROPIC_ENVIRONMENT_ID!,
    environmentKey: env.ANTHROPIC_ENVIRONMENT_KEY!,
    anthropicBaseUrl: env.ANTHROPIC_BASE_URL || undefined,
    sandboxTimeoutSeconds: Number(env.SANDBOX_TIMEOUT_SECONDS ?? 3600),
    maxConcurrentSessions: Number(env.MAX_CONCURRENT_SESSIONS ?? 20),
    snapshotName: env.SANDBOX_SNAPSHOT_NAME || undefined,
    networkPolicy: policy,
    allowedDomains: (env.SANDBOX_ALLOWED_DOMAINS ?? DEFAULT_ALLOWED_DOMAINS).split(",").map((d) => d.trim()).filter(Boolean),
    sdkVersion: env.ANTHROPIC_SDK_VERSION ?? "1.12.1",
    workerMaxIdle: env.WORKER_MAX_IDLE ?? "60s",
    keepSandboxes: bool("KEEP_SANDBOXES"),
    forwardEnvironmentKey: bool("FORWARD_ENVIRONMENT_KEY"),
    readyTimeoutSeconds: 90,
    sessionPersistence: persistence !== "none",
    persistenceKind: persistence === "runtime" ? "runtime" : "filesystem",
    sandboxPlan: (env.SANDBOX_PLAN ?? "starter") as Plan,
  };
}
