"""Exercise the shipped JavaScript with the existing Node DOM/bridge harness."""

import re
from pathlib import Path

import pytest

from test_panel_pending_polling_reliability import _run_harness


@pytest.fixture(scope="module")
def script():
    html = (Path(__file__).resolve().parents[1] / "quickres/webview/panel.html").read_text(encoding="utf-8")
    return re.search(r"<script>(.*?)</script>", html, re.S).group(1)


SETUP = r"""
const base = {width:1920,height:1080,aspect_ratio:'16:9',kind:'native',custom:false};
const custom = {width:1728,height:1080,aspect_ratio:'8:5',kind:'stretched',custom:true};
const snapshot = presets => ({current_resolution:{width:1920,height:1080},presets});
S.presets = [base];
S.hotkey = {key:'F6',native_res:'1920x1080',stretched_res:'1920x1080',running:false};
renderResSection(S); renderHotkeySection(S);
const click = el('qr-panel').__handlers.click[0];
const options = id => el(id).children.map(o => o.value);
"""


def test_save_exposes_both_dropdowns_and_custom_hotkey_arguments(script):
    result = _run_harness(script, SETUP + r"""
(async () => {
  const calls = [];
  window.pywebview = {api:{
    add_resolution_preset:async value => {calls.push(['save',value]); return {ok:true,data:snapshot([base,custom])};},
    start_hotkey:async (...args) => {calls.push(['start',...args]); return {ok:true,data:{running:true}};}
  }};
  el('qr-custom-input').value = '1728 x 1080';
  await click({target:el('qr-custom-save')});
  const selects = [options('qr-hotkey-native'),options('qr-hotkey-stretched')];
  const cards = el('qr-res-presets').children.map(item => item.children[0].dataset.resolution);
  const controls = el('qr-res-presets').children.map(item => item.children.length);
  el('qr-hotkey-key').value = 'F8';
  el('qr-hotkey-stretched').value = '1728x1080';
  await click({target:el('qr-hotkey-start')});
  console.log(JSON.stringify({calls,selects,cards,controls,profile:S.hotkey}));
  process.exit(0);
})().catch(e=>fail(e.stack));
""")
    assert result["calls"] == [["save", [1728, 1080]], ["start", "F8", "1920x1080", "1728x1080"]]
    assert result["selects"] == [["1920x1080", "1728x1080"]] * 2
    assert result["cards"] == ["1920x1080", "1728x1080"]
    assert result["controls"] == [2, 3]  # Only saved custom cards get a Remove button.
    assert result["profile"]["running"] is True


def test_remove_preserves_saved_profile_and_unsaved_draft(script):
    result = _run_harness(script, SETUP + r"""
(async () => {
  S.presets = [base,custom];
  S.hotkey.running = false;
  S.hotkey.stretched_res = '1728x1080';
  S.hotkeyDraft = {key:'F9',native_res:'1920x1080',stretched_res:'1700x1080'};
  renderResSection(S); renderHotkeySection(S);
  window.pywebview = {api:{remove_resolution_preset:async value => ({ok:true,data:snapshot([base])})}};
  await click({target:{closest:selector => selector === '[data-remove-preset]' ? {dataset:{removePreset:'1728x1080'}} : null}});
  renderAll(S); // Language/theme changes must also retain the draft-only mode.
  console.log(JSON.stringify({presets:S.presets.length,profile:S.hotkey,draft:S.hotkeyDraft,
    shown:el('qr-hotkey-stretched').value,options:options('qr-hotkey-stretched')}));
  process.exit(0);
})().catch(e=>fail(e.stack));
""")
    assert result["presets"] == 1
    assert result["profile"]["running"] is False
    assert result["profile"]["stretched_res"] == "1728x1080"
    assert result["draft"]["key"] == "F9"
    assert result["shown"] == "1700x1080"
    assert result["options"] == ["1920x1080", "1728x1080", "1700x1080"]


