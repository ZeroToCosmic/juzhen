(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root && root.document && root.document.querySelector("#console-page-elements")) api.boot(root);
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function boot(browserRoot) {
    const document = browserRoot.document;
    const statusNode = document.querySelector("#page-elements-status");
    const setMessage = (message) => { if (statusNode) statusNode.textContent = message.text || ""; if (statusNode) statusNode.classList.toggle("error", Boolean(message.error)); };
    const requestJson = async function (url, method, body) {
      const response = await browserRoot.fetch(url, {
        method: method || "GET",
        headers: body === undefined ? {} : {"Content-Type": "application/json"},
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "same-origin",
      });
      let data = {};
      try { data = await response.json(); } catch (_) { data = {error: "服务返回格式错误"}; }
      return {status: response.status, data};
    };
    const controller = browserRoot.PageElementsController.createPageElementsController({
      root: document,
      requestJson,
      setTimeout: browserRoot.setTimeout.bind(browserRoot),
      clearTimeout: browserRoot.clearTimeout.bind(browserRoot),
      addBeforeUnload: (handler) => browserRoot.addEventListener("beforeunload", handler),
      removeBeforeUnload: (handler) => browserRoot.removeEventListener("beforeunload", handler),
      confirm: browserRoot.confirm.bind(browserRoot),
      prompt: browserRoot.prompt.bind(browserRoot),
      onMessage: setMessage,
    });
    const host = document.querySelector("#console-page-elements");
    if (host) host.pageElementsController = controller;
    controller.init().catch(() => setMessage({text: "元素库加载失败，请稍后重试。", error: true}));
    return controller;
  }

  return {boot};
});
