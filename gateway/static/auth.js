(function (root, factory) {
  "use strict";
  const exported = factory(root);
  if (typeof module === "object" && module.exports) module.exports = exported;
  if (root && root.document) {
    let controller = null;
    root.AuthUI = {
      createAuthUI: exported.createAuthUI,
      init: function () {
        if (!controller) {
          controller = exported.createAuthUI(
            exported.browserDependencies(root),
          );
        }
        return controller.init();
      },
    };
    if (root.document.readyState === "loading") {
      root.document.addEventListener("DOMContentLoaded", root.AuthUI.init);
    } else {
      root.AuthUI.init();
    }
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const LOGIN_ERROR = "用户名或密码无效，或账号暂时不可用。";
  const PASSWORD_CHANGE_ERROR = "密码修改失败，请检查后重试。";

  function createAuthUI(dependencies) {
    const deps = dependencies || {};
    let initialized = false;

    function setMessage(node, message) {
      if (node && typeof deps.setText === "function") {
        deps.setText(node, message);
      }
    }

    function clearPasswordFields(elements) {
      elements.currentPassword.value = "";
      elements.newPassword.value = "";
      elements.confirmPassword.value = "";
    }

    function request(url, body) {
      return deps.requestJson(url, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": deps.csrfToken(),
        },
        body: JSON.stringify(body),
      });
    }

    async function login(username, password, errorNode) {
      setMessage(errorNode, "");
      try {
        const result = await request("/api/auth/login", {
          username,
          password,
        });
        if (!result || result.status !== 200) {
          setMessage(errorNode, LOGIN_ERROR);
          return false;
        }
        if (
          result.data
          && typeof result.data.csrf_token === "string"
          && result.data.csrf_token
          && typeof deps.setCsrfToken === "function"
        ) {
          deps.setCsrfToken(result.data.csrf_token);
        }
        deps.navigate(
          result.data && result.data.must_change_password
            ? "password-change"
            : "/",
        );
        return true;
      } catch (_error) {
        setMessage(errorNode, LOGIN_ERROR);
        return false;
      }
    }

    async function changePassword(
      currentPassword,
      newPassword,
      errorNode,
    ) {
      setMessage(errorNode, "");
      try {
        const result = await request("/api/auth/change-password", {
          current_password: currentPassword,
          new_password: newPassword,
        });
        if (!result || result.status !== 200) {
          setMessage(errorNode, PASSWORD_CHANGE_ERROR);
          return false;
        }
        deps.navigate("/login");
        return true;
      } catch (_error) {
        setMessage(errorNode, PASSWORD_CHANGE_ERROR);
        return false;
      }
    }

    function init() {
      if (initialized || !deps.elements) return;
      initialized = true;
      const elements = deps.elements();
      if (!elements) return;

      elements.loginForm.addEventListener("submit", async function (event) {
        event.preventDefault();
        elements.loginButton.disabled = true;
        try {
          await login(
            elements.username.value,
            elements.password.value,
            elements.loginError,
          );
        } finally {
          elements.password.value = "";
          elements.loginButton.disabled = false;
        }
      });

      elements.passwordChangeForm.addEventListener(
        "submit",
        async function (event) {
          event.preventDefault();
          if (elements.newPassword.value !== elements.confirmPassword.value) {
            setMessage(elements.passwordChangeError, PASSWORD_CHANGE_ERROR);
            clearPasswordFields(elements);
            elements.newPassword.focus();
            return;
          }
          elements.passwordChangeButton.disabled = true;
          try {
            await changePassword(
              elements.currentPassword.value,
              elements.newPassword.value,
              elements.passwordChangeError,
            );
          } finally {
            clearPasswordFields(elements);
            elements.passwordChangeButton.disabled = false;
          }
        },
      );
    }

    return {changePassword, init, login};
  }

  function browserDependencies(browserRoot) {
    const document = browserRoot.document;
    return {
      requestJson: async function (url, options) {
        const response = await browserRoot.fetch(url, options);
        let data = {};
        try {
          data = await response.json();
        } catch (_error) {
          data = {};
        }
        return {status: response.status, data};
      },
      csrfToken: function () {
        const input = document.querySelector('[name="csrf_token"]');
        return input ? input.value : "";
      },
      setCsrfToken: function (value) {
        const input = document.querySelector('[name="csrf_token"]');
        if (input) input.value = value;
      },
      navigate: function (destination) {
        if (destination === "password-change") {
          const loginView = document.getElementById("login-view");
          const passwordChangeView = document.getElementById(
            "password-change-view",
          );
          loginView.hidden = true;
          passwordChangeView.hidden = false;
          document.getElementById("current-password").focus();
          return;
        }
        browserRoot.location.assign(destination);
      },
      setText: function (node, value) {
        node.textContent = value;
      },
      elements: function () {
        return {
          loginForm: document.getElementById("login-form"),
          username: document.getElementById("username"),
          password: document.getElementById("password"),
          loginError: document.getElementById("login-error"),
          loginButton: document.getElementById("login-button"),
          passwordChangeForm: document.getElementById(
            "password-change-form",
          ),
          currentPassword: document.getElementById("current-password"),
          newPassword: document.getElementById("new-password"),
          confirmPassword: document.getElementById("confirm-password"),
          passwordChangeError: document.getElementById(
            "password-change-error",
          ),
          passwordChangeButton: document.getElementById(
            "password-change-button",
          ),
        };
      },
    };
  }

  return {browserDependencies, createAuthUI};
});
