"""Validation and independent child workers for the desktop-pet interface."""

from dataclasses import dataclass
import argparse
import importlib.util
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import uuid


ROOT = Path(__file__).resolve().parent
CLICKER = ROOT / 'clicker'
COLLECTOR = ROOT / 'collector'


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_clicker = _load('_pet_clicker_parameters', CLICKER / 'macro.py')
_monitor = _load('_pet_monitor_parameters', COLLECTOR / 'monitoring.py')
_claim_history = _load('_pet_claim_history', COLLECTOR / 'claim_history.py')
read_claim_history = _claim_history.read_runs
claim_summary = _claim_history.summary
CLAIM_STATUS = _claim_history.LABELS


def clicker_options(count, interval, hours='0'):
    count = _clicker.parse_count(count.strip() or '2000')
    interval = _clicker.parse_interval(interval.strip() or '0.1')
    hours = _clicker.parse_hours(hours.strip() or '0')
    if count == 0 and hours == 0:
        raise ValueError('次数与运行时长至少填写一项，避免无限运行。')
    return ['--count', str(count), '--interval', str(interval), '--hours', str(hours)]


def collector_options(hours, minutes, idle='0', idle_interval='0.1'):
    try:
        duration = _monitor.parse_hours(hours.strip() or '6')
        seconds = float(minutes.strip() or '15') * 60
        if not math.isfinite(seconds) or seconds < 0.5:
            raise ValueError('检查间隔须为至少 0.5 秒的有限数值，请以分钟填写。')
        clicks = _clicker.parse_count(str(idle).strip() or '0')
        click_interval = _clicker.parse_interval(str(idle_interval).strip() or '0.1')
    except (ValueError, TypeError, argparse.ArgumentTypeError) as error:
        raise ValueError(str(error)) from None
    options = ['--hours', str(duration), '--interval', str(seconds)]
    if clicks:
        # Gap clicks run between rounds inside the collector, so one step alternates
        # claiming and clicking instead of running the two workers one after another.
        options += ['--idle-count', str(clicks), '--idle-interval', str(click_interval)]
    return options


PLAN_FIELDS = {'clicker': ('count', 'interval', 'duration'),
               'collector': ('hours', 'minutes', 'idle', 'idle_interval')}


def step_options(step):
    """Validate one plan step and return the worker arguments it needs."""
    kind = step.get('kind') if isinstance(step, dict) else None
    if kind == 'clicker':
        parser = clicker_options
    elif kind == 'collector':
        parser = collector_options
    else:
        raise ValueError('计划步骤的任务类型无效。')
    return parser(*(str(step.get(key, '')) for key in PLAN_FIELDS[kind]))



def plan_options(step):
    options = step_options(step)
    # --plan makes the worker exit with 3 when stopped, so later steps stay off.
    return options + ['--plan']


def step_summary(step):
    options = step_options(step)
    if step['kind'] == 'clicker':
        return f"连点 {options[1]} 次 · 间隔 {options[3]} 秒 · 时长 {options[5]} 小时"
    summary = f"监测 {options[1]} 小时 · 每 {float(options[3]) / 60:g} 分钟检查"
    if len(options) > 4:
        summary += f" · 间隙连点 {options[5]} 次"
    return summary


class PlanRunner:
    """Run plan steps in order; a stopped or failed step ends the plan."""

    def __init__(self, workers):
        self.workers = workers
        self.steps = []
        self.position = -1
        self.running = False

    def current(self):
        if 0 <= self.position < len(self.steps):
            return self.steps[self.position]
        return None

    def start(self, steps):
        if self.running:
            raise ValueError('计划已经在运行。')
        if not steps:
            raise ValueError('计划中还没有任务。')
        self.steps = list(steps)
        self.position = 0
        self.running = True
        try:
            self.workers.start(*self.steps[0])
        except Exception:
            self.running = False
            raise

    def advance(self, kind, code):
        """Called after a worker exits; return started/done/stopped or None."""
        if not self.running:
            return None
        current = self.current()
        if current is None or kind != current[0]:
            return None
        if code != 0:
            self.running = False
            return 'stopped'
        if self.position + 1 >= len(self.steps):
            self.running = False
            return 'done'
        self.position += 1
        try:
            self.workers.start(*self.steps[self.position])
        except Exception:
            self.running = False
            raise
        return 'started'

    def abort(self):
        self.running = False


