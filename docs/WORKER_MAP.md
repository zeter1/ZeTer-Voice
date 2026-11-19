# MP3 WORKER MAP — точечное чтение `_job_worker`

`tts_app/task_conversion_worker.py` намеренно оставлен одним транзакционным pipeline: recovery checkpoints, error context и cleanup используют общее локальное состояние. Вместо рискованного переписывания функции Codex должен читать её по `CODEX-REGION` диапазонам.

| Region | Lines | Что там |
|---|---:|---|
| `worker-bootstrap` | 31–129 | pywin32 init, counters, hashes, base context |
| `split-output-recovery` | 130–342 | split, output dir, FFmpeg discovery, recovery manifest, disk baseline |
| `sapi-generation` | 343–750 | chunk loop, confirmed WAV checkpoints, retries, diagnostics |
| `ffmpeg-assembly` | 751–1011 | concat to a staged file, progress callback, cancellation |
| `validate-success` | 1012–1264 | full decode and sample count, atomic publication, success events |
| `cancellation` | 1265–1319 | InterruptedError / user cancel path |
| `failure-recovery` | 1320–1463 | error diagnostics, recovery preservation, user message |
| `final-cleanup` | 1464–1477 | staged-file cleanup, stage reset, CoUninitialize |

Быстрый поиск:

```bash
python tools/where.py CODEX-REGION
python tools/where.py report_ffmpeg_progress
```

Если задача относится к одной стадии, открывай только её range + прямой helper (`sapi_*`, `ffmpeg_tools.py`, `runtime_recovery.py` и т.п.). Полный worker нужен только для cross-stage lifecycle/recovery изменений.
