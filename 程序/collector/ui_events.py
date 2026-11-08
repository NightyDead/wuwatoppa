"""Opt-in, single-line UI telemetry; contains no scheduling or desktop input."""
import json
import sys

PREFIX = '@@PET_UI@@'


class UiEvents:
    def __init__(self, enabled, run_id, output=None):
        self.enabled, self.run_id = enabled, run_id
        self.output = output if output is not None else sys.stdout
        self.seq = 0

    def send(self, event, claimed, now, deadline, next_seconds=None, reason='', history_id=''):
        if not self.enabled: return
        self.seq += 1
        payload = dict(v=1, run_id=self.run_id, seq=self.seq, event=event, claimed=claimed,
                       emitted_monotonic=now, remaining_seconds=max(0, deadline-now) if deadline is not None else None,
                       next_check_seconds=next_seconds, reason=reason, history_id=history_id)
        try: print(PREFIX+json.dumps(payload, ensure_ascii=False, allow_nan=False), file=self.output, flush=True)
        except (OSError, ValueError): pass
