"""
HOW THIS WORKS:
- Every user-facing string lives in STRINGS["en"], keyed by a short name.

- STRINGS["ru"] holds the Russian translation for the same key.

- Strings with dynamic content use {placeholders}, e.g. "Reverting {name}...".
  NEVER remove or rename anything inside { } when translating, only translate
  the surrounding text.

- If a key is missing from a non-English language, it silently falls back to
  English, so a partial translation never breaks the app.

FOR TRANSLATORS:
Only edit the STRINGS["ru"] block below. It has the same keys, in the same
order, as STRINGS["en"] above it, just scroll up to see the English text
for whatever key you're on.

Leave STRINGS["en"] and everything else in
this file untouched.

Keep any {word} placeholders exactly as they appear
in the English version (e.g. {name}, {seconds}), just move them to
wherever fits in the Russian sentence.
"""

import ctypes
import threading

LANG_RUSSIAN = 0x19

STRINGS = {
    "en": {
        # --- Main window ---
        "app_title": "QuickRes",
        "custom_resolution_label": "Custom Resolution",
        "custom_res_placeholder": "e.g. 1440x1080",
        "btn_apply": "Apply",
        "btn_save_preset": "Save preset",
        "preset_save_note": "Save a driver-supported resolution to Quick resolutions and both hotkey lists without switching.",
        "preset_saved": "Preset saved. Select it in the hotkey lists.",
        "preset_already_exists": "This resolution is already in the preset list.",
        "preset_removed": "Preset removed. Existing hotkey settings are unchanged.",
        "preset_remove_label": "Remove saved preset",
        "preset_busy": "A preset or hotkey operation is in progress. Try again.",
        "preset_in_use": "Stop the hotkey before removing a preset it uses. Resolve any pending restoration first.",
        "preset_invalid_dimensions": "Resolution dimensions must be between 10 and 99999.",
        "preset_limit_reached": "At most {count} custom presets can be saved.",
        "resolution_query_failed": "Could not read any supported resolutions. Check the display connection and try again.",
        "hotkey_toggle_label": "Hotkey Toggle",
        "btn_start_hotkey": "Start Hotkey",
        "btn_stop_hotkey": "Stop Hotkey",
        "native_label": "Native:",
        "stretched_label": "Stretched:",
        "btn_faq": "FAQ",
        "btn_monitors": "Monitors",
        # --- Boot failure (persistent retry state, panel.html) ---
        "boot_error_title": "Startup failed",
        "boot_error_body": "Something went wrong while starting QuickRes. You can try again.",
        "btn_retry": "Retry",
        # --- Resolution-not-found dialog ---
        "dialog_res_not_found_title": "Resolution not found",
        "btn_nvidia_panel": "NVIDIA Control Panel",
        "btn_amd_software": "AMD Software",
        "btn_intel_graphics": "Intel Graphics Software",
        "btn_cancel": "Cancel",
        # --- FAQ window ---
        "faq_window_title": "FAQ",
        "faq_q1": 'Why does it say "Resolution not found"?',
        "faq_a1": (
            "QuickRes checks your GPU driver's list of registered resolutions "
            "before switching. If a resolution isn't on that list, Windows has "
            "no way to switch to it yet.\n\n"
            "When this happens, QuickRes shows a popup with a button for your "
            "graphics software (NVIDIA Control Panel, AMD Software, or Intel "
            "Graphics Software, based on what's actually detected in your PC). "
            "Click it, then add the resolution as a custom resolution there:\n\n"
            "NVIDIA: Display > Change Resolution > Customize >"
            "Create Custom Resolution\n\n"
            "AMD: Display > Custom Resolutions > Create New\n\n"
            "Intel: Display > General > Resolution > + (create custom profile)"
        ),
        "faq_q2": "Nothing happens when I click a button?",
        "faq_a2": "Try running QuickRes as Administrator.",
        "faq_q3": "Screen looks wrong after switching? (valorant)",
        "faq_a3": (
            "Make sure Valorant is on the fill/agent select screen before you "
            "switch to a stretched res. You have to do this every match, since "
            "loading into a new game resets your resolution and switching too "
            "early will give you black bars. Set a hotkey below so you can "
            "toggle between native and stretched with one keypress instead of "
            "clicking through the app."
        ),
        "faq_q4": "What does the hotkey do?",
        "faq_a4": (
            "Once you press Start Hotkey, the key you picked toggles your "
            "display between the Native and Stretched resolutions set above. "
            "Press it once on the fill/agent select screen to go stretched, "
            "press it again after the match to go back to native.\n\n"
        ),
        "theme_light": "Light",
        "theme_dark": "Dark",
        # --- Monitors window ---
        "monitors_window_title": "Monitors",
        "monitor_status_enabled": "Enabled",
        "monitor_status_disabled": "Disabled",
        "btn_disable": "Disable",
        "btn_enable": "Enable",
        # --- Revert / keep-disabled dialog ---
        "revert_dialog_title": "Keep this monitor disabled?",
        "btn_keep_disabled": "Keep disabled",
        "btn_revert_now": "Revert now",
        # --- Webview panel chrome ---
        "quick_resolutions_label": "Quick resolutions",
        "hotkey_state_stopped": "Stopped",
        "hotkey_state_running": "Listening",
        "notice_title": "A monitor may still be disabled",
        "btn_updates": "Updates",
        "monitors_detected_count": "{count} detected",
        "btn_force_unlock": "Force unlock",
        "revert_note": "If you do nothing, QuickRes re-enables it automatically.",
        "btn_disable_all": "Disable all",
        "preset_kind_native": "Native",
        "preset_kind_stretched": "Stretched",
        "preset_kind_low": "Low",
        # --- Update-available modal (wires the Updates button to
        # the existing download/verify/apply pipeline) ---
        "update_available_title": "Update available",
        "update_available_body": "QuickRes {version} is ready to install.",
        "btn_update_now": "Update Now",
        "btn_later": "Later",
        "btn_retry_download": "Retry download",
        "update_downloading": "Downloading update… {percent}%",
        "update_downloading_unknown": "Downloading update…",
        "update_verifying": "Verifying update…",
        "update_ready": "Update downloaded. Restarting to install…",
        "update_installing": "Restarting to install the update…",
        "update_failed": "Could not download the update. Check your connection and try again.",
    },
    "ru": {
        # --- Main window ---
        "app_title": "QuickRes",
        "custom_resolution_label": "Пользовательское разрешение",
        "custom_res_placeholder": "например, 1440x1080",
        "btn_apply": "Применить",
        "btn_save_preset": "Сохранить",
        "preset_save_note": "Сохранить поддерживаемое драйвером разрешение в быстрые разрешения и оба списка горячей клавиши без переключения.",
        "preset_saved": "Разрешение сохранено. Выберите его в списках горячей клавиши.",
        "preset_already_exists": "Это разрешение уже есть в списке.",
        "preset_removed": "Разрешение удалено. Настройки горячей клавиши не изменены.",
        "preset_remove_label": "Удалить сохранённое разрешение",
        "preset_busy": "Выполняется операция с разрешением или горячей клавишей. Повторите попытку.",
        "preset_in_use": "Остановите горячую клавишу перед удалением используемого разрешения. Сначала завершите ожидающее восстановление.",
        "preset_invalid_dimensions": "Ширина и высота должны быть от 10 до 99999.",
        "preset_limit_reached": "Можно сохранить не более {count} пользовательских разрешений.",
        "resolution_query_failed": "Не удалось получить поддерживаемые разрешения. Проверьте подключение дисплея и повторите попытку.",
        "hotkey_toggle_label": "Горячая клавиша",
        "btn_start_hotkey": "Включить",
        "btn_stop_hotkey": "Отключить",
        "native_label": "Нативное:",
        "stretched_label": "Растянутое:",
        "btn_faq": "FAQ",
        "btn_monitors": "Мониторы",
        # --- Boot failure (persistent retry state, panel.html) ---
        "boot_error_title": "Не удалось запустить приложение",
        "boot_error_body": "Что-то пошло не так при запуске QuickRes. Вы можете попробовать снова.",
        "btn_retry": "Повторить",
        # --- Resolution-not-found dialog ---
        "dialog_res_not_found_title": "Разрешение не найдено",
        "btn_nvidia_panel": "Панель управления NVIDIA",
        "btn_amd_software": "AMD Software",
        "btn_intel_graphics": "Intel Graphics Software",
        "btn_cancel": "Отмена",
        # --- FAQ window ---
        "faq_window_title": "FAQ",
        "faq_q1": "Почему появляется сообщение «Разрешение не найдено»?",
        "faq_a1": (
            "Перед переключением QuickRes проверяет список разрешений, зарегистрированных "
            "в драйвере видеокарты. Если нужного разрешения в списке нет, Windows пока "
            "не сможет на него переключиться.\n\n"
            "В таком случае QuickRes показывает окно с кнопкой для программы управления "
            "графикой: Панель управления NVIDIA, AMD Software или Intel Graphics Software, "
            "в зависимости от того, что обнаружено на вашем компьютере. Нажмите кнопку "
            "и добавьте нужное разрешение как пользовательское:\n\n"
            "NVIDIA: Дисплей > Изменение разрешения > Настройка > "
            "Создать пользовательское разрешение\n\n"
            "AMD: Дисплей > Пользовательские разрешения > Создать новое\n\n"
            "Intel: Дисплей > Общие > Разрешение > + (создать пользовательский профиль)"
        ),
        "faq_q2": "Почему ничего не происходит при нажатии на кнопку?",
        "faq_a2": "Попробуйте запустить QuickRes от имени администратора.",
        "faq_q3": "После переключения изображение выглядит неправильно? (Valorant)",
        "faq_a3": (
            "Перед переключением на растянутое разрешение убедитесь, что Valorant находится "
            "на экране выбора агента. Это нужно делать перед каждым матчем, поскольку при "
            "загрузке новой игры разрешение сбрасывается, а слишком раннее переключение "
            "может привести к появлению чёрных полос. Настройте горячую клавишу ниже, чтобы "
            "переключаться между нативным и растянутым разрешением одним нажатием, не открывая "
            "каждый раз приложение."
        ),
        "faq_q4": "Что делает горячая клавиша?",
        "faq_a4": (
            "После нажатия «Включить» выбранная клавиша будет переключать экран между "
            "указанными выше нативным и растянутым разрешениями. Нажмите её один раз "
            "на экране выбора агента, чтобы перейти на растянутое разрешение, и ещё раз "
            "после матча, чтобы вернуться к нативному.\n\n"
        ),
        "theme_light": "Светлая",
        "theme_dark": "Тёмная",
        # --- Monitors window ---
        "monitors_window_title": "Мониторы",
        "monitor_status_enabled": "Включён",
        "monitor_status_disabled": "Отключён",
        "btn_disable": "Отключить",
        "btn_enable": "Включить",
        # --- Revert / keep-disabled dialog ---
        "revert_dialog_title": "Оставить этот монитор отключённым?",
        "btn_keep_disabled": "Оставить отключённым",
        "btn_revert_now": "Восстановить сейчас",
        # --- Webview panel chrome ---
        "quick_resolutions_label": "Быстрые разрешения",
        "hotkey_state_stopped": "Остановлено",
        "hotkey_state_running": "Прослушивание",
        "notice_title": "Монитор может быть всё ещё отключён",
        "btn_updates": "Обновления",
        "monitors_detected_count": "обнаружено: {count}",
        "btn_force_unlock": "Принудительно разблокировать",
        "revert_note": "Если ничего не делать, QuickRes включит его автоматически.",
        "btn_disable_all": "Отключить все",
        "preset_kind_native": "Нативное",
        "preset_kind_stretched": "Растянутое",
        "preset_kind_low": "Низкое",
        # --- Update-available modal (wires the Updates button to
        # the existing download/verify/apply pipeline) ---
        "update_available_title": "Доступно обновление",
        "update_available_body": "QuickRes {version} готов к установке.",
        "btn_update_now": "Обновить сейчас",
        "btn_later": "Позже",
        "btn_retry_download": "Повторить загрузку",
        "update_downloading": "Загрузка обновления… {percent}%",
        "update_downloading_unknown": "Загрузка обновления…",
        "update_verifying": "Проверка обновления…",
        "update_ready": "Обновление загружено. Перезапуск для установки…",
        "update_installing": "Перезапуск для установки обновления…",
        "update_failed": "Не удалось загрузить обновление. Проверьте подключение и повторите попытку.",
    },
}

