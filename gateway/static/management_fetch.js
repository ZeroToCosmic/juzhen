(function (root, factory) {
  "use strict";
  const exported = factory();
  if (typeof module === "object" && module.exports) module.exports = exported;
  if (root && root.document && typeof root.fetch === "function") {
    if (!root.fetch.managementAuthenticated) {
      root.fetch = exported.createManagementFetch(
        root,
        root.fetch.bind(root),
      );
    }
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

  function createManagementFetch(browserRoot, nativeFetch) {
    async function managementFetch(input, options) {
      const requestOptions = options ? {...options} : {};
      const method = String(
        requestOptions.method || (input && input.method) || "GET",
      ).toUpperCase();
      const rawUrl = (
        input && typeof input.url === "string"
          ? input.url
          : String(input)
      );
      const url = new URL(rawUrl, browserRoot.location.href);
      const sameOrigin = url.origin === browserRoot.location.origin;
      if (sameOrigin && UNSAFE_METHODS.has(method)) {
        const headers = new browserRoot.Headers(
          requestOptions.headers || (input && input.headers),
        );
        const tokenNode = browserRoot.document.querySelector(
          'meta[name="csrf-token"]',
        );
        if (tokenNode && tokenNode.content) {
          headers.set("X-CSRF-Token", tokenNode.content);
        }
        requestOptions.headers = headers;
      }
      const response = await nativeFetch(input, requestOptions);
      if (sameOrigin && response.status === 401) {
        browserRoot.location.assign("/login");
      }
      return response;
    }
    managementFetch.managementAuthenticated = true;
    return managementFetch;
  }

  return {createManagementFetch};
});