SCRIPTS = {'clicker': (CLICKER / 'macro.py', []),
           'collector': (COLLECTOR / 'auto_collect.py', []),
           'preview': (COLLECTOR / 'auto_collect.py', ['--preview']),
           'test': (COLLECTOR / 'auto_collect.py', ['--once']),
           'check_clicker': (CLICKER / 'macro.py', ['--check']),
           'check_collector': (COLLECTOR / 'auto_collect.py', ['--check'])}


@dataclass
class Worker:
    process: object
    stop_file: Path
    ui_run_id: str = ''
    reader_done: object = None
    options: tuple = ()


class Workers:
    def __init__(self, runtime=None):
        self.runtime = Path(runtime) if runtime is not None else ROOT / 'runtime'
        self.active = {}
        self.messages = queue.Queue(maxsize=4096)

    def running(self, kind):
        worker = self.active.get(kind)
        return worker is not None and worker.process.poll() is None

    def gap_clicks(self, kind):
        """Gap clicks of a worker as launched; editing the form later changes nothing."""
        worker = self.active.get(kind)
        options = list(worker.options) if worker is not None else []
        try:
            return int(options[options.index('--idle-count') + 1])
        except (ValueError, IndexError):
            return 0

    def start(self, kind, options):
        if kind not in SCRIPTS:
            raise ValueError('未知功能。')
        if self.running(kind):
            raise ValueError('这项任务已经在运行。')
        if kind in ('collector', 'test') and any(
                self.running(other) for other in ('collector', 'test') if other != kind):
            raise ValueError('自动领取与单次领取测试不能同时运行。')
        self.runtime.mkdir(parents=True, exist_ok=True)
        signal = self.runtime / f'{kind}-{uuid.uuid4().hex}.stop'
        script, extra = SCRIPTS[kind]
        # Only the long-running collector reports structured progress to the UI.
        token = uuid.uuid4().hex if kind == 'collector' else ''
        telemetry = ['--ui-events', '--ui-run-id', token] if token else []
        if getattr(sys, 'frozen', False):
            # A frozen exe cannot run a data-file script directly; re-enter the
            # exe through --job so the child never opens a second main window.
            command = [str(Path(sys.executable)), '--job', kind, *options,
                       '--stop-file', str(signal), *telemetry]
        else:
            python = Path(sys.executable)
            if python.name.casefold() == 'pythonw.exe':
                python = python.with_name('python.exe')
            command = [str(python), '-u', str(script), *extra, *options,
                       '--stop-file', str(signal), *telemetry]
        environment = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        process = subprocess.Popen(command, cwd=str(script.parent), env=environment,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding='utf-8',
                                   errors='replace', bufsize=1,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        reader_done = threading.Event()
        self.active[kind] = Worker(process, signal, token, reader_done, tuple(options))
        threading.Thread(target=self._read, args=(kind, process, reader_done), daemon=True).start()

    def _read(self, kind, process, reader_done=None):
        try:
            for line in process.stdout:
                line = line.strip()
                if line:
                    self.messages.put((kind, line))
        finally:
            process.stdout.close()
            if reader_done is not None: reader_done.set()

    def stop(self, kind):
        if self.running(kind):
            self.active[kind].stop_file.touch(exist_ok=True)

    def stop_all(self):
        for kind in tuple(self.active):
            self.stop(kind)

    def finished(self):
        completed = []
        for kind, worker in tuple(self.active.items()):
            code = worker.process.poll()
            # Wait for the reader thread so the last lines are not dropped.
            if code is not None and (worker.reader_done is None or worker.reader_done.is_set()):
                worker.stop_file.unlink(missing_ok=True)
                completed.append((kind, code))
                del self.active[kind]
        return completed
