"""Round 27 R3 Reliability fixes for `quickres/webview/panel.html`.

These tests exercise the panel's actual authored JavaScript (not a
reimplementation of it) by extracting the real `<script>` block out of the
static HTML file -- the same extraction pattern `tests/test_panel_tokens.py`
already uses for the `<style>`/`<script>` blocks -- and running it under a
real Node.js process with a small hand-rolled DOM/bridge stub. This lets the
tests assert on observable behavior (the resulting `S.pending` state, and
whether the background guard-poll timer keeps running) produced by the exact
click handlers and modal-close logic shipped in the file, rather than on the
file's source text.

Covers two scenarios:

1. Clicking "Keep disabled" / "Revert now" on a notice card that lists more
   than one pending target (a merged crash-recovery record) must not wipe
   out a sibling target that is still genuinely pending -- the handler has
   to re-query the server's current pending state instead of assuming the
   whole record is settled.
2. Closing an unrelated modal (e.g. FAQ) must not stop an active
   guard-poll/countdown that a still-unresolved pending state needs -- only
   closing the Monitors modal itself (the modal that actually owns that
   polling) may stop it.

Run scoped: `python -m pytest tests/test_panel_pending_polling_reliability.py -q`
"""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

PANEL_PATH = Path(__file__).resolve().parent.parent / "quickres" / "webview" / "panel.html"

NODE = shutil.which("node")


def _script_block(html: str) -> str:
    matches = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
    assert len(matches) == 1, f"expected exactly one <script> block, found {len(matches)}"
    return matches[0]


@pytest.fixture(scope="module")
def panel_script():
    html = PANEL_PATH.read_text(encoding="utf-8")
    return _script_block(html)


# A minimal, generic DOM/window/bridge stub. `document.getElementById`
# returns the SAME fake element object on every call for a given id (so an
# `addEventListener` registration made while the real script runs top-to-
# bottom can be retrieved later by the test), and `document.createElement`
# hands out fresh ones. Every fake element accepts arbitrary property
# get/set and no-ops the DOM methods the panel's render functions call
# (appendChild, closest, classList, ...) so the full script can execute
# without needing a real browser DOM.
_HARNESS_PRELUDE = r"""
'use strict';

const elementRegistry = new Map();

function makeFakeElement(id) {
  const store = { id: id || '', className: '', textContent: '', innerHTML: '', hidden: false, value: '' };
  const handlers = {};
  const children = [];
  return {
    get id() { return store.id; },
    set id(v) { store.id = v; },
    get className() { return store.className; },
    set className(v) { store.className = v; },
    get textContent() { return store.textContent; },
    set textContent(v) { store.textContent = v; },
    get innerHTML() { return store.innerHTML; },
    set innerHTML(v) { store.innerHTML = v; children.length = 0; },
    get hidden() { return store.hidden; },
    set hidden(v) { store.hidden = v; },
    get value() { return store.value; },
    set value(v) { store.value = v; },
    dataset: {},
    children: children,
    get firstElementChild() { return children[0] || null; },
    addEventListener(type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
    __handlers: handlers,
    appendChild(child) { children.push(child); return child; },
    cloneNode() {
      const copy = makeFakeElement();
      copy.value = store.value;
      copy.textContent = store.textContent;
      return copy;
    },
    removeChild(child) { const i = children.indexOf(child); if (i >= 0) children.splice(i, 1); return child; },
    setAttribute() {},
    closest() { return null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    style: {},
  };
}

const document = {
  getElementById(id) {
    if (!elementRegistry.has(id)) elementRegistry.set(id, makeFakeElement(id));
    return elementRegistry.get(id);
  },
  createElement() { return makeFakeElement(); },
  documentElement: makeFakeElement('documentElement'),
};

const windowHandlers = {};
const window = {
  pywebview: undefined,
  addEventListener(type, fn) { (windowHandlers[type] = windowHandlers[type] || []).push(fn); },
  onerror: null,
  onunhandledrejection: null,
};

function fail(message) {
  console.error('HARNESS_ERROR: ' + message);
  process.exit(1);
}
"""


