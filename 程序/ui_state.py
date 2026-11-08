"""Read-only view of collector timing. Never schedules input."""
import json
import math
import time

PREFIX = '@@PET_UI@@'
EVENTS = ('started', 'checking', 'claimed', 'gap', 'sleeping', 'finished')


def duration(seconds):
    seconds = max(0, int(math.ceil(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f'{hours} 小时 {minutes} 分钟' if hours else f'{minutes} 分 {seconds:02d} 秒' if minutes else f'{seconds} 秒'


class MonitorState:
    """Parse the collector's opt-in telemetry into count and countdown text."""

    def __init__(self): self.begin(None)

    def begin(self, run_id):
        self.run_id, self.seq, self.count = run_id, 0, 0
        self.event = 'pending' if run_id else 'idle'
        self.deadline = self.next_check = None
        self.reason = ''
        self.history_id = ''

    def consume(self, line):
        if not line.startswith(PREFIX) or len(line.encode('utf-8')) > 8192: return False
        try:
            data = json.loads(line[len(PREFIX):])
            if type(data['v']) is not int or data['v'] != 1 or data['run_id'] != self.run_id or data['event'] not in EVENTS: return False
            seq, count, sent = data['seq'], data['claimed'], data['emitted_monotonic']
            if type(seq) is not int or seq <= self.seq or type(count) is not int or count < 0: return False
            values = [sent, data.get('remaining_seconds'), data.get('next_check_seconds')]
            for value in values:
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0): return False
            if sent is None: return False
            self.seq, self.count, self.event = seq, count, data['event']
            self.deadline = sent+values[1] if values[1] is not None else None
            self.next_check = sent+values[2] if values[2] is not None else None
            self.reason = str(data.get('reason', ''))[:100]
            if isinstance(data.get('history_id'), str): self.history_id = data['history_id'][:80]
            return True
        except (ValueError, TypeError, KeyError): return False

    def unexpected_exit(self):
        if self.event != 'finished': self.event, self.reason = 'finished', 'incomplete'

    def recover(self, rows):
        if self.reason != 'incomplete' or not self.history_id: return
        row = next((row for row in rows if row.get('id') == self.history_id), None)
        if row is not None and type(row.get('count')) is int and row['count'] >= 0:
            self.count = max(self.count, row['count'])

    def snapshot(self, now=None):
        now = time.monotonic() if now is None else now
        remaining = max(0, self.deadline-now) if self.deadline is not None else None
        next_seconds = max(0, self.next_check-now) if self.next_check is not None else None
        text = {'idle': '尚未开始', 'pending': '正在启动…', 'started': '正在启动…',
                'checking': '正在检查', 'gap': '间隙连点中', 'claimed': '正在复查本轮宝箱'}.get(self.event, '')
        if self.event == 'sleeping':
            text = '本次剩余时间内不再检查' if self.deadline is not None and self.next_check is not None and self.next_check >= self.deadline else '下次检查 '+duration(next_seconds or 0)+'后'
        if self.event == 'finished':
            text = {'time-limit': '本次监测已完成', 'stopped': '本次监测已停止',
                    'error': '本次监测异常结束', 'incomplete': '已结束，状态未完整上报'}.get(self.reason, '本次监测已结束')
            remaining = next_seconds = None
        return dict(count=self.count, next_seconds=next_seconds, remaining_seconds=remaining, next_text=text,
                    remaining_text='剩余 '+duration(remaining) if remaining is not None else '')
