const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {createAuthUI} = require("../gateway/static/auth");

const LOGIN_ERROR = "用户名或密码无效，或账号暂时不可用。";

function harness(response) {
  const calls = [];
  const destinations = [];
  let csrf = "csrf-1";
  const ui = createAuthUI({
    requestJson: async (url, options) => {
      calls.push({url, options});
      if (response instanceof Error) throw response;
      return typeof response === "function"
        ? response(url, options)
        : response;
    },
    csrfToken: () => csrf,
    setCsrfToken: (value) => {
      csrf = value;
    },
    navigate: (destination) => destinations.push(destination),
    setText: (node, value) => {
      node.textContent = value;
    },
  });
  return {calls, destinations, ui};
}

test("login sends csrf and replaces no server text as html", async () => {
  const {calls, destinations, ui} = harness({
    status: 200,
    data: {must_change_password: false},
  });
  const errorNode = {textContent: "old error"};

  await ui.login("admin", "password", errorNode);

  assert.equal(calls[0].url, "/api/auth/login");
  assert.equal(calls[0].options.headers["X-CSRF-Token"], "csrf-1");
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    username: "admin",
    password: "password",
  });
  assert.equal(errorNode.textContent, "");
  assert.deepEqual(destinations, ["/"]);
});

test("every login failure uses one generic message", async () => {
  for (const response of [
    {status: 401, data: {error: "<img src=x onerror=alert(1)>"}},
    {status: 423, data: {error: "account locked"}},
    new Error("network details"),
  ]) {
    const {destinations, ui} = harness(response);
    const errorNode = {textContent: ""};

    const succeeded = await ui.login("unknown", "bad", errorNode);

    assert.equal(succeeded, false);
    assert.equal(errorNode.textContent, LOGIN_ERROR);
    assert.deepEqual(destinations, []);
  }
});

test("must-change login navigates only to password-change view", async () => {
  const {calls, destinations, ui} = harness((url) => {
    if (url === "/api/auth/login") {
      return {
        status: 200,
        data: {
          must_change_password: true,
          csrf_token: "csrf-rotated",
        },
      };
    }
    return {status: 200, data: {login_required: true}};
  });

  const succeeded = await ui.login(
    "temporary",
    "one-time password",
    {textContent: ""},
  );
  await ui.changePassword(
    "one-time password",
    "replacement password 456",
    {textContent: ""},
  );

  assert.equal(succeeded, true);
  assert.deepEqual(destinations, ["password-change", "/login"]);
  assert.equal(
    calls[1].options.headers["X-CSRF-Token"],
    "csrf-rotated",
  );
});

test("password change sends csrf and returns to login without retaining secrets", async () => {
  const {calls, destinations, ui} = harness({
    status: 200,
    data: {login_required: true},
  });
  const errorNode = {textContent: ""};

  const succeeded = await ui.changePassword(
    "one-time password",
    "replacement password 456",
    errorNode,
  );

  assert.equal(succeeded, true);
  assert.equal(calls[0].url, "/api/auth/change-password");
  assert.equal(calls[0].options.headers["X-CSRF-Token"], "csrf-1");
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    current_password: "one-time password",
    new_password: "replacement password 456",
  });
  assert.equal(errorNode.textContent, "");
  assert.deepEqual(destinations, ["/login"]);
  assert.deepEqual(Object.keys(ui).sort(), ["changePassword", "init", "login"]);
});

test("auth controller never uses HTML injection or browser storage", () => {
  const source = fs.readFileSync(
    path.join(__dirname, "../gateway/static/auth.js"),
    "utf8",
  );

  assert.doesNotMatch(source, /\.innerHTML\b/);
  assert.doesNotMatch(source, /\b(?:localStorage|sessionStorage)\b/);
});

test("password mismatch clears current, new, and confirmation fields", async () => {
  const handlers = {};
  const field = (value = "") => ({
    value,
    focus: () => {},
  });
  const elements = {
    loginForm: {
      addEventListener: (name, handler) => {
        handlers[`login-${name}`] = handler;
      },
    },
    username: field(),
    password: field(),
    loginError: {textContent: ""},
    loginButton: {disabled: false},
    passwordChangeForm: {
      addEventListener: (name, handler) => {
        handlers[`change-${name}`] = handler;
      },
    },
    currentPassword: field("temporary password"),
    newPassword: field("replacement password 456"),
    confirmPassword: field("different password 789"),
    passwordChangeError: {textContent: ""},
    passwordChangeButton: {disabled: false},
  };
  const ui = createAuthUI({
    elements: () => elements,
    requestJson: async () => {
      throw new Error("mismatch must not request");
    },
    csrfToken: () => "csrf",
    navigate: () => {},
    setText: (node, value) => {
      node.textContent = value;
    },
  });
  ui.init();

  await handlers["change-submit"]({preventDefault: () => {}});

  assert.equal(elements.currentPassword.value, "");
  assert.equal(elements.newPassword.value, "");
  assert.equal(elements.confirmPassword.value, "");
});