@pytest.mark.parametrize("rejection", ["return {ok:false,kind:'error',message:'disk full'}", "throw new Error('disconnected')"])
def test_save_failure_keeps_ui_unchanged_and_can_retry(script, rejection):
    result = _run_harness(script, SETUP + r"""
(async () => {
  let attempts = 0;
  window.pywebview = {api:{add_resolution_preset:async () => {
    if (++attempts === 1) { REJECT; }
    return {ok:true,data:snapshot([base,custom])};
  }}};
  el('qr-custom-input').value = '1728x1080';
  await click({target:el('qr-custom-save')});
  const first = {count:S.presets.length,busy:presetMutationInFlight,disabled:el('qr-custom-save').disabled};
  await click({target:el('qr-custom-save')});
  console.log(JSON.stringify({first,attempts,count:S.presets.length}));
  process.exit(0);
})().catch(e=>fail(e.stack));
""".replace("REJECT", rejection))
    assert result == {"first": {"count": 1, "busy": False, "disabled": False}, "attempts": 2, "count": 2}


def test_old_poll_response_cannot_erase_newly_saved_preset(script):
    result = _run_harness(script, SETUP + r"""
(async () => {
  let release;
  window.pywebview = {api:{
    get_resolution_state:() => new Promise(resolve => release = resolve),
    add_resolution_preset:async () => ({ok:true,data:snapshot([base,custom])})
  }};
  const poll = refreshResolutionState();
  el('qr-custom-input').value = '1728x1080';
  await click({target:el('qr-custom-save')});
  release({ok:true,data:snapshot([base])}); await poll;
  console.log(JSON.stringify({count:S.presets.length,options:options('qr-hotkey-stretched')}));
  process.exit(0);
})().catch(e=>fail(e.stack));
""")
    assert result == {"count": 2, "options": ["1920x1080", "1728x1080"]}


def test_rapid_save_is_not_duplicated_and_keeps_intervening_user_edits(script):
    result = _run_harness(script, SETUP + r"""
(async () => {
  let release, calls = 0;
  window.pywebview = {api:{add_resolution_preset:() => {
    calls++; return new Promise(resolve => release = resolve);
  }}};
  el('qr-custom-input').value = '1728x1080';
  const saving = click({target:el('qr-custom-save')});
  await click({target:el('qr-custom-save')});
  const busy = el('qr-custom-save').disabled;
  el('qr-custom-input').value = '1600x1200';
  el('qr-hotkey-key').value = 'F9';
  el('qr-panel').__handlers.change[0]({target:el('qr-hotkey-key')});
  release({ok:true,data:snapshot([base,custom])}); await saving;
  console.log(JSON.stringify({calls,busy,input:el('qr-custom-input').value,key:el('qr-hotkey-key').value}));
  process.exit(0);
})().catch(e=>fail(e.stack));
""")
    assert result == {"calls": 1, "busy": True, "input": "1600x1200", "key": "F9"}


def test_invalid_text_does_not_call_save_or_apply_and_apply_still_works(script):
    result = _run_harness(script, SETUP + r"""
(async () => {
  const calls = [];
  window.pywebview = {api:{pick_resolution:async (...args) => {
    calls.push(args); return {ok:true,data:{ok:false,reason:null,message:'driver refused'}};
  }}};
  el('qr-custom-input').value = 'invalid';
  await click({target:el('qr-custom-save')});
  await click({target:el('qr-custom-apply')});
  el('qr-custom-input').value = '1728x1080';
  await click({target:el('qr-custom-apply')});
  console.log(JSON.stringify({calls,count:S.presets.length}));
  process.exit(0);
})().catch(e=>fail(e.stack));
""")
    assert result == {"calls": [[1728, 1080]], "count": 1}


