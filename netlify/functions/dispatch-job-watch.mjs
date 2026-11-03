const DEFAULT_OWNER = "arunimshukla";
const DEFAULT_REPOSITORY = "ai-security-job-alert-agent";
const DEFAULT_WORKFLOW = "watch.yml";
const DEFAULT_REF = "main";

function runtimeValue(name) {
  if (globalThis.Netlify?.env?.get) {
    return globalThis.Netlify.env.get(name);
  }
  return process.env[name];
}

export async function dispatchWorkflow({
  fetchImpl = fetch,
  env = {
    GITHUB_ACTIONS_TOKEN: runtimeValue("GITHUB_ACTIONS_TOKEN"),
    GITHUB_OWNER: runtimeValue("GITHUB_OWNER"),
    GITHUB_REPOSITORY: runtimeValue("GITHUB_REPOSITORY"),
    GITHUB_WORKFLOW: runtimeValue("GITHUB_WORKFLOW"),
    GITHUB_REF: runtimeValue("GITHUB_REF"),
  },
} = {}) {
  const token = env.GITHUB_ACTIONS_TOKEN;
  if (!token) {
    throw new Error("GITHUB_ACTIONS_TOKEN is not configured");
  }

  const owner = env.GITHUB_OWNER || DEFAULT_OWNER;
  const repository = env.GITHUB_REPOSITORY || DEFAULT_REPOSITORY;
  const workflow = env.GITHUB_WORKFLOW || DEFAULT_WORKFLOW;
  const ref = env.GITHUB_REF || DEFAULT_REF;
  const endpoint = [
    "https://api.github.com/repos",
    encodeURIComponent(owner),
    encodeURIComponent(repository),
    "actions/workflows",
    encodeURIComponent(workflow),
    "dispatches",
  ].join("/");

  const response = await fetchImpl(endpoint, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      "User-Agent": "ai-security-job-alert-netlify-scheduler",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({ ref }),
  });

  if (!response.ok) {
    const detail = (await response.text()).slice(0, 500);
    throw new Error(`GitHub workflow dispatch failed (${response.status}): ${detail}`);
  }

  return { dispatched: true, owner, repository, workflow, ref };
}

export default async () => {
  const result = await dispatchWorkflow();
  console.log(JSON.stringify({ ...result, dispatched_at: new Date().toISOString() }));
};

// Offset the trigger from the top of the hour and from the GitHub-native fallback.
export const config = {
  schedule: "2,7,12,17,22,27,32,37,42,47,52,57 * * * *",
};
