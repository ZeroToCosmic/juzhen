const assert = require("node:assert/strict");
const test = require("node:test");
const { PassThrough } = require("node:stream");

test("ghost-cursor exposes a path generator", () => {
  assert.equal(typeof require("ghost-cursor").path, "function");
});

test("generatePath passes a target box and normalizes generated points", () => {
  const { generatePath } = require("../browser/ghost-cursor-worker");
  const calls = [];
  const target = { x: 20, y: 30, width: 40, height: 50 };

  const result = generatePath({
    id: "route-1",
    start: { x: 1, y: 2 },
    end: { x: 3, y: 4 },
    target,
  }, (start, destination) => {
    calls.push([start, destination]);
    return [
      { x: 1, y: 2, timestamp: 100 },
      { x: 3, y: 4, extra: "discard" },
    ];
  });

  assert.deepEqual(calls, [[
    { x: 1, y: 2 },
    { x: 3, y: 4, width: target.width, height: target.height },
  ]]);
  assert.deepEqual(result, {
    id: "route-1",
    points: [{ x: 1, y: 2 }, { x: 3, y: 4 }],
  });
});

test("installed ghost-cursor ends a target-sized path at request end", () => {
  const { generatePath } = require("../browser/ghost-cursor-worker");
  const end = { x: 73.25, y: 41.5 };

  const result = generatePath({
    id: "real-target-endpoint",
    start: { x: 2, y: 3 },
    end,
    target: { x: 10, y: 12, width: 30, height: 20 },
  });

  assert.deepEqual(result.points.at(-1), end);
});

test("generatePath rejects malformed input and non-finite generated coordinates", () => {
  const { generatePath } = require("../browser/ghost-cursor-worker");

  assert.throws(
    () => generatePath({ id: "", start: { x: 1, y: 2 }, end: { x: 3, y: 4 } }),
    /id must be a non-empty string/,
  );
  assert.throws(
    () => generatePath({ id: "bad", start: { x: 1, y: 2 }, end: { x: 3, y: 4 } }, () => [
      { x: 1, y: 2 },
      { x: Infinity, y: 4 },
    ]),
    /point 2\.x must be a finite number/,
  );
});

test("generatePath rejects paths with fewer than two points", () => {
  const { generatePath } = require("../browser/ghost-cursor-worker");

  assert.throws(
    () => generatePath({ id: "short", start: { x: 1, y: 2 }, end: { x: 3, y: 4 } }, () => [{ x: 1, y: 2 }]),
    /at least two points/,
  );
});

test("the JSON-lines worker returns an error and continues with the next line", async () => {
  const { startWorker } = require("../browser/ghost-cursor-worker");
  const input = new PassThrough();
  const output = new PassThrough();
  let text = "";
  output.setEncoding("utf8");
  const complete = new Promise((resolve) => {
    output.on("data", (chunk) => {
      text += chunk;
      if (text.split("\n").filter(Boolean).length === 2) resolve();
    });
  });

  startWorker(input, output);
  input.write('{"id":"bad","start":{"x":1,"y":2}}\n');
  input.write('{"id":"good","start":{"x":1,"y":2},"end":{"x":3,"y":4}}\n');
  input.end();

  await complete;
  const responses = text.trim().split("\n").map((line) => JSON.parse(line));

  assert.equal(responses.length, 2);
  assert.equal(responses[0].id, "bad");
  assert.equal(typeof responses[0].error, "string");
  assert.equal(responses[0].error.includes("Error:"), false);
  assert.equal(responses[1].id, "good");
  assert.equal(responses[1].points.length >= 2, true);
});
