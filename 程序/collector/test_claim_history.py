import json
import io
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import claim_history
import auto_collect


class HistoryTests(unittest.TestCase):
    def test_normal_completion_is_readable_after_process_has_ended(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'history.json'
            run = claim_history.RunRecord(path)
            run.update(4)
            run.finish('time-limit')
            latest = claim_history.read_runs(path)[-1]
            self.assertEqual(latest['count'], 4)
            self.assertEqual(latest['status'], 'completed')
            self.assertTrue(latest['finished_at'])
            self.assertIn('4 个宝箱', claim_history.summary(latest))

    def test_unfinished_run_keeps_last_saved_count(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'history.json'
            run = claim_history.RunRecord(path)
            run.update(2)
            del run  # Simulate losing the process without a final summary.
            latest = claim_history.read_runs(path)[-1]
            self.assertEqual(latest['count'], 2)
            self.assertEqual(latest['status'], 'running')
            self.assertIn('未记录结束', claim_history.summary(latest))

    def test_only_recent_twenty_runs_are_kept(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'history.json'
            for count in range(25):
                run = claim_history.RunRecord(path)
                run.update(count)
                run.finish('stopped')
            rows = claim_history.read_runs(path)
            self.assertEqual(len(rows), 20)
            self.assertEqual([row['count'] for row in rows], list(range(5, 25)))
            self.assertLess(path.stat().st_size, 12000)
            self.assertEqual([p.name for p in Path(folder).iterdir()], ['history.json'])

    def test_many_claims_update_one_record_without_growing_history(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'history.json'
            run = claim_history.RunRecord(path)
            for count in range(1, 12):
                run.update(count)
            rows = claim_history.read_runs(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['count'], 11)
            self.assertLess(path.stat().st_size, 1000)

    def test_failed_replacement_keeps_previous_valid_record(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'history.json'
            run = claim_history.RunRecord(path)
            run.update(1)
            with patch.object(claim_history.os, 'replace', side_effect=OSError('blocked')):
                with self.assertRaises(OSError):
                    run.update(2)
            self.assertEqual(claim_history.read_runs(path)[-1]['count'], 1)
            self.assertEqual([p.name for p in Path(folder).iterdir()], ['history.json'])

    def test_corrupt_or_oversize_file_does_not_break_startup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'history.json'
            for text in ('broken', '{}', '[]', 'x' * 70000):
                path.write_text(text, encoding='utf-8')
                self.assertEqual(claim_history.read_runs(path), [])

    def test_main_persists_only_confirmed_changes_and_finishes_record(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'history.json'
                def monitor(attempt, control, **options):
                    for status in ('cycle', 'unverified', 'unknown', 'changed', 'changed'):
                        options['report'](status)
                    if fail:
                        raise RuntimeError('simulated error')
                    return 'time-limit'
                with patch.object(auto_collect, 'WindowsDesktop') as desktop_class, \
                        patch.object(auto_collect, 'ChestDetector'), \
                        patch.object(auto_collect, 'RunRecord', side_effect=lambda: claim_history.RunRecord(path)), \
                        patch.object(auto_collect, 'run_monitor', side_effect=monitor), \
                        patch('sys.argv', ['auto_collect.py', '--hours', '1']), \
                        redirect_stdout(io.StringIO()):
                    desktop = desktop_class.return_value
                    desktop.stop_pressed.return_value = False
                    if fail:
                        with self.assertRaises(RuntimeError):
                            auto_collect.main()
                    else:
                        self.assertEqual(auto_collect.main(), 0)
                    desktop.capture.assert_not_called()
                    desktop.click.assert_not_called()
                record = claim_history.read_runs(path)[-1]
                self.assertEqual(record['count'], 2)
                self.assertEqual(record['status'], 'error' if fail else 'completed')


if __name__ == '__main__':
    unittest.main()
