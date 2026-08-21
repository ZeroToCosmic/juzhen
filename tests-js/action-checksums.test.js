"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const api = require("../gateway/static/action_checksums.js");

const fixturePath = path.join(
  __dirname,
  "..",
  "tests",
  "fixtures",
  "remote_actions",
  "checksum_vectors.json",
);
const vectors = JSON.parse(fs.readFileSync(fixturePath, "utf8")).vectors;

test("checksum fixture contains fifty independent vectors", () => {
  assert.equal(vectors.length, 50);
});

test("checksum vectors match JavaScript", async () => {
  for (const vector of vectors) {
    assert.equal(
      await api.contentChecksum(vector.content_input),
      vector.content_checksum,
    );
    assert.equal(
      await api.releaseChecksum(vector.release_input),
      vector.release_checksum,
    );
  }
});

test("canonical JSON rejects non-finite numbers and lone surrogates", () => {
  assert.throws(() => api.canonicalize({ value: Number.NaN }));
  assert.throws(() => api.canonicalize({ value: Number.POSITIVE_INFINITY }));
  assert.throws(() => api.canonicalize({ value: "\ud800" }));
  assert.throws(() => api.canonicalize({ value: 2 ** 53 }));
  assert.doesNotThrow(() => api.canonicalize({ value: 2 ** 53 - 1 }));
});

test("canonical JSON rejects non-JSON object and array shapes", () => {
  const sparse = [];
  sparse[1] = 1;
  assert.throws(() => api.canonicalize(sparse));
  assert.throws(() => api.canonicalize(new Date("2026-08-20T00:00:00.000Z")));
  const extended = [1];
  extended.extra = true;
  assert.throws(() => api.canonicalize(extended));
  assert.throws(() => api.canonicalize(new Set(["not-json"])));
});

test("content checksum excludes display metadata", async () => {
  const base = vectors[0].content_input;
  const decorated = {
    ...base,
    action_id: "act_00000000000000000000000000",
    revision: 99,
    name: "display only",
  };
  assert.equal(await api.contentChecksum(decorated), vectors[0].content_checksum);
});

test("release checksum rejects ULIDs above the 128-bit range", async () => {
  await assert.rejects(() =>
    api.releaseChecksum({
      action_id: `act_${"Z".repeat(26)}`,
      revision: 1,
      content_checksum: vectors[0].content_checksum,
    }),
  );
});
