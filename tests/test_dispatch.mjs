import assert from "node:assert/strict";
import test from "node:test";

import { config, dispatchWorkflow } from "../netlify/functions/dispatch-job-watch.mjs";

const ENV = {
  GITHUB_ACTIONS_TOKEN: "test-token",
  GITHUB_OWNER: "example-owner",
  GITHUB_REPOSITORY: "example-repository",
  GITHUB_WORKFLOW: "watch.yml",
  GITHUB_REF: "main",
};

test("the dispatcher calls the selected private-repository workflow", async () => {
  let request;
  const result = await dispatchWorkflow({
    env: ENV,
    fetchImpl: async (url, options) => {
      request = { url, options };
      return new Response(null, { status: 204 });
    },
  });

  assert.equal(
    request.url,
    "https://api.github.com/repos/example-owner/example-repository/actions/workflows/watch.yml/dispatches",
  );
  assert.equal(request.options.method, "POST");
  assert.equal(request.options.headers.Authorization, "Bearer test-token");
  assert.deepEqual(JSON.parse(request.options.body), { ref: "main" });
  assert.equal(result.dispatched, true);
});

test("the dispatcher fails closed when the token is missing", async () => {
  await assert.rejects(
    dispatchWorkflow({ env: {}, fetchImpl: async () => new Response(null, { status: 204 }) }),
    /GITHUB_ACTIONS_TOKEN is not configured/,
  );
});

test("the dispatcher surfaces GitHub API failures without exposing the token", async () => {
  await assert.rejects(
    dispatchWorkflow({
      env: ENV,
      fetchImpl: async () => new Response("forbidden", { status: 403 }),
    }),
    (error) => {
      assert.match(error.message, /403.*forbidden/);
      assert.doesNotMatch(error.message, /test-token/);
      return true;
    },
  );
});

test("the schedule runs every five minutes at an offset", () => {
  assert.equal(config.schedule, "2,7,12,17,22,27,32,37,42,47,52,57 * * * *");
});
