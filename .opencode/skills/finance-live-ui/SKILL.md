---
name: Finance Live UI
description: Capture or interact with the deployed Finance Dashboard through authenticated Selenium, with explicit approval for live-changing actions.
---

# Finance Dashboard live UI

Use for screenshots, navigation, and browser actions on the deployed Finance Dashboard.

## Screenshot

- Prefer the Home Assistant `homeassistant_screenshot_url` tool for a visual capture. Use the relative path `/10d6f312_custom_finance` (not an absolute URL), and start with a 390 × 844 viewport.
- `hacks/finance-browser.sh` can also authenticate Selenium to the HA frontend using the LLAT from the ignored `.env`. It injects the token into a temporary browser profile, opens the Finance panel through normal Home Assistant Ingress, and does not save credentials.
- Selenium screenshots are written under `FINANCE_TEST_ARTIFACT_DIR` (default `/data/v2/cache/opencode`). Do not commit screenshots or include private financial content in logs or documentation.

## Action-file workflow

Provide a JSON array with at most 20 actions to `hacks/finance-browser.sh --actions-file <file>`:

- Read-only: `wait`, `count`, `screenshot`.
- Potentially mutating: `click`, `fill`, `select`, `keypress`. These are rejected unless the user has approved the actions and the command includes `--apply`.

Actions use CSS selectors, operate only on a unique match, stop on the first failure, and do not execute arbitrary JavaScript. The controller reports action types/status, not page text or entered values. Clicks can still trigger consequential actions such as saving a category, deleting a goal, or starting sync; review the action file before `--apply`, and do not retry an uncertain action blindly.

Example structure (replace the selector with one confirmed from the live UI):

```json
[
  {"type": "click", "selector": "CSS_SELECTOR"},
  {"type": "screenshot", "name": "after-action.png"}
]
```

Clicks and other changing actions require explicit `--apply`. `count`, `wait`, and screenshots do not. The full legacy `validate-ui-real.py --ha-ingress` suite is for isolated local tests; its old `/api/hassio` bootstrap is blocked on this HA instance. Use the live controller for ad hoc interaction, and the MCP screenshot tool for visual verification.
