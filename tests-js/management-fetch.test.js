const assert = require("node:assert/strict");
const test = require("node:test");

const {
  createManagementFetch,
} = require("../gateway/static/management_fetch");

function harness(status = 200) {
  const calls = [];
  const destinations = [];
  const browserRoot = {
    Headers,
    document: {
      querySelector: (selector) => (
        selector === 'meta[name="csrf-token"]'
          ? {content: "csrf-shared"}
          : null
      ),
    },
    location: {
      href: "https://console.example.test/",
      origin: "https://console.example.test",
      assign: (destination) => destinations.push(destination),
    },
  };
  const fetch = createManagementFetch(
    browserRoot,
    async (input, options) => {
      calls.push({input, options});
      return {status};
    },
  );
  return {calls, destinations, fetch};
}

test("shared fetch adds csrf only to same-origin unsafe requests", async () => {
  const {calls, fetch} = harness();

  await fetch("/api/settings", {method: "PUT"});
  await fetch("https://uploads.example.test/import", {method: "POST"});
  await fetch("/api/settings");

  assert.equal(
    calls[0].options.headers.get("X-CSRF-Token"),
    "csrf-shared",
  );
  assert.equal(calls[1].options.headers, undefined);
  assert.equal(calls[2].options.headers, undefined);
});

test("shared fetch redirects only same-origin 401 responses", async () => {
  const {destinations, fetch} = harness(401);

  await fetch("https://api.example.test/session");
  await fetch("/api/auth/session");

  assert.deepEqual(destinations, ["/login"]);
});

test("cross-origin URL objects receive neither csrf nor login redirect", async () => {
  const {calls, destinations, fetch} = harness(401);

  await fetch(
    new URL("https://uploads.example.test/import"),
    {method: "POST"},
  );

  assert.equal(calls[0].options.headers, undefined);
  assert.deepEqual(destinations, []);
});
