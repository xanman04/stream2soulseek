import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

test("Wrangler keeps logs enabled without invocation payloads and disables URL-bearing traces", async () => {
  const source = await readFile(new URL("../wrangler.jsonc", import.meta.url), "utf8");
  const config = JSON.parse(source);
  assert.equal(config.observability.enabled, true);
  assert.equal(config.observability.logs.enabled, true);
  assert.equal(config.observability.logs.invocation_logs, false);
  assert.equal(config.observability.traces.enabled, false);
  assert.equal(config.observability.redact_query_string, undefined);
});