def test_unsupported_save_opens_driver_guidance_without_adding_then_can_retry(script):
    result = _run_harness(script, SETUP + r"""
(async () => {
  const dialogs = [];
  openVendorDialog = (...args) => dialogs.push(args);
  let attempts = 0;
  window.pywebview = {api:{add_resolution_preset:async () => ({ok:true,data:++attempts === 1
    ? {ok:false,reason:'unsupported',width:1728,height:1080,vendors:['nvidia']}
    : {ok:true,added:true,...snapshot([base,custom])}})}};
  el('qr-custom-input').value = '1728x1080';
  await click({target:el('qr-custom-save')});
  const before = {count:S.presets.length,options:options('qr-hotkey-stretched'),disabled:el('qr-custom-save').disabled};
  await click({target:el('qr-custom-save')});
  console.log(JSON.stringify({dialogs,before,count:S.presets.length,input:el('qr-custom-input').value}));
  process.exit(0);
})().catch(e=>fail(e.stack));
""")
    assert result == {"dialogs": [[["nvidia"], 1728, 1080]],
                      "before": {"count": 1, "options": ["1920x1080"], "disabled": False},
                      "count": 2, "input": "1728x1080"}


def test_unsupported_hotkey_does_not_report_listening_or_discard_draft(script):
    result = _run_harness(script, SETUP + r"""
(async () => {
  let dialog;
  openVendorDialog = (...args) => dialog = args;
  S.hotkeyDraft = {key:'F8',native_res:'1920x1080',stretched_res:'1728x1080'};
  renderResSection(S); renderHotkeySection(S);
  window.pywebview = {api:{start_hotkey:async () => ({ok:true,data:{
    ok:false,reason:'unsupported',width:1728,height:1080,vendors:['amd']
  }})}};
  await click({target:el('qr-hotkey-start')});
  console.log(JSON.stringify({dialog,running:S.hotkey.running,polling:hotkeyPollTimer !== null,
    draft:S.hotkeyDraft,shown:el('qr-hotkey-stretched').value,startHidden:el('qr-hotkey-start').hidden}));
  process.exit(0);
})().catch(e=>fail(e.stack));
""")
    assert result == {"dialog": [["amd"], 1728, 1080], "running": False, "polling": False,
                      "draft": {"key": "F8", "native_res": "1920x1080", "stretched_res": "1728x1080"},
                      "shown": "1728x1080", "startHidden": False}


def test_blocked_remove_leaves_the_active_profile_and_card_untouched(script):
    result = _run_harness(script, SETUP + r"""
(async () => {
  S.presets = [base,custom];
  S.hotkey.running = true; S.hotkey.stretched_res = '1728x1080';
  renderResSection(S); renderHotkeySection(S);
  window.pywebview = {api:{remove_resolution_preset:async () => ({ok:false,kind:'error',message:'Stop the hotkey first'})}};
  await click({target:{closest:selector => selector === '[data-remove-preset]' ? {dataset:{removePreset:'1728x1080'}} : null}});
  console.log(JSON.stringify({count:S.presets.length,running:S.hotkey.running,shown:el('qr-hotkey-stretched').value,busy:presetMutationInFlight}));
  process.exit(0);
})().catch(e=>fail(e.stack));
""")
    assert result == {"count": 2, "running": True, "shown": "1728x1080", "busy": False}


def test_new_controls_and_duplicate_feedback_use_translated_strings(script):
    result = _run_harness(script, SETUP + r"""
(async () => {
  S.strings = {btn_save_preset:'Сохранить',preset_save_note:'Поддерживается драйвером',
    preset_remove_label:'Удалить',preset_already_exists:'Уже есть'};
  S.presets = [base,custom];
  renderAll(S);
  const removeTitle = el('qr-res-presets').children[1].children[2].title;
  const statuses = []; pushStatus = (...args) => statuses.push(args);
  window.pywebview = {api:{add_resolution_preset:async () => ({ok:true,data:{ok:true,added:false,...snapshot([base,custom])}})}};
  el('qr-custom-input').value = '1728x1080';
  await click({target:el('qr-custom-save')});
  console.log(JSON.stringify({button:el('qr-custom-save').textContent,note:el('qr-custom-note').textContent,removeTitle,statuses,count:S.presets.length}));
  process.exit(0);
})().catch(e=>fail(e.stack));
""")
    assert result == {"button": "Сохранить", "note": "Поддерживается драйвером", "removeTitle": "Удалить 1728x1080",
                      "statuses": [["ok", "Уже есть"]], "count": 2}
