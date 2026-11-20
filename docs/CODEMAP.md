# CODEMAP — куда идти Codex

## Куда идти по симптому

| Что видит пользователь | С чего начать | Что должно пройти |
|---|---|---|
| F8 пропускает, дублирует или вставляет чужой текст | Найди [событие по capture_id](LOGGING_FOR_AI.md#добавление-текста-и-восстановление-вкладок). Проверь [снимок буфера](../tts_app/windows_hotkey_capture.py) и [доставку во вкладку](../tts_app/app_hotkeys.py). | [Тесты захвата](../tools/test_text_save_capture_regressions.py) и [ручной сценарий](VERIFICATION.md#ручная-проверка-захвата-в-windows). При задержанном обновлении ожидается полный Unicode-текст ровно один раз в назначенной вкладке. |
| При закрытии пропадает текст | [Сохранение вкладок](../tts_app/app_workspace_save.py) и [завершение окна](../tts_app/app_shutdown.py). | [Тесты закрытия](../tools/test_workspace_shutdown.py). При ошибке записи окно остаётся открытым. Повторная попытка сохраняет свежий текст. |
| После смены языка программа не перезапускается | [Карточка настроек](../tts_app/mp3_settings_ui.py), [решение о перезапуске](../tts_app/app_shutdown.py) и [запуск новой копии](../tts_app/app.py). | [Тесты закрытия](../tools/test_workspace_shutdown.py). При сбое сохранения перезапуск отменяется. После успеха открывается та же копия с сохранёнными вкладками. |
| MP3 не возобновляется или готовый файл неполный | [Чтение манифеста восстановления](../tts_app/runtime_recovery.py), затем [этап MP3](WORKER_MAP.md). | [Тесты целостности MP3](../tools/test_mp3_text_integrity.py). При недоступном манифесте фрагменты сохраняются. Готовый MP3 декодируется и проверяется до публикации. |

## Владельцы по подсистемам

| Что меняется | Implementation owner | При необходимости рядом |
|---|---|---|
| запуск программы / composition приложения | `ZeTer Voice.py`, `tts_app/app.py` | — |
| constants, common formatting/process helpers | `tts_app/runtime_core.py` | `tts_app/runtime.py` только для compatibility export |
| UI freeze watchdog, snapshot стека, uncaught callbacks/threads, single-instance Windows mutex | `tts_app/ui_stall_diagnostics.py` | `app_workspace_init.py`, `app_shutdown.py`, `app.py` |
| ускорение огромных строк, text metrics cache и word wrap | `tts_app/task_text_performance.py` | `task_ui_build.py`, `task_ui_status.py`, `task_capture.py` |
| compact text diagnostics | `tts_app/runtime_diagnostics.py` | `runtime_core.py` |
| WAV/ffprobe/final MP3 validation/disk forecast | `tts_app/runtime_audio.py` | `ffmpeg_tools.py` |
| recovery scan / existing WAV checkpoints | `tts_app/runtime_recovery.py` | `runtime_audio.py` |
| Ctrl+C/V/X/A/Z/Y Tk shortcuts | `tts_app/runtime_shortcuts.py` | — |
| paths/settings/atomic write/backups/help files | `tts_app/runtime_storage.py` | `runtime_core.py` |
| compatibility imports старого `runtime` | `tts_app/runtime.py` | implementation owner выше |
| logger compact/redaction/error classification | `tts_app/problem_logger_compact.py` | `problem_logger_events.py`, `docs/LOGGING_FOR_AI.md` |
| logger session lifecycle / stale sessions | `tts_app/problem_logger_session.py` | `problem_logger.py` facade |
| безопасное истечение срока диагностических сессий | `tts_app/problem_log_retention.py` | `problem_logger_session.py`, `runtime_storage.py` |
| logger warnings/observations/diagnostic insights | `tts_app/problem_logger_insights.py` | `problem_logger_summary.py` |
| AI_READ_FIRST / diagnostic summary | `tts_app/problem_logger_summary.py` | `problem_logger_compact.py`, insights/events |
| logger events/stats/errors/task lifecycle | `tts_app/problem_logger_events.py` | — |
| разбиение текста / bookmark segment helpers | `tts_app/text_processing.py` | — |
| SAPI voices/devices/synthesize WAV | `tts_app/sapi_core.py` | `runtime_core.py` |
| дополнительные голоса, hash/путь модели | `tts_app/voice_catalog.py` | `docs/VOICE_ENGINES.md` |
| выбор Python для worker/installer | `tts_app/engine_runtime.py` | [подготовка среды EXE в полном репозитории](https://github.com/zeter1/ZeTer-Voice/blob/main/build_support/prepare_engine_runtime.py) |
| вкладка установки и удаления движков в настройках | `tts_app/engine_manager_ui.py` | `mp3_settings_ui.py`, `app_shutdown.py` |
| additive установка / восстановимое удаление | `tts_app/engine_installation.py` | `tools/manage_voice_engine.py`, `voice_catalog.py` |
| Silero/Edge worker и отменяемый синтез WAV | `tts_app/voice_worker.py`, `tts_app/voice_process.py` | `tools/setup_voices.py` |
| прослушивание Silero/Edge через SAPI SpeakStream | `tts_app/generated_preview.py` | `sapi_preview_diagnostics.py`, `voice_process.py` |
| SAPI live preview stream / подтверждение блока | `tts_app/sapi_preview.py` | `sapi_core.py`, `text_processing.py` |
| SAPI scalar snapshots / audio-delta oracle / exception COM lifetime | `tts_app/sapi_preview_diagnostics.py` | `sapi_preview.py`, `docs/LOGGING_FOR_AI.md` |
| FFmpeg executable/concat/final MP3 | `tts_app/ffmpeg_tools.py` | `runtime_audio.py` |
| Windows autostart / открыть папку | `tts_app/windows_autostart.py` | `runtime_storage.py` |
| hotkey config/parse/enable/duplicate state | `tts_app/windows_hotkey_config.py` | `windows_hotkeys.py` composition |
| чтение Unicode, подтверждение владельца буфера и доставка снимка в Tk | `tts_app/windows_hotkey_capture.py` | `windows_hotkey_hook.py`, [контракт захвата](PROJECT.md#захват-текста) |
| Windows keyboard hook, очередь команд копирования, проверка клавиш и SendInput | `tts_app/windows_hotkey_hook.py` | hotkey capture |
| строка текстовых вкладок, закрытие и кнопка «+» | `tts_app/widgets.py` | `app_tabs.py` |
| белая тема окон и элементов Tk | `tts_app/workspace_theme.py` | `app_workspace_init.py` |
| вкладка «О программе» и контакты | `tts_app/about_ui.py` | `app_workspace_init.py` |
| язык интерфейса и словари | `tts_app/i18n.py`, `tts_app/ui_messages_*.py` | `mp3_settings_ui.py` и `app_shutdown.py` для применения при перезапуске |
| вкладка «Настройки», MP3, автозапуск и логи проблем | `tts_app/mp3_settings_ui.py` | `runtime_storage.py`, `mp3_format.py`, `app_recovery.py` |
| редактор, панель чтения и MP3 | `tts_app/task_ui_build.py` | `task_ui_state.py` |
| боковая панель голоса, звука и захвата текста | `tts_app/task_ui_sidebar.py` | `app_audio_settings.py`, `app_hotkeys.py` |
| counters/queue/text-state/delete logging | `tts_app/task_ui_status.py` | — |
| voice/title/workspace record/task UI state | `tts_app/task_ui_state.py` | — |
| MP3 source snapshot/completion tracking | `tts_app/task_source_tracking.py` | — |
| global selection + clipboard paste | `tts_app/task_capture.py` | `windows_hotkey_capture.py` |
| размещение текста и перенос из прежних папок | `tts_app/runtime_storage.py` | `app_workspace_init.py` |
| архивирование прежнего текста и очистка по сроку | `tts_app/workspace_text_history.py` | `app_workspace_save.py`, `app_workspace_init.py` |
| загрузить файл / очистить / получить весь текст | `tts_app/task_file_io.py` | `sapi_core.py` для read_text_file |
| preview bookmark/selection/start state | `tts_app/task_preview_state.py` | `text_processing.py` |
| preview worker/start/done | `tts_app/task_preview_player.py` | `sapi_preview.py` |
| удаление прочитанного + appended tail | `tts_app/task_preview_delete.py` | preview player |
| pause/resume/stop/final preview UI state | `tts_app/task_preview_controls.py` | preview player |
| кнопки/start/cancel/recovery создания MP3 | `tts_app/task_conversion_controls.py` | worker только при pipeline-проблеме |
| тяжёлый pipeline `_job_worker` | `tts_app/task_conversion_worker.py` | **сначала `docs/WORKER_MAP.md`**, затем SAPI/FFmpeg/runtime owner |
| главное окно: init/build/restore workspace | `tts_app/app_workspace_init.py` | `task_tab.py` |
| создать/переименовать/закрыть вкладку | `tts_app/app_tabs.py` | workspace init |
| запись настроек и реакция на её сбой | `tts_app/app_audio_settings.py` | `runtime_storage.py`, `app_shutdown.py` |
| автосохранение вкладок, временный манифест и восстановление при сбое записи | `tts_app/app_workspace_save.py` | `runtime_storage.py`, `app_workspace_init.py` |
| recovery dialogs | `tts_app/app_recovery.py` | runtime recovery |
| назначение tab hotkeys, доставка текста во вкладку и подсказки при отказе | `tts_app/app_hotkeys.py` | `windows_hotkey_*`, `task_capture.py` |
| причины отказа захвата и действия для диагностики | `tts_app/hotkey_capture_guidance.py` | `app_hotkeys.py`, `ui_messages_capture_notifications.py` |
| голоса/устройства/общие audio settings UI | `tts_app/app_audio_settings.py` | `sapi_core.py` |
| обработка app event queue | `tts_app/app_event_dispatch.py` | producer конкретного события |
| уведомления о готовом MP3 и последнем сбое захвата | `tts_app/conversion_notifications.py` | `app_event_dispatch.py` и `app_hotkeys.py` на главном потоке Tk |
| оформление и содержимое предупреждения захвата | `tts_app/capture_notification.py` | `conversion_notifications.py`, `workspace_theme.py` |
| shutdown / close / background threads | `tts_app/app_shutdown.py` | event dispatch |

## Composition/facade files

Обычно их **не надо читать глубоко**: `runtime.py`, `sapi.py`, `windows_tools.py`, `problem_logger.py`, `task_ui.py`, `task_sources.py`, `task_preview_runtime.py`, `app_events.py`, `app_workspace.py`, `app_recovery_hotkeys.py`, `task_tab.py`, `app.py`. Они существуют ради совместимости/MRO и указывают на implementation owner.

## Маршрут изменения

1. `python -B tools/where.py WindowsCopyCapture` находит определения. Для поиска вызовов используй `rg -n "WindowsCopyCapture" tts_app tools`.
2. Открой файл, который отвечает за поведение, и нужный участок.
3. Если для проверки нужно больше контекста, открыть ближайшего вызывающего, владельца состояния и тест.
4. Для `_job_worker` выбери [этап по метке](WORKER_MAP.md).
5. После правки проверить сценарий и запустить [проверки на отдельной копии](VERIFICATION.md).
