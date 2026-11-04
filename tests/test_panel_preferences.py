import re
from pathlib import Path

import pytest

from test_panel_pending_polling_reliability import _run_harness


@pytest.fixture(scope="module")
def script():
    html = (Path(__file__).resolve().parents[1] / "quickres/webview/panel.html").read_text(encoding="utf-8")
    return re.search(r"<script>(.*?)</script>", html, re.S).group(1)


def test_search_dimensions_aspect_and_favorite_order(script):
    result = _run_harness(script, r"""
S.presets = [
  {width:1920,height:1080,aspect_ratio:'16:9'},
  {width:1440,height:1080,aspect_ratio:'4:3'},
  {width:2560,height:1440,aspect_ratio:'16:9'}
];
S.uiPreferences.favorite_resolutions = ['1440x1080'];
const outputs = {};
for (const query of ['', '1920', '1080', '1920 X 1080', '2560×1440', '4:3', '16:9', '9999']) {
  el('qr-res-search').value = query;
  el('qr-res-search').__handlers.input[0]({target:el('qr-res-search')});
  outputs[query] = el('qr-res-presets').children.map(item => item.children[0].dataset.resolution);
}
console.log(JSON.stringify({outputs, empty:!el('qr-search-empty').hidden, source:S.presets.map(p=>p.width)}));
""")
    assert result["outputs"] == {
        "": ["1440x1080", "1920x1080", "2560x1440"],
        "1920": ["1920x1080"],
        "1080": ["1440x1080", "1920x1080"],
        "1920 X 1080": ["1920x1080"],
        "2560×1440": ["2560x1440"],
        "4:3": ["1440x1080"],
        "16:9": ["1920x1080", "2560x1440"],
        "9999": [],
    }
    assert result["empty"] is True
    assert result["source"] == [1920, 1440, 2560]


def test_preferences_serialize_and_survive_translation_and_poll(script):
    result = _run_harness(script, r"""
(async () => {
  const saves = [];
  let release;
  window.pywebview = {api:{
    set_ui_preferences: async prefs => {
      saves.push(prefs);
      if (saves.length === 1) await new Promise(resolve => release = resolve);
      return {ok:true,data:prefs};
    },
    get_resolution_state: async () => ({ok:true,data:{current_resolution:{width:1920,height:1080},presets:[{width:1920,height:1080,aspect_ratio:'16:9'}]}}),
  }};
  const click = el('qr-panel').__handlers.click[0];
  const target = (selector, data) => ({closest:s => s === selector ? {dataset:data} : null});
  const first = click({target:target('[data-section]', {section:'hotkey'})});
  await Promise.resolve(); await Promise.resolve();
  const second = click({target:target('[data-favorite]', {favorite:'1920x1080'})});
  await Promise.resolve();
  const inFlight = saves.length;
  release(); await first; await second;
  S.language.resolved = 'ru';
  el('qr-custom-input').value = '1600x1200';
  el('qr-res-search').value = '1920'; S.resolutionQuery = '1920';
  el('qr-hotkey-key').value = 'F9';
  el('qr-panel').__handlers.change[0]({target:el('qr-hotkey-key')});
  renderAll(S);
  await refreshResolutionState();
  console.log(JSON.stringify({inFlight,saves,prefs:S.uiPreferences,hidden:el('qr-body-hotkey').hidden,custom:el('qr-custom-input').value,search:el('qr-res-search').value,key:el('qr-hotkey-summary').textContent,label:el('qr-search-label').textContent}));
})().catch(e=>fail(e.stack));
""")
    assert result["inFlight"] == 1
    assert result["saves"] == [
        {"collapsed_sections": ["hotkey"], "favorite_resolutions": []},
        {"collapsed_sections": ["hotkey"], "favorite_resolutions": ["1920x1080"]},
    ]
    assert result["prefs"] == result["saves"][-1]
    assert result["hidden"] is True
    assert result["custom"] == "1600x1200"
    assert result["search"] == "1920"
    assert result["key"] == "F9"
    assert result["label"] == "Поиск разрешений"


def test_card_focus_and_scroll_survive_background_redraw(script):
    result = _run_harness(script, r"""
S.presets = [{width:1920,height:1080,aspect_ratio:'16:9'}];
renderPresetCards(S);
const old = el('qr-res-presets').children[0].children[1];
document.activeElement = old;
el('qr-res-presets').scrollTop = 70;
const create = document.createElement;
let focused;
document.createElement = () => {
  const element = create();
  element.focus = () => { focused = element.dataset.favorite; };
  return element;
};
renderPresetCards(S);
console.log(JSON.stringify({focused,scroll:el('qr-res-presets').scrollTop}));
""")
    assert result == {"focused": "1920x1080", "scroll": 70}


def test_migration_opens_update_dialog_and_download_action(script):
    result = _run_harness(script, r"""
(async () => {
  let opened;
  openModal = kind => opened = kind;
  window.pywebview = {api:{check_updates:async()=>({ok:true,data:{migration_required:true,installer_url:'https://github.com/lxzydev/QuickRes/releases/latest'}})}};
  await el('qr-actionbar').__handlers.click[0]({target:el('qr-check-updates')});
  const modal = updateModalMarkup(S.updateInfo);
  const actions = modal.children[1].children[3];
  const action = actions.children[3];
  console.log(JSON.stringify({opened,url:action.dataset.external,label:action.textContent,message:updateStatusText({}),percent:updateProgressPercent({percentage:48,total_bytes:null})}));
})().catch(e=>fail(e.stack));
""")
    assert result["opened"] == "update"
    assert result["url"] == "https://github.com/lxzydev/QuickRes/releases/latest"
    assert result["label"] == "Open downloads"
    assert "new installer" in result["message"]
    assert result["percent"] == 48


def test_boot_restores_preferences_and_running_summary_keeps_actual_key(script):
    result = _run_harness(script, r"""
(async () => {
  window.pywebview = {api:{get_initial_state:async()=>({ok:true,data:{
    theme:'dark',version:'3',language:{setting:'en',resolved:'en',options:{en:'English'}},strings:{},
    current_resolution:{width:1920,height:1080},presets:[{width:1920,height:1080,aspect_ratio:'16:9'}],
    hotkey:{key:'F6',native_res:'1920x1080',stretched_res:'1920x1080',running:true},
    ui_preferences:{collapsed_sections:['custom','hotkey'],favorite_resolutions:['1920x1080']},
    monitors:[],pending:{outcomes:[],force_unlockable:false},faq:[]
  }})}};
  await boot();
  el('qr-hotkey-key').value = 'F9';
  el('qr-panel').__handlers.change[0]({target:el('qr-hotkey-key')});
  renderHotkeySection(S);
  console.log(JSON.stringify({customHidden:el('qr-body-custom').hidden,hotkeyHidden:el('qr-body-hotkey').hidden,
    favorites:S.uiPreferences.favorite_resolutions,actual:S.hotkey.key,draft:el('qr-hotkey-key').value,
    summary:el('qr-hotkey-summary').textContent,status:el('qr-hotkey-state').textContent}));
  process.exit(0);
})().catch(e=>fail(e.stack));
""")
    assert result == {
        "customHidden": True,
        "hotkeyHidden": True,
        "favorites": ["1920x1080"],
        "actual": "F6",
        "draft": "F9",
        "summary": "F6",
        "status": "Listening",
    }
