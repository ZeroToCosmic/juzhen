(function checksumModule(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.ActionChecksums = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function createApi() {
  "use strict";

  const CONTENT_FIELDS = [
    "executor_kind",
    "definition_schema_version",
    "parameter_schema",
    "result_schema",
    "snapshot",
    "execution_defaults",
  ];
  const ACTION_ID = /^act_[0-7][0-9A-HJKMNP-TV-Z]{25}$/;
  const CHECKSUM = /^sha256:[0-9a-f]{64}$/;

  function assertUnicode(value) {
    for (let index = 0; index < value.length; index += 1) {
      const unit = value.charCodeAt(index);
      if (unit >= 0xd800 && unit <= 0xdbff) {
        const next = value.charCodeAt(index + 1);
        if (!(next >= 0xdc00 && next <= 0xdfff)) {
          throw new TypeError("lone high surrogate is not valid Unicode");
        }
        index += 1;
      } else if (unit >= 0xdc00 && unit <= 0xdfff) {
        throw new TypeError("lone low surrogate is not valid Unicode");
      }
    }
  }

  function canonicalize(value) {
    if (value === null || typeof value === "boolean") {
      return JSON.stringify(value);
    }
    if (typeof value === "string") {
      assertUnicode(value);
      return JSON.stringify(value);
    }
    if (typeof value === "number") {
      if (!Number.isFinite(value)) {
        throw new TypeError("non-finite numbers are not valid RFC 8785 JSON");
      }
      if (Number.isInteger(value) && !Number.isSafeInteger(value)) {
        throw new TypeError("unsafe integers are not valid cross-language JCS input");
      }
      return JSON.stringify(value);
    }
    if (Array.isArray(value)) {
      if (Object.keys(value).length !== value.length) {
        throw new TypeError("sparse or extended arrays are not JSON values");
      }
      const items = [];
      for (let index = 0; index < value.length; index += 1) {
        if (!Object.prototype.hasOwnProperty.call(value, index)) {
          throw new TypeError("sparse arrays are not JSON values");
        }
        items.push(canonicalize(value[index]));
      }
      return `[${items.join(",")}]`;
    }
    if (typeof value === "object") {
      const prototype = Object.getPrototypeOf(value);
      if (prototype !== Object.prototype && prototype !== null) {
        throw new TypeError("only plain JSON objects can be canonicalized");
      }
      if (Object.getOwnPropertySymbols(value).length) {
        throw new TypeError("symbol properties are not JSON values");
      }
      const keys = Object.keys(value).sort();
      return `{${keys.map((key) => {
        assertUnicode(key);
        const item = value[key];
        if (item === undefined || typeof item === "function" || typeof item === "symbol") {
          throw new TypeError("object contains a non-JSON value");
        }
        return `${JSON.stringify(key)}:${canonicalize(item)}`;
      }).join(",")}}`;
    }
    throw new TypeError("value is not JSON-compatible");
  }

  async function sha256(document) {
    const bytes = new TextEncoder().encode(canonicalize(document));
    let subtle = globalThis.crypto && globalThis.crypto.subtle;
    if (!subtle && typeof require === "function") {
      subtle = require("node:crypto").webcrypto.subtle;
    }
    if (!subtle) {
      throw new Error("Web Crypto API is unavailable");
    }
    const digest = await subtle.digest("SHA-256", bytes);
    const hex = Array.from(new Uint8Array(digest), (byte) =>
      byte.toString(16).padStart(2, "0")
    ).join("");
    return `sha256:${hex}`;
  }

  function contentProjection(release) {
    const projected = {};
    for (const field of CONTENT_FIELDS) {
      if (!Object.prototype.hasOwnProperty.call(release, field)) {
        throw new TypeError(`action content is missing ${field}`);
      }
      projected[field] = release[field];
    }
    return projected;
  }

  async function contentChecksum(release) {
    return sha256(contentProjection(release));
  }

  async function releaseChecksum(input) {
    if (!ACTION_ID.test(input.action_id)) {
      throw new TypeError("invalid action_id");
    }
    if (!Number.isSafeInteger(input.revision) || input.revision < 1) {
      throw new TypeError("revision must be a positive safe integer");
    }
    if (!CHECKSUM.test(input.content_checksum)) {
      throw new TypeError("invalid content_checksum");
    }
    return sha256({
      action_id: input.action_id,
      revision: input.revision,
      content_checksum: input.content_checksum,
    });
  }

  return {
    canonicalize,
    contentChecksum,
    contentProjection,
    releaseChecksum,
  };
});
