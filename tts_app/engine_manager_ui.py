"""Persistent engine settings page. Background work reports through a Tk-owned queue."""
import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
from .engine_installation import engine_status
from .engine_runtime import engine_python, engine_environment, program_root


def engine_operation_busy(app) -> bool:
    manager = getattr(app, 'engine_manager', None)
    return bool(manager and manager.busy)


def audio_active(app) -> bool:
    return any((getattr(task, 'worker', None) and task.worker.is_alive()) or
               (getattr(task, 'preview_thread', None) and task.preview_thread.is_alive())
               for task in app.tabs.values())


def _stop_owned_process(process) -> tuple[bool, bool]:
    """Report whether the tree stop and the root process exit were confirmed."""
    windows = sys.platform == 'win32'
    tree_stopped = not windows
    try:
        if windows:
            result = subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=4, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            tree_stopped = result.returncode == 0
        else:
            process.terminate()
        if tree_stopped:
            process.wait(timeout=2)
            return True, True
    except (OSError, subprocess.TimeoutExpired):
        pass
    # Reap the exact process even if the tree command fails. This cannot prove
    # that an installer child left behind by a failed tree command exited.
    try:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return tree_stopped, False
    return tree_stopped, True


class EngineManager:
    def __init__(self, app, parent):
        self.app = app
        self.root = app.root
        self.frame = ttk.Frame(parent, padding=14)
        self.messages = queue.Queue()
        self.stop = threading.Event()
        self.busy = False
        self.closed = False
        self.thread = None
        self.pending_process = None
        self.tree_stop_unconfirmed = False
        self.after_id = None
        self.status_generation = 0
        self.statuses = {}
        self.buttons = []
        ttk.Label(self.frame, text='Установленные компоненты не обновляются автоматически.',
                  wraplength=720).pack(anchor='w', pady=(0, 10))
        cards = ttk.Frame(self.frame)
        cards.pack(fill='x', pady=(0, 8))
        cards.columnconfigure((0, 1), weight=1, uniform='engine_cards')
        for column, (engine, title, detail) in enumerate([
            ('silero', 'Silero · 5 русских голосов', 'Без интернета после установки. Модель около 148 МБ; при отсутствии PyTorch потребуется дополнительная загрузка.'),
            ('edge', 'Microsoft Edge · Светлана и Дмитрий', 'Нужен интернет. При озвучке текст передаётся Microsoft. Устанавливается отдельная копия для этой программы.'),
        ]):
            card = ttk.LabelFrame(cards, text=title, padding=10)
            card.grid(row=0, column=column, sticky='nsew', padx=(0, 10) if column == 0 else 0)
            ttk.Label(card, text=detail, wraplength=350).pack(anchor='w')
            value = tk.StringVar(value='Проверяю…')
            self.statuses[engine] = value
            ttk.Label(card, textvariable=value, wraplength=350).pack(anchor='w', pady=6)
            row = ttk.Frame(card)
            row.pack(anchor='w')
            for action, label in [('install', 'Установить'), ('remove', 'Удалить')]:
                button = ttk.Button(row, text=label, command=lambda e=engine, a=action: self.start(a, e))
                button.pack(side='left', padx=(0, 8))
                self.buttons.append(button)
        ttk.Label(self.frame, text='Windows SAPI и сторонние системные голоса остаются без изменений.\n'
                  'При удалении сохраняется резервная копия. Ваши тексты, MP3 и настройки не удаляются.',
                  wraplength=720).pack(anchor='w', pady=4)
        controls = ttk.Frame(self.frame)
        controls.pack(fill='x', pady=(4, 0))
        self.cancel = ttk.Button(controls, text='Отменить операцию', command=self.stop.set, state='disabled')
        self.cancel.pack(side='left')
        self.progress = ttk.Progressbar(controls, mode='indeterminate')
        self.progress.pack(side='left', fill='x', expand=True, padx=(12, 0))
        self.message = tk.StringVar(value='')
        ttk.Label(self.frame, textvariable=self.message, wraplength=720).pack(anchor='w', pady=(6, 0))
        self.frame.bind('<Destroy>', self._destroyed, add='+')
        self.refresh()
        self.after_id = self.root.after(100, self.poll)

    def _destroyed(self, event):
        if event.widget == self.frame:
            self.close()

    def refresh(self):
        self.status_generation += 1
        generation = self.status_generation

        def inspect():
            try:
                result = {engine: engine_status(engine) for engine in ('silero', 'edge')}
            except Exception:
                result = {engine: 'Не удалось проверить файлы' for engine in ('silero', 'edge')}
            self.messages.put(('status', (generation, result)))
        threading.Thread(target=inspect, daemon=True, name='engine-status').start()

    def start(self, action, engine):
        if (self.closed or self.busy or self.tree_stop_unconfirmed or self.pending_process is not None
                or getattr(self.app, 'shutdown_in_progress', False)):
            return
        if audio_active(self.app):
            messagebox.showinfo('Голосовые движки', 'Сначала остановите прослушивание и создание MP3.', parent=self.root)
            return
        if action == 'remove' and not messagebox.askyesno('Удалить движок?',
                'Удалить выбранный движок из программы? Резервная копия будет сохранена.\n'
                'Windows-голоса, настройки и ваши тексты останутся без изменений.', parent=self.root, default='no'):
            return
        # A confirmation dialog runs a nested event loop: hotkeys/close may have run.
        if (self.closed or self.busy or self.tree_stop_unconfirmed or self.pending_process is not None
                or getattr(self.app, 'shutdown_in_progress', False) or audio_active(self.app)):
            return
        self.busy = True
        self.stop.clear()
        for button in self.buttons:
            button.configure(state='disabled')
        self.cancel.configure(state='normal')
        self.progress.start()
        self.message.set('Устанавливаю… Для загрузки нужен интернет.' if action == 'install' else 'Удаляю с сохранением копии…')
        self.thread = threading.Thread(target=self.run, args=(action, engine), daemon=True, name='engine-management')
        self.thread.start()

    def run(self, action, engine):
        process = None
        stop_reason = None
        result = 'Операция не завершена. Проверьте интернет и доступ к папке программы.'
        try:
            script = program_root() / 'tools' / 'manage_voice_engine.py'
            with tempfile.TemporaryFile() as output:
                process = subprocess.Popen([engine_python(), str(script), action, engine], stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.DEVNULL, env=engine_environment(),
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                deadline = time.monotonic() + 1800
                while process.poll() is None:
                    if self.stop.wait(0.05):
                        stop_reason = 'cancelled'
                        break
                    if time.monotonic() >= deadline:
                        stop_reason = 'timeout'
                        break
                if process.poll() is not None:
                    output.seek(0)
                    payload = json.loads(output.read(4096))
                    if payload['ok'] is (process.returncode == 0) and isinstance(payload['message'], str):
                        result = payload['message'][:1000]
        except Exception:
            pass
        finally:
            pending_process = None
            tree_stop_unconfirmed = False
            try:
                if process is not None and process.poll() is None:
                    tree_stopped, process_exited = _stop_owned_process(process)
                    tree_stop_unconfirmed = not tree_stopped
                    if not process_exited:
                        pending_process = process
                    elif stop_reason == 'cancelled':
                        result = 'Операция остановлена. Успевшие установиться компоненты сохранены.'
                    elif stop_reason == 'timeout':
                        result = 'Время операции истекло. Успевшие установиться компоненты сохранены.'
            except Exception:
                pending_process = process
                tree_stop_unconfirmed = process is not None
            finally:
                if tree_stop_unconfirmed:
                    result = ('Не удалось подтвердить остановку дерева процессов. '
                              'Установка и удаление заблокированы в текущем сеансе.')
                elif pending_process is not None:
                    result = ('Не удалось подтвердить остановку процесса установки. '
                              'Управление будет доступно после его выхода.')
                self.messages.put(('done', (result, pending_process, tree_stop_unconfirmed)))

    def _refresh_after_operation(self):
        if not self.tree_stop_unconfirmed:
            for button in self.buttons:
                button.configure(state='normal')
        self.refresh()
        self.app._load_voices_async()

    def poll(self):
        self.after_id = None
        if self.closed:
            return
        for _ in range(8):
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == 'status':
                generation, statuses = payload
                if generation == self.status_generation:
                    for engine, value in statuses.items():
                        self.statuses[engine].set(value)
            elif kind == 'done':
                message, self.pending_process, self.tree_stop_unconfirmed = payload
                self.busy = False
                self.message.set(message)
                self.progress.stop()
                self.cancel.configure(state='disabled')
                if self.pending_process is None:
                    self._refresh_after_operation()
        if self.pending_process is not None:
            try:
                exited = self.pending_process.poll() is not None
            except OSError:
                exited = False
            if exited:
                self.pending_process = None
                self._refresh_after_operation()
        self.after_id = self.root.after(100, self.poll)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stop.set()
        if self.after_id is not None:
            self.root.after_cancel(self.after_id)
            self.after_id = None
