# Проверка изменений ZeTer Voice

## Изолированная копия исходников

Сначала проверь, из какой папки запускается изменяемая программа и какой экземпляр создал логи. Для Python папка данных вычисляется от исходного файла, для EXE — от исполняемого файла. При недоступной записи программа может использовать `%LOCALAPPDATA%/TextToMp3Irina`. Даже импорт пакета подготавливает папки и проверяет запись. [Пути и данные](PROJECT.md#постоянные-данные)

Из корня исходников определи в PowerShell блок ниже. Для каждого запуска передай через `-CheckParent` существующую папку, в которую разрешена запись. Codex может указать папку `work` текущей задачи. Блок создаёт чистую копию `.py`, проверяет запись и задаёт отдельные пути для резервной папки данных и временных файлов Python.

```powershell
$verifyZeTerVoice = {
    param(
        [Parameter(Mandatory)]
        [string] $CheckParent,
        [string[]] $CheckArgs = @('tools/verify.py')
    )

    $ErrorActionPreference = 'Stop'
    $source = (Resolve-Path -LiteralPath '.').ProviderPath
    $parent = (Resolve-Path -LiteralPath $CheckParent).ProviderPath
    $entry = Join-Path $source 'ZeTer Voice.py'
    $gate = Join-Path $source 'tools/verify.py'
    if (!(Test-Path -LiteralPath $entry -PathType Leaf) -or
        !(Test-Path -LiteralPath $gate -PathType Leaf)) {
        throw 'Откройте корень исходников ZeTer Voice.'
    }

    $mirror = Join-Path $parent ('ZeTerVoice-check-' + [Guid]::NewGuid().ToString('N'))
    $temporary = Join-Path $mirror '.validation-temp'
    $localData = Join-Path $mirror '.validation-localappdata'
    New-Item -ItemType Directory -Path $mirror, $temporary, $localData | Out-Null
    Copy-Item -LiteralPath $entry -Destination $mirror
    foreach ($name in 'tts_app','tools') {
        $destination = Join-Path $mirror $name
        New-Item -ItemType Directory -Path $destination | Out-Null
        Get-ChildItem -LiteralPath (Join-Path $source $name) -File -Filter '*.py' |
            ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $destination }
    }
    $probe = Join-Path $mirror '.check-write'
    [IO.File]::WriteAllText($probe, 'ok')
    Remove-Item -LiteralPath $probe

    $checkEnvironment = @{
        LOCALAPPDATA = $localData
        TMPDIR = $temporary
        TEMP = $temporary
        TMP = $temporary
    }
    $previousEnvironment = @{}
    foreach ($name in $checkEnvironment.Keys) {
        $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    }
    Push-Location -LiteralPath $mirror
    try {
        foreach ($name in $checkEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable($name, $checkEnvironment[$name], 'Process')
        }
        Write-Host "Копия для проверки: $mirror"
        python -B @CheckArgs
        if ($LASTEXITCODE -ne 0) { throw "Проверка завершилась с кодом $LASTEXITCODE." }
    } finally {
        foreach ($name in $checkEnvironment.Keys) {
            $previous = $previousEnvironment[$name]
            if ($null -eq $previous) {
                [Environment]::SetEnvironmentVariable($name, [NullString]::Value, 'Process')
            } else {
                [Environment]::SetEnvironmentVariable($name, $previous, 'Process')
            }
        }
        Pop-Location
    }
}
```

Замените путь на свою папку и выполните общую или ближайшую проверку.

~~~powershell
& $verifyZeTerVoice -CheckParent 'D:\Work\ZeTerVoice-checks'
& $verifyZeTerVoice -CheckParent 'D:\Work\ZeTerVoice-checks' -CheckArgs @('tools/test_workspace_shutdown.py')
~~~

В копию для проверки не нужны пользовательские настройки, тексты, логи, установленные движки и резервные копии. Используй вымышленные настройки, тестовую озвучку или предусмотренные тестами подмены. Для сборки EXE нужен полный репозиторий с файлами сборки. Системная регистрация, реальный звук, сеть и рабочий буфер требуют отдельного, явно выбранного сценария.

Результат общего запуска — `verify: OK` и код выхода 0. Скрипт проверяет компиляцию, импорты, границы модулей и регрессии, затем создаёт `docs/SYMBOL_INDEX.md`, `docs/SYMBOL_INDEX.json` и `docs/MODULE_SIZES.json` в копии. Если исходники изменились, сравни и перенеси актуальные индексы после проверки.

Прочитай вывод отдельных тестов и отметь `skipped` с причиной. Тест параметров MP3 проверяет аудиопоток, если `ffmpeg` и `ffprobe` доступны через `PATH`. Пакета `imageio-ffmpeg` может хватить для конвертации в программе, а наличие обоих инструментов проверяется отдельно. Проверка окон Tk требует `--gui`. Реальные SAPI, аудиоустройство, горячая клавиша и собранный EXE проверяются своими сценариями.

Для документации сверяй локальные ссылки и заголовки, команды с фактическими скриптами и метки MP3 с исходниками. Исторические отчёты сохраняют результаты своих запусков.

## Проверки по подсистемам

Команды из таблицы запускаются в подготовленной копии. Для локального сбоя начни с ближайшей проверки. Общий скрипт запускает тесты без `--gui`, поэтому проверку окон уведомлений указывай отдельно.

| Изменение | Ближайшие проверки | Что подтвердить |
|---|---|---|
| Глобальный захват и буфер | `tools/test_text_save_capture_regressions.py`, `tools/test_capture_large_text.py` | Два разных фрагмента, точный Unicode и переносы, правильная вкладка, один результат на запрос. При смене владельца или устаревшем назначении лишней вставки нет. |
| Сохранение, закрытие, история текста | `tools/test_workspace_shutdown.py`, `tools/test_workspace_text_history.py` | Ошибка записи оставляет окно открытым. После повторной попытки свежий текст и вкладки читаются с диска. При восстановлении защищены прежний текст и новый ввод. |
| Настройки, вкладки и уведомления | `tools/test_workspace_layout.py`, `tools/test_conversion_notifications.py --gui` | Настройки сохраняются, вкладки остаются доступны. Карточка захвата видна при свёрнутом окне, сохраняется после успеха и закрывается крестиком. |
| Логи и зависание интерфейса | `tools/test_logging_compact.py`, `tools/test_ui_diagnostics.py` | Причина и проверка читаются в нужной сессии, текст пользователя не попадает в события. Проверяются выключенная запись, очистка по сроку и независимая запись о сбое логгера. |
| Чтение и устройства | `tools/test_preview_audio.py`, `tools/test_runtime_resilience.py`, `tools/test_reliability_followup.py` | Подтверждение аудиоблока, остановка, смена устройства и сохранность неподтверждённого текста. Для SAPI и конкретного устройства нужен отдельный сеанс Windows. |
| MP3 и восстановление | `tools/test_mp3_preparation.py`, `tools/test_mp3_text_integrity.py` | Снимок текста остаётся неизменным для задания. При отмене, восстановлении или занятом целевом имени сохраняются нужные файлы. Для проверки результата учитывай декодирование и число отсчётов. |
| Дополнительные движки | `tools/test_extra_voices.py`, `tools/test_engine_manager.py`, `tools/test_engine_runtime.py` | Путь воркера, отмена установки, проверки модели и сохранность другого движка. Отдельно проверь настоящий синтез нужным движком, если менялась его интеграция. |

Для проверки окон передай `-CheckArgs @('tools/test_conversion_notifications.py', '--gui')`. При первом сбое сохрани команду и этап. Повтор запуска после изменения проверяет новое состояние.

## Ручная проверка захвата в Windows

1. Запусти именно обновлённый `ZeTer Voice.py`. Для проверки EXE отдельно укажи, какая сборка запущена.
2. Назначь клавишу одной тестовой вкладке. Выдели в Блокноте первый вымышленный фрагмент, а в другой программе второй. Включи разные строки и символы. По очереди нажми назначенную клавишу в обоих окнах и сверь полный текст и порядок появления во вкладке. Повтори с сочетанием, например Ctrl+F9. Отдельно нажми назначенную клавишу до отпускания мыши при выделении и проверь, что текст приходит после отпускания.
3. В отдельной попытке нажми клавишу без выделения в Блокноте. При отказе проверь карточку и причину в `AI_READ_FIRST.json` текущей сессии. Следующий успешный захват должен добавить текст и оставить предупреждение до закрытия крестиком.
4. Если сбой происходит только в одном приложении, повтори с тем же вымышленным текстом и сохрани время, `capture_id`, причину и этап. [Как читать события захвата](LOGGING_FOR_AI.md#добавление-текста-и-восстановление-вкладок)

Проверка с имитацией Win32 или COM подтверждает программную логику. Отдельно отмечай, что было запущено и физически проверено. Для неподтверждённого оборудования, приложения или сборки указывай `NOT_VERIFIED`.
