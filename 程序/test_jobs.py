import ast
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch
import jobs


class JobsTests(unittest.TestCase):
    def test_frozen_build_reenters_exe_for_background_jobs(self):
        with tempfile.TemporaryDirectory() as folder, patch('jobs.threading.Thread.start'), \
                patch('jobs.subprocess.Popen') as spawn, \
                patch('jobs.sys.frozen', True, create=True), \
                patch('jobs.sys.executable', r'C:\app\桌宠助手.exe'):
            spawn.return_value.poll.return_value = None
            manager = jobs.Workers(runtime=Path(folder))
            manager.start('preview', [])
            command = spawn.call_args.args[0]
            self.assertEqual(command[:3], [r'C:\app\桌宠助手.exe', '--job', 'preview'])
            self.assertIn('--stop-file', command)
            self.assertNotIn('launcher.py', command)

    def test_collector_ui_token_is_unique_and_stdout_finishes_before_completion(self):
        with tempfile.TemporaryDirectory() as folder, patch('jobs.threading.Thread.start'), patch('jobs.subprocess.Popen') as spawn:
            spawn.return_value.poll.return_value = None
            manager = jobs.Workers(runtime=Path(folder))
            manager.start('collector', ['--hours', '1'])
            first = manager.active['collector'].ui_run_id
            self.assertIn('--ui-events', spawn.call_args.args[0])
            self.assertIn(first, spawn.call_args.args[0])
            spawn.return_value.poll.return_value = 0
            self.assertEqual(manager.finished(), [])
            manager.active['collector'].reader_done.set()
            self.assertEqual(manager.finished(), [('collector', 0)])
            spawn.return_value.poll.return_value = None
            manager.start('collector', ['--hours', '1'])
            self.assertNotEqual(manager.active['collector'].ui_run_id, first)

    def test_windows_command_files_have_crlf_line_endings(self):
        for path in jobs.ROOT.parent.rglob('*.cmd'):
            with self.subTest(path=path):
                data = path.read_bytes()
                self.assertNotIn(b'\n', data.replace(b'\r\n', b''))
                self.assertFalse(data.startswith(b'\xef\xbb\xbf'))

    @unittest.skipUnless(os.name == 'nt', 'Windows launcher')
    def test_real_start_command_check_mode(self):
        import subprocess
        result = subprocess.run(['cmd.exe', '/d', '/c', str(jobs.ROOT.parent / '启动.cmd'), '--check'],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
                                env=dict(os.environ, PYTHONUTF8='1'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b'launcher check passed', result.stdout)
        self.assertEqual(result.stderr.strip(), b'')

    def test_clicker_uses_existing_limits(self):
        self.assertEqual(jobs.clicker_options('', ''),
                         ['--count', '2000', '--interval', '0.1', '--hours', '0.0'])
        self.assertEqual(jobs.clicker_options('0', '.1', '2'),
                         ['--count', '0', '--interval', '0.1', '--hours', '2.0'])
        for count, interval, hours in [('0', '.1', '0'), ('100001', '.1', '0'),
                                       ('1', 'nan', '0'), ('1', '.00001', '0'),
                                       ('1', '.1', '-1'), ('1', '.1', 'nan')]:
            with self.subTest(count=count, interval=interval, hours=hours), \
                    self.assertRaises(ValueError):
                jobs.clicker_options(count, interval, hours)

    def test_collector_minutes_are_converted_to_seconds(self):
        self.assertEqual(jobs.collector_options('6', '15'),
                         ['--hours', '6.0', '--interval', '900.0'])
        for hours, interval in [('0', '15'), ('nan', '15'), ('6', 'inf'), ('6', '0')]:
            with self.subTest(hours=hours, interval=interval), self.assertRaises(ValueError):
                jobs.collector_options(hours, interval)

    def test_collector_gap_clicks_are_optional(self):
        self.assertEqual(
            jobs.collector_options('6', '15', '2000', '.1'),
            ['--hours', '6.0', '--interval', '900.0',
             '--idle-count', '2000', '--idle-interval', '0.1'])
        self.assertEqual(jobs.collector_options('6', '15', '0', '.1'),
                         ['--hours', '6.0', '--interval', '900.0'])
        for idle, interval in [('-1', '.1'), ('2000', '0'), ('2000', 'nan')]:
            with self.subTest(idle=idle, interval=interval), self.assertRaises(ValueError):
                jobs.collector_options('6', '15', idle, interval)

    def test_gap_clicks_come_from_launched_options_not_the_current_form(self):
        # 监测带着间隙连点跑起来后，把输入框改成 0 并不代表它停了；
        # 连点器的互斥判断必须看启动时的参数。
        with tempfile.TemporaryDirectory() as folder, patch('jobs.threading.Thread.start'), \
                patch('jobs.subprocess.Popen') as spawn:
            spawn.return_value.poll.return_value = None
            manager = jobs.Workers(runtime=Path(folder))
            self.assertEqual(manager.gap_clicks('collector'), 0)
            manager.start('collector', jobs.collector_options('6', '15', '2500', '.1'))
            self.assertEqual(manager.gap_clicks('collector'), 2500)
            manager.start('clicker', jobs.clicker_options('10', '.1'))
            self.assertEqual(manager.gap_clicks('clicker'), 0)

    def test_workers_run_independently_and_can_stop_together(self):
        with tempfile.TemporaryDirectory() as folder, patch('jobs.threading.Thread.start'):
            processes = [Mock(pid=11), Mock(pid=12)]
            for process in processes:
                process.poll.return_value = None
            with patch('jobs.subprocess.Popen', side_effect=processes) as spawn:
                manager = jobs.Workers(runtime=Path(folder))
                manager.start('clicker', jobs.clicker_options('10', '.1'))
                manager.start('collector', jobs.collector_options('6', '15'))
            self.assertTrue(manager.running('clicker'))
            self.assertTrue(manager.running('collector'))
            self.assertEqual(spawn.call_count, 2)
            manager.stop('clicker')
            self.assertTrue(manager.active['clicker'].stop_file.exists())
            self.assertFalse(manager.active['collector'].stop_file.exists())
            manager.stop_all()
            self.assertTrue(manager.active['collector'].stop_file.exists())
            for process in processes:
                process.terminate.assert_not_called()
                process.kill.assert_not_called()

    def test_duplicate_start_and_conflicting_claim_test_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder, patch('jobs.threading.Thread.start'), \
                patch('jobs.subprocess.Popen') as spawn:
            spawn.return_value.poll.return_value = None
            manager = jobs.Workers(runtime=Path(folder))
            manager.start('collector', ['--hours', '1'])
            for kind in ('collector', 'test'):
                with self.subTest(kind=kind), self.assertRaises(ValueError):
                    manager.start(kind, [])
            self.assertEqual(spawn.call_count, 1)

    def test_finished_job_cleans_up_only_its_stop_signal(self):
        with tempfile.TemporaryDirectory() as folder, patch('jobs.threading.Thread.start'), \
                patch('jobs.subprocess.Popen') as spawn:
            process = spawn.return_value
            process.poll.return_value = None
            manager = jobs.Workers(runtime=Path(folder))
            manager.start('clicker', [])
            manager.stop('clicker')
            signal = manager.active['clicker'].stop_file
            unrelated = Path(folder) / 'keep.txt'
            unrelated.write_text('keep')
            process.poll.return_value = 0
            manager.active['clicker'].reader_done.set()
            done = manager.finished()
            self.assertEqual([kind for kind, code in done], ['clicker'])
            self.assertFalse(signal.exists())
            self.assertTrue(unrelated.exists())

    def test_commands_use_actual_scripts_and_have_no_shell(self):
        with tempfile.TemporaryDirectory() as folder, patch('jobs.threading.Thread.start'), \
                patch('jobs.subprocess.Popen') as spawn:
            spawn.return_value.poll.return_value = None
            manager = jobs.Workers(runtime=Path(folder))
            manager.start('preview', [])
            args, kwargs = spawn.call_args
            self.assertTrue(Path(args[0][2]).is_file())
            self.assertIn('--preview', args[0])
            self.assertFalse(kwargs.get('shell', False))
            self.assertEqual(kwargs['stdin'], jobs.subprocess.DEVNULL)


class PlanTests(unittest.TestCase):
    def test_plan_options_validate_and_mark_plan_mode(self):
        self.assertEqual(
            jobs.plan_options({'kind': 'clicker', 'count': '10',
                               'interval': '.1', 'duration': '0'}),
            ['--count', '10', '--interval', '0.1', '--hours', '0.0', '--plan'])
        self.assertEqual(
            jobs.plan_options({'kind': 'collector', 'hours': '1', 'minutes': '15'}),
            ['--hours', '1.0', '--interval', '900.0', '--plan'])
        self.assertEqual(
            jobs.plan_options({'kind': 'collector', 'hours': '1', 'minutes': '15',
                               'idle': '500'}),
            ['--hours', '1.0', '--interval', '900.0', '--idle-count', '500',
             '--idle-interval', '0.1', '--plan'])
        for step in ({'kind': 'other'},
                     {'kind': 'clicker', 'count': '0', 'interval': '.1', 'duration': '0'},
                     {'kind': 'collector', 'hours': '0', 'minutes': '15'}):
            with self.subTest(step=step), self.assertRaises(ValueError):
                jobs.plan_options(step)

    def test_plan_summary_shows_each_step_settings(self):
        self.assertEqual(
            jobs.step_summary({'kind': 'clicker', 'count': '5000',
                               'interval': '.2', 'duration': '1'}),
            '连点 5000 次 · 间隔 0.2 秒 · 时长 1.0 小时')
        self.assertEqual(
            jobs.step_summary({'kind': 'collector', 'hours': '2', 'minutes': '15'}),
            '监测 2.0 小时 · 每 15 分钟检查')
        self.assertEqual(
            jobs.step_summary({'kind': 'collector', 'hours': '2', 'minutes': '15',
                               'idle': '2000', 'idle_interval': '.1'}),
            '监测 2.0 小时 · 每 15 分钟检查 · 间隙连点 2000 次')

    def test_plan_runner_advances_only_after_clean_exit(self):
        workers = Mock()
        runner = jobs.PlanRunner(workers)
        runner.start([('collector', ['--hours', '1', '--plan']),
                      ('clicker', ['--count', '10', '--plan'])])
        self.assertEqual(workers.start.call_args_list,
                         [call('collector', ['--hours', '1', '--plan'])])
        self.assertEqual(runner.current(), ('collector', ['--hours', '1', '--plan']))
        self.assertIsNone(runner.advance('clicker', 0))
        self.assertEqual(runner.advance('collector', 0), 'started')
        self.assertEqual(workers.start.call_count, 2)
        self.assertEqual(runner.advance('clicker', 0), 'done')
        self.assertFalse(runner.running)

    def test_plan_runner_stops_after_error_or_stop_code(self):
        for code in (1, 3):
            with self.subTest(code=code):
                workers = Mock()
                runner = jobs.PlanRunner(workers)
                runner.start([('clicker', []), ('collector', [])])
                self.assertEqual(runner.advance('clicker', code), 'stopped')
                self.assertFalse(runner.running)
                self.assertEqual(workers.start.call_count, 1)
                self.assertIsNone(runner.advance('clicker', 0))

    def test_plan_runner_rejects_empty_or_duplicate_start(self):
        runner = jobs.PlanRunner(Mock())
        with self.assertRaises(ValueError):
            runner.start([])
        runner.start([('clicker', [])])
        with self.assertRaises(ValueError):
            runner.start([('clicker', [])])

    def test_plan_runner_resets_when_next_worker_cannot_start(self):
        workers = Mock()
        runner = jobs.PlanRunner(workers)
        runner.start([('clicker', []), ('collector', [])])
        workers.start.side_effect = OSError('boom')
        with self.assertRaises(OSError):
            runner.advance('clicker', 0)
        self.assertFalse(runner.running)


class LauncherTests(unittest.TestCase):
    """The appearance dict is an instance attribute, so it can hide a method."""

    def setUp(self):
        self.source = (jobs.ROOT / 'launcher.py').read_text(encoding='utf-8')
        self.window = next(node for node in ast.parse(self.source).body
                           if isinstance(node, ast.ClassDef) and node.name == 'MainWindow')

    def test_appearance_attribute_does_not_shadow_a_method(self):
        # self.options = 外观字典 会盖掉同名方法，按钮随后就只抛 TypeError。
        methods = {node.name for node in ast.walk(self.window)
                   if isinstance(node, ast.FunctionDef)}
        self.assertNotIn('options', methods)
        self.assertIn('job_options', methods)

    def test_single_features_get_their_arguments_from_the_helper(self):
        self.assertNotIn('self.options(', self.source)
        self.assertEqual(self.source.count('self.job_options('), 2)


if __name__ == '__main__':
    unittest.main()
