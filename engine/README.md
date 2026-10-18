# Patched engine

Computer Use Plus runs on [Open Computer Use](https://github.com/iFurySt/open-computer-use) (MIT). The stock
releases fail silently in ways that cost real work: an Outlook draft saved with an empty subject although every
call reported success. This folder pins an upstream commit (`UPSTREAM.json`) and carries our changes as plain
patches (`patches/`), so exactly what differs from upstream is reviewable. `scripts/build-engine.py` builds it.

## What the patches change

| Problem in stock engine | Upstream | Patch behaviour |
|---|---|---|
| `set_value` reports success while the control ignored the write | [#83](https://github.com/iFurySt/open-computer-use/issues/83) finding 2, open, no PR | Value is read back; the call fails when nothing changed |
| AX writes into web fields (React and friends) show up but are never registered by the app | not filed | Web text fields are filled with real keystrokes; a native field that ignores the AX write is retried with keystrokes |
| `type_text` refuses a focused field in a background app ("requires a focused editable text element") | #83 finding 5 | Focus read live, falls back to the field last clicked; field read back, WARNING when unchanged, "unverified" when no field is known |
| Snapshots and action read-backs follow whichever window has focus, so indexes drift between windows | #83 finding 1 (High), open | `get_app_state window_id` / `window_title` pin a window; header lists windows; window changes are announced |
| Every action returns the whole tree plus a screenshot | [#16](https://github.com/iFurySt/open-computer-use/issues/16), #83 finding 6a | `OPEN_COMPUTER_USE_ACTION_RESULT=diff` returns changed lines only; `OPEN_COMPUTER_USE_ACTION_SCREENSHOT=0` skips capture |
| No clipboard entry for long text | #83 finding 6d | `type_text method=paste` (clipboard restored; falls back to typing when the app ignores Cmd+V) |
| Nearby hit-testing converts window points as screenshot pixels (wrong spot when scaled, error without a screenshot) | not filed | Window points mapped directly |
| A click on an element behind a sheet presses the sheet's button | not filed | Refused while a sheet covers the window |

Already upstream and included by the pin: `query` (#73), `select_text` (#68), selective AX/screenshot collection
(#101), background keyboard `key_method=sky_key` (#65).

## Identity

The build uses upstream's Dev bundle identity (`com.ifuryst.opencomputeruse.dev`, "Open Computer Use (Dev).app")
and its own agent socket namespace (`hermes`), so it sits next to a stock install without sharing permissions or
agents. It is signed with a self-signed identity created on first build in a dedicated keychain under the Hermes
root (`tools/ocu-signing`). That signature is stable across rebuilds, so macOS keeps the Accessibility and Screen
Recording grants; grant them once to **Open Computer Use (Dev)**.
