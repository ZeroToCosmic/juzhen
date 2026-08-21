const assert = require("node:assert/strict");
const test = require("node:test");

const { captureScreen } = require("../browser/screen");

test("captureScreen takes a jpeg screenshot at quality 60 and returns base64", async () => {
  const screenshotBuffer = Buffer.from("fake jpeg bytes");
  const calls = [];
  const page = {
    screenshot: async (options) => {
      calls.push(options);
      return screenshotBuffer;
    },
  };

  const result = await captureScreen(page);

  assert.deepEqual(calls, [{ type: "jpeg", quality: 60 }]);
  assert.equal(result, screenshotBuffer.toString("base64"));
});
