# Workflow Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a guided web console at `/` that lets the user operate the project step by step.

**Architecture:** Serve a single Flask-rendered dashboard with embedded JavaScript that calls the existing JSON APIs. Add `GET /api/status` to report service health and configuration completeness.

**Tech Stack:** Python, Flask, vanilla HTML/CSS/JavaScript

## Global Constraints

- `/` is the primary workflow console.
- `/settings` remains available as the advanced settings page.
- Existing APIs stay compatible: `/api/settings`, `/check_ip`, `/publish/buffer`.
- Browser takeover is represented with the exact Node command until a backend runner exists.

---

### Task 1: Dashboard and Status

**Files:**
- Modify: `gateway/app.py`
- Test: `tests/test_console.py`

**Interfaces:**
- Produces: `GET /`
- Produces: `GET /api/status`

**Behavior:**
- Dashboard includes workflow steps for status, settings, proxy check, Buffer publish, and browser takeover.
- Status API returns `service`, `config`, and `browser` fields.
- Configuration completeness checks proxy host, port, username, password, IPInfo URL, Buffer URL, and CDP URL.
