# Launcher Console Overview Startup Design

## Problem

The Windows launcher currently opens `http://127.0.0.1:5000/`. That legacy dashboard defaults to its old settings panel, so the first screen after startup does not match the new Console UI. Navigating away and returning through the sidebar reaches `/console/settings`, which explains why the settings UI changes after navigation.

## Design

Change the launcher's single `APP_URL` constant to:

```text
http://127.0.0.1:5000/console/overview
```

The existing startup readiness check and the successful-start browser open both continue to use this constant. The launcher therefore verifies and opens the same new Console page.

## Compatibility

- Preserve the legacy root route `/` and its existing UI.
- Do not redirect `/`.
- Do not change Console routes, settings routes, service startup order, retry behavior, or process lifecycle.
- Do not add configuration for choosing a startup page.

## Verification

- Add a launcher regression test that captures the URL passed to `webbrowser.open` after successful startup.
- Assert that the captured URL is exactly `http://127.0.0.1:5000/console/overview`.
- Run the focused launcher restart tests and existing Console page tests.
