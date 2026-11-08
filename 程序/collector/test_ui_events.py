import json
import unittest
from io import StringIO
from ui_events import UiEvents, PREFIX


class EventTests(unittest.TestCase):
    def test_disabled_does_not_write(self):
        output = StringIO()
        UiEvents(False, '', output).send('started', 0, 100, 200)
        self.assertEqual(output.getvalue(), '')

    def test_event_includes_absolute_count_sequence_and_clock(self):
        output = StringIO()
        events = UiEvents(True, 'run', output)
        events.send('claimed', 2, 100, 200)
        events.send('sleeping', 2, 110, 200, 30)
        rows = [json.loads(line[len(PREFIX):]) for line in output.getvalue().splitlines()]
        self.assertEqual(rows[1]['seq'], 2)
        self.assertEqual(rows[1]['remaining_seconds'], 90)
        self.assertEqual(rows[1]['claimed'], 2)