def _run_harness(script: str, scenario: str) -> dict:
    """Concatenates the DOM stub prelude, the real panel.html <script>
    contents, and a scenario epilogue into one file, runs it with a real
    Node.js process, and returns the JSON object the scenario printed.

    A non-zero exit (a thrown error, or the harness's own `fail()`) surfaces
    the full stdout/stderr in the assertion message rather than swallowing
    it, since the whole point of this suite is to catch real behavioral
    regressions, not to pass silently on a broken harness.
    """
    assert NODE, "node executable not found on PATH -- required to run this test"
    full_source = _HARNESS_PRELUDE + "\n" + script + "\n" + scenario
    # Written to a real UTF-8 file rather than piped via stdin: panel.html's
    # script contains non-ASCII glyphs (toast marks etc.), and stdin pipes
    # default to the console's active codepage (cp1252 on this Windows
    # host), which cannot encode them.
    with tempfile.TemporaryDirectory() as tmp_dir:
        harness_path = Path(tmp_dir) / "panel_harness.js"
        harness_path.write_text(full_source, encoding="utf-8")
        proc = subprocess.run(
            [NODE, str(harness_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
    assert proc.returncode == 0, (
        f"node harness exited {proc.returncode}\n--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}"
    )
    lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
    assert lines, f"node harness produced no output\n--- stderr ---\n{proc.stderr}"
    return json.loads(lines[-1])


def test_hotkey_profile_survives_start_stop_start(panel_script):
    result = _run_harness(panel_script, r"""
(async () => {
  const calls = [];
  window.pywebview = { api: {
    start_hotkey: async (...args) => { calls.push(args); return {ok:true, data:{running:true}}; },
    stop_hotkey: async () => ({ok:true, data:{running:false}}),
  }};
  S.hotkey = {key:'F6', native_res:'1920x1080', stretched_res:'1440x1080', running:false};
  el('qr-hotkey-key').value = 'F7';
  el('qr-hotkey-native').value = '2560x1440';
  el('qr-hotkey-stretched').value = '1920x1440';
  const click = el('qr-panel').__handlers.click[0];
  await click({target:el('qr-hotkey-start')});
  const shown = [el('qr-hotkey-key').value, el('qr-hotkey-native').value, el('qr-hotkey-stretched').value];
  await click({target:el('qr-hotkey-stop')});
  await click({target:el('qr-hotkey-start')});
  console.log(JSON.stringify({calls, shown}));
  process.exit(0);
})().catch(err => fail(err.stack));
""")
    profile = ["F7", "2560x1440", "1920x1440"]
    assert result == {"calls": [profile, profile], "shown": profile}


def test_failed_stop_keeps_retry_button_after_status_poll(panel_script):
    result = _run_harness(panel_script, r"""
(async () => {
  window.pywebview = { api: {
    stop_hotkey: async () => ({ok:false, kind:'error', message:'driver refused'}),
    get_hotkey_status: async () => ({ok:true, data:{running:false, restore_pending:true}}),
  }};
  S.hotkey.running = true;
  const click = el('qr-panel').__handlers.click[0];
  await click({target:el('qr-hotkey-stop')});
  await pollHotkeyOnce();
  console.log(JSON.stringify({stopHidden:el('qr-hotkey-stop').hidden, startHidden:el('qr-hotkey-start').hidden}));
  process.exit(0);
})().catch(err => fail(err.stack));
""")
    assert result == {"stopHidden": False, "startHidden": True}


def test_boot_resumes_polling_and_retry_stop_for_pending_restore(panel_script):
    result = _run_harness(panel_script, r"""
(async () => {
  const initial = Object.assign({}, S, {
    current_resolution:null,
    hotkey:{key:'F6', native_res:'1920x1080', stretched_res:'1440x1080', running:false, restore_pending:true},
  });
  window.pywebview = {api:{get_initial_state:async () => ({ok:true, data:initial})}};
  await boot();
  console.log(JSON.stringify({polling:hotkeyPollTimer !== null, stopHidden:el('qr-hotkey-stop').hidden}));
  process.exit(0);
})().catch(err => fail(err.stack));
""")
    assert result == {"polling": True, "stopHidden": False}


@pytest.mark.parametrize("late_response", [False, True])
def test_hotkey_poll_preserves_edits_and_ignores_pre_stop_response(panel_script, late_response):
    scenario = r"""
(async () => {
  let reply;
  window.pywebview = { api: {
    get_hotkey_status: () => new Promise(resolve => { reply = resolve; }),
    stop_hotkey: async () => ({ok:true, data:{running:false}}),
  }};
  S.hotkey.running = true;
  el('qr-hotkey-key').value = 'F8';
  const poll = pollHotkeyOnce();
  if (LATE) await el('qr-panel').__handlers.click[0]({target:el('qr-hotkey-stop')});
  reply({ok:true, data:{running:true}});
  await poll;
  console.log(JSON.stringify({key:el('qr-hotkey-key').value, running:S.hotkey.running}));
  process.exit(0);
})().catch(err => fail(err.stack));
""".replace("LATE", str(late_response).lower())
    result = _run_harness(panel_script, scenario)
    if late_response:
        assert result["running"] is False
    else:
        assert result["key"] == "F8"


# -- Finding 1: keep_disabled()/revert_now() must not wipe a still-pending sibling --

_SIBLING_SCENARIO = """
(async () => {
  // Precondition: the notice card lists TWO merged targets -- A (still
  // genuinely pending, no live guard, e.g. a crash-recovered UNCONFIRMABLE
  // entry) and B (the target this click is actually about to resolve).
  S.pending = {
    outcomes: [
      { resolution: 'unconfirmable', instance_id: 'A', friendly_name: 'Monitor A', message: 'A pending', elapsed_s: 30, can_force_unlock: false },
      { resolution: 'confirmed', instance_id: 'B', friendly_name: 'Monitor B', message: 'B ok', elapsed_s: 2, can_force_unlock: false },
    ],
    force_unlockable: false,
  };
  S.guardRemainingS = null;
  S.modal = 'monitors';
  startGuardPolling();
  const pollingBefore = guardPollTimer !== null;

  window.pywebview = {
    api: {
      __API_METHOD__: async () => (__API_RESPONSE__),
      recheck_pending: async () => ({
        ok: true,
        data: {
          outcomes: [
            { resolution: 'unconfirmable', instance_id: 'A', friendly_name: 'Monitor A', message: 'A pending', elapsed_s: 34, can_force_unlock: false },
          ],
          force_unlockable: false,
          guard_remaining_s: null,
        },
      }),
      list_monitors: async () => ({ ok: true, data: [] }),
    },
  };

  const overlaysEl = document.getElementById('qr-overlays');
  const clickHandlers = overlaysEl.__handlers['click'];
  if (!clickHandlers || clickHandlers.length !== 1) {
    fail('expected exactly one click handler on qr-overlays, found ' + (clickHandlers ? clickHandlers.length : 0));
    return;
  }

  await clickHandlers[0]({ target: { id: '__BUTTON_ID__', closest: () => null } });

  const outcomesAfter = (S.pending && S.pending.outcomes) || [];
  const sibling = outcomesAfter.find(function (o) { return o.instance_id === 'A'; });

  console.log(JSON.stringify({
    pollingBefore: pollingBefore,
    outcomeCountAfter: outcomesAfter.length,
    siblingSurvived: !!sibling,
    pollingAfter: guardPollTimer !== null,
  }));
  process.exit(0);
})().catch(function (err) {
  fail((err && err.stack) || String(err));
});
"""


@pytest.mark.parametrize(
    "button_id,api_method,api_response",
    [
        ("qr-notice-keep", "keep_disabled", {"ok": True, "data": {"kept": True}}),
        ("qr-notice-revert", "revert_now", {"ok": True, "data": {"results": []}}),
    ],
)
def test_notice_resolution_preserves_still_pending_sibling(panel_script, button_id, api_method, api_response):
    scenario = (
        _SIBLING_SCENARIO
        .replace("__BUTTON_ID__", button_id)
        .replace("__API_METHOD__", api_method)
        .replace("__API_RESPONSE__", json.dumps(api_response))
    )
    result = _run_harness(panel_script, scenario)

    assert result["pollingBefore"] is True
    # The sibling target (A) was never touched by this click -- it must
    # still be present in S.pending.outcomes afterwards, and the guard poll
    # that would surface its eventual resolution must still be running.
    assert result["outcomeCountAfter"] == 1, (
        f"expected the still-pending sibling to survive, got {result['outcomeCountAfter']} outcomes"
    )
    assert result["siblingSurvived"] is True, "sibling target A was wiped from S.pending.outcomes"
    assert result["pollingAfter"] is True, "guard polling was stopped even though target A is still pending"


# -- Finding 2: closeOverlay() must only stop polling for the Monitors modal --

_MODAL_CLOSE_SCENARIO = """
(async () => {
  // Case A: an active guard poll while a NON-Monitors modal (FAQ) is open --
  // closing that modal must not stop it.
  S.pending = {
    outcomes: [
      { resolution: 'unconfirmable', instance_id: 'A', friendly_name: 'Monitor A', message: 'pending', elapsed_s: 5, can_force_unlock: false },
    ],
    force_unlockable: false,
  };
  S.guardRemainingS = null;
  S.modal = 'faq';
  startGuardPolling();
  const pollingActiveBeforeFaqClose = guardPollTimer !== null;
  closeOverlay();
  const pollingActiveAfterFaqClose = guardPollTimer !== null;

  // Case B: the same active poll, but this time it's the Monitors modal
  // itself being closed -- this must still stop it (regression coverage).
  S.modal = 'monitors';
  startGuardPolling();
  const pollingActiveBeforeMonitorsClose = guardPollTimer !== null;
  closeOverlay();
  const pollingActiveAfterMonitorsClose = guardPollTimer !== null;

  console.log(JSON.stringify({
    pollingActiveBeforeFaqClose: pollingActiveBeforeFaqClose,
    pollingActiveAfterFaqClose: pollingActiveAfterFaqClose,
    pollingActiveBeforeMonitorsClose: pollingActiveBeforeMonitorsClose,
    pollingActiveAfterMonitorsClose: pollingActiveAfterMonitorsClose,
  }));
  process.exit(0);
})().catch(function (err) {
  fail((err && err.stack) || String(err));
});
"""


def test_close_overlay_only_stops_polling_for_monitors_modal(panel_script):
    result = _run_harness(panel_script, _MODAL_CLOSE_SCENARIO)

    assert result["pollingActiveBeforeFaqClose"] is True
    assert result["pollingActiveAfterFaqClose"] is True, (
        "closing an unrelated (FAQ) modal stopped an active guard poll a still-pending target needs"
    )
    assert result["pollingActiveBeforeMonitorsClose"] is True
    assert result["pollingActiveAfterMonitorsClose"] is False, (
        "closing the Monitors modal itself no longer stops its own modal-scoped guard polling"
    )


# -- Finding 3: auto-revert must refresh monitor enabled-state in the UI --

_AUTO_REVERT_REFRESH_SCENARIO = """
(async () => {
  S.pending = {
    outcomes: [
      { resolution: 'in_flight', instance_id: 'A', friendly_name: 'Monitor A', message: 'pending', elapsed_s: 5, can_force_unlock: false },
    ],
    force_unlockable: false,
  };
  S.monitors = [{ instance_id: 'A', friendly_name: 'Monitor A', enabled: false }];
  S.guardRemainingS = 0;

  window.pywebview = {
    api: {
      recheck_pending: async () => ({
        ok: true,
        data: { outcomes: [], force_unlockable: false, guard_remaining_s: null },
      }),
      list_monitors: async () => ({
        ok: true,
        data: [{ instance_id: 'A', friendly_name: 'Monitor A', enabled: true }],
      }),
    },
  };

  await pollGuardOnce();
  console.log(JSON.stringify({
    monitorEnabled: S.monitors[0].enabled,
    pendingCount: S.pending.outcomes.length,
    pollingStopped: guardPollTimer === null,
  }));
  process.exit(0);
})().catch(function (err) {
  fail((err && err.stack) || String(err));
});
"""


def test_auto_revert_refreshes_monitor_enabled_state(panel_script):
    result = _run_harness(panel_script, _AUTO_REVERT_REFRESH_SCENARIO)

    assert result["monitorEnabled"] is True, (
        "the monitor was re-enabled by the auto-revert but the panel kept "
        "the stale disabled entry"
    )
    assert result["pendingCount"] == 0
    assert result["pollingStopped"] is True
