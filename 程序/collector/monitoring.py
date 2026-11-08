"""Bounded monitoring and interruptible scheduling, independent of desktop input."""

import argparse
import math
import time


def parse_hours(value):
    try:
        hours = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError('请输入大于 0 的小时数，例如 6 或 0.5。')
    if not math.isfinite(hours) or hours <= 0 or not math.isfinite(hours * 3600):
        raise argparse.ArgumentTypeError('运行时长必须是大于 0 的有限小时数。')
    return hours


def prompt_hours(read=input, write=print):
    while True:
        try:
            value = read('请输入本次运行时长（小时，例如 6；支持 0.5，输入 q 取消）：').strip()
        except EOFError:
            write('未填写运行时长，已取消启动。')
            return None
        if value.casefold() in ('q', 'quit', 'exit'):
            return None
        try:
            return parse_hours(value)
        except argparse.ArgumentTypeError as error:
            write(str(error))


class MonitorControl:
    def __init__(self, event, duration_seconds=None, clock=None):
        self.event = event
        self.clock = clock or time.monotonic
        self.started = self.clock()
        self.deadline = None if duration_seconds is None else self.started + duration_seconds
        self.expired = False

    def stopped(self):
        if self.deadline is not None and self.clock() >= self.deadline:
            self.expired = True
            self.event.set()
        return self.event.is_set()

    def wait(self, seconds):
        if self.stopped():
            return True
        seconds = max(0, seconds)
        if self.deadline is not None:
            seconds = min(seconds, max(0, self.deadline - self.clock()))
        self.event.wait(seconds)
        return self.stopped()


def run_monitor(attempt, control, interval=900, once=False, report=None, max_attempts=5,
                gap=None, ready_rechecks=0, click_finished_at=None):
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError('检测间隔必须大于 0 且有限。')
    if not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError('尝试次数必须是正整数。')
    rechecks = 0 if once else min(ready_rechecks, math.ceil(interval) - 1)
    cadence_start = control.started
    next_check = cadence_start

    def notify(status, detail=None):
        if report is not None:
            report(status, detail)

    while not control.stopped():
        control.wait(max(0, next_check-control.clock()))
        if control.stopped():
            break
        notify('cycle')
        round_click = None
        # At most two chests can exist. Reobserve before the second click.
        for index in range(1 if once else 2):
            status = None
            limit = 1 if once else max_attempts
            for number in range(1, limit + 1):
                if control.stopped():
                    break
                try:
                    status = attempt()
                    # Keep observations for a late chest separate from click retries.
                    if number == 1 and status == 'waiting' and rechecks:
                        notify('rechecking', rechecks)
                        for _ in range(rechecks):
                            if control.wait(1):
                                status = 'stopped'
                                break
                            status = attempt()
                            if status != 'waiting':
                                break
                except (OSError, RuntimeError) as error:
                    status = 'error'
                    notify('error', str(error))
                    break
                notify(status)
                if status != 'unverified':
                    break
                if number == limit:
                    if not once:
                        notify('retry-exhausted', limit)
                    break
                notify('retry', {'completed': number, 'next': number + 1, 'limit': limit})
                control.wait(1)
            if status != 'changed':
                break
            if click_finished_at is not None:
                round_click = click_finished_at()
            if not once and index == 0:
                control.wait(1)
        if control.stopped():
            break
        if once:
            return 'once'
        if round_click is not None:
            cadence_start = round_click
            next_check = cadence_start + interval
        else:
            # Keep the cadence through empty rounds and skip missed checks.
            periods = math.floor((control.clock() - cadence_start) / interval) + 1
            next_check = cadence_start + periods * interval
        # Optional work for the wait; it runs right after a round, so a burst of
        # input never overlaps with screenshots or clicks of the next round.
        if gap is not None and not control.stopped():
            gap(max(0, next_check-control.clock()))
        notify('sleeping', max(0, next_check-control.clock()))
    return 'time-limit' if control.expired else 'stopped'
