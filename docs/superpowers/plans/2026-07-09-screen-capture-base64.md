# Screen Capture Base64 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `captureScreen(page)` for taking a JPEG screenshot and returning its Base64 string.

**Architecture:** Keep screenshot utilities in a focused Node module under `browser/`. Test with a fake Playwright page so no live browser is required.

**Tech Stack:** Node.js, Playwright page API, Node built-in `node:test`

## Global Constraints

- Function name is `captureScreen`.
- Input is a Playwright-like `page`.
- Screenshot options are exactly `{ type: "jpeg", quality: 60 }`.
- Return value is a raw Base64 string without a data URL prefix.

---

### Task 1: Capture Screen Utility

**Files:**
- Create: `browser/screen.js`
- Create: `tests-js/screen.test.js`

**Interfaces:**
- Produces: `captureScreen(page: { screenshot(options): Promise<Buffer> }) -> Promise<string>`

**Behavior:**
- Calls `page.screenshot({ type: "jpeg", quality: 60 })`.
- Converts the returned buffer with `buffer.toString("base64")`.
- Returns the Base64 string.