LANGUAGE_NAMES = {
    "auto": "Auto",
    "en": "English",
    "ru": "Русский",
}

_current_lang = "auto"

# pywebview dispatches every JS->Python bridge call on
# its own thread (the same concurrency model that config._update_lock /
# Api._op_lock / Api._hotkey_lock all already guard against), and
# set_language()/get_language()/t() had no equivalent protection for this
# module-level variable. Guard both the write and every read with one lock.
_lang_lock = threading.Lock()


def detect_system_language() -> str:
    try:
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        primary = langid & 0x3FF
        if primary == LANG_RUSSIAN:
            return "ru"
    except Exception:
        pass
    return "en"


def resolve_language(setting: str) -> str:
    if setting == "auto" or setting not in STRINGS:
        return detect_system_language()
    return setting


def set_language(lang: str):
    global _current_lang
    with _lang_lock:
        _current_lang = lang if lang in STRINGS else "en"


def get_language() -> str:
    with _lang_lock:
        return _current_lang


def t(key: str, lang: str | None = None, **kwargs) -> str:
    """Look up `key`'s translated string.

    `lang`, when passed, PINS the lookup to
    that exact language instead of re-reading the shared `_current_lang`
    global -- callers that build a multi-key bundle (bridge.py's
    `_ui_strings`/`_faq_bundle`) must resolve the language ONCE and pass the
    same pinned value into every `t()` call in that bundle, otherwise a
    concurrent `set_language()` call on another bridge-dispatch thread can
    land mid-bundle and mix strings from two different languages into one
    response. `lang=None` (the default) preserves the old behavior of
    reading the current global under `_lang_lock` -- used by any caller that
    genuinely wants "whatever language is active right now" for a single,
    standalone lookup.
    """
    if lang is None:
        with _lang_lock:
            lang = _current_lang
    template = STRINGS.get(lang, {}).get(key)
    if not template:
        template = STRINGS["en"].get(key, key)
    return template.format(**kwargs) if kwargs else template
