#!/usr/bin/env node

const { chromium } = require("playwright");
const { runAgent } = require("./cdp");

runAgent({ chromium }).catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
