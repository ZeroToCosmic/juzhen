const test = require("node:test");
const assert = require("node:assert/strict");

const {
  parseCompactNumber,
  parseSamplerArgs,
  sampleTikTokMetricsFromText,
} = require("../browser/tiktok-sampler");

test("parseCompactNumber handles compact latin and chinese units", () => {
  assert.equal(parseCompactNumber("1.2K"), 1200);
  assert.equal(parseCompactNumber("3.4M"), 3400000);
  assert.equal(parseCompactNumber("5万"), 50000);
  assert.equal(parseCompactNumber("1.5亿"), 150000000);
  assert.equal(parseCompactNumber("987"), 987);
});

test("sampleTikTokMetricsFromText extracts public visible counters", () => {
  const metrics = sampleTikTokMetricsFromText(`
    Likes
    1.2K
    Comments
    34
    Views
    9.8K
  `);

  assert.deepEqual(metrics, {
    likes_24h: 1200,
    comments: 34,
    views_24h: 9800,
  });
});

test("parseSamplerArgs reads profile selector and url", () => {
  assert.deepEqual(
    parseSamplerArgs([
      "node",
      "browser/tiktok-sampler.js",
      "--profile-id",
      "abc",
      "--url",
      "https://www.tiktok.com/@a/video/1",
      "--no-close",
    ]),
    {
      profileId: "abc",
      profileNo: "",
      url: "https://www.tiktok.com/@a/video/1",
      closeAfterRun: false,
    },
  );
});
