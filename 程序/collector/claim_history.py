"""Small, bounded per-run counters, saved on each confirmed collection."""

from datetime import datetime
import json
import os
from pathlib import Path
import uuid


DEFAULT_PATH = Path(__file__).with_name('领取记录.json')
MAX_RUNS = 20
MAX_BYTES = 65536
LABELS = {'running': '未记录结束', 'completed': '正常完成',
          'stopped': '已停止', 'error': '异常结束'}
FIELDS = ('id', 'started_at', 'updated_at', 'finished_at', 'count', 'status', 'reason')


def read_runs(path=None):
    path = Path(path) if path is not None else DEFAULT_PATH
    try:
        if path.stat().st_size > MAX_BYTES:
            return []
        data = json.loads(path.read_text(encoding='utf-8'))
        rows = data.get('runs', []) if isinstance(data, dict) else []
        if not isinstance(rows, list):
            return []
        result = []
        for row in rows[-MAX_RUNS:]:
            if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                    or not isinstance(row.get('count'), int) or row['count'] < 0
                    or row.get('status') not in LABELS):
                continue
            clean = {key: row.get(key, '') for key in FIELDS}
            if all(isinstance(clean[key], str) and len(clean[key]) <= 80
                   for key in FIELDS if key != 'count'):
                result.append(clean)
        return result
    except (OSError, ValueError):
        return []


def summary(row):
    started = row['started_at'][:19].replace('T', ' ')
    return f"{started} 开始的监测：共记录领取 {row['count']} 个宝箱（{LABELS[row['status']]}）。"


class RunRecord:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else DEFAULT_PATH
        now = self._now()
        self.data = dict(id=uuid.uuid4().hex, started_at=now, updated_at=now,
                         finished_at='', count=0, status='running', reason='')
        self._save()

    @staticmethod
    def _now():
        return datetime.now().astimezone().isoformat(timespec='seconds')

    def update(self, count):
        self.data['count'] = int(count)
        self.data['updated_at'] = self._now()
        self._save()

    def finish(self, reason):
        self.data.update(status='completed' if reason in ('time-limit', 'once') else
                         'stopped' if reason == 'stopped' else 'error', reason=reason,
                         finished_at=self._now(), updated_at=self._now())
        self._save()

    def _save(self):
        rows = [row for row in read_runs(self.path) if row['id'] != self.data['id']]
        rows.append(self.data)
        text = json.dumps({'version': 1, 'runs': rows[-MAX_RUNS:]}, ensure_ascii=False, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        try:
            with temporary.open('w', encoding='utf-8') as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
