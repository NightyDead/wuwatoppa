import unittest
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch
import macro
from macro import run_macro


class MacroTests(unittest.TestCase):
    def test_sends_exactly_1000_presses(self):
        sent = []
        done = run_macro(1000, 0.1, lambda: sent.append(1),
                         lambda: False, lambda seconds: None)
        self.assertEqual(done, 1000)
        self.assertEqual(len(sent), 1000)

    def test_stop_prevents_further_presses(self):
        sent = []
        done = run_macro(1000, 0.1, lambda: sent.append(1),
                         lambda: len(sent) >= 3, lambda seconds: None)
        self.assertEqual(done, 3)
        self.assertEqual(len(sent), 3)

    def test_already_stopped_sends_nothing(self):
        sent = []
        done = run_macro(1000, 0.1, lambda: sent.append(1),
                         lambda: True, lambda seconds: None)
        self.assertEqual((done, sent), (0, []))

    def test_count_zero_keeps_sending_until_stopped(self):
        sent = []
        done = run_macro(0, 0.1, lambda: sent.append(1),
                         lambda: len(sent) >= 5, lambda seconds: None)
        self.assertEqual((done, len(sent)), (5, 5))

    def test_hours_limit_stops_after_requested_duration(self):
        sent = []
        now = [0.0]

        def clock():
            return now[0]

        def sleep(seconds):
            now[0] += seconds

        done = run_macro(0, 0.1, lambda: sent.append(1), lambda: False, sleep,
                         hours=0.001, clock=clock)
        self.assertEqual(done, len(sent))
        self.assertGreater(len(sent), 0)
        self.assertGreaterEqual(now[0], 3.6)
        self.assertLess(now[0], 3.8)

    def test_count_and_hours_stop_at_whichever_comes_first(self):
        sent = []
        done = run_macro(3, 0.1, lambda: sent.append(1), lambda: False,
                         lambda seconds: None, hours=1)
        self.assertEqual((done, len(sent)), (3, 3))

    def test_reason_reports_count_duration_and_stop(self):
        reason = {}
        run_macro(2, 0.1, lambda: None, lambda: False, lambda seconds: None,
                  reason=reason)
        self.assertEqual(reason['value'], 'count')

        reason = {}
        now = [0.0]

        def clock():
            return now[0]

        def sleep(seconds):
            now[0] += seconds

        run_macro(0, 0.1, lambda: None, lambda: False, sleep,
                  hours=0.001, clock=clock, reason=reason)
        self.assertEqual(reason['value'], 'hours')

        reason = {}
        run_macro(10, 0.1, lambda: None, lambda: True, lambda seconds: None,
                  reason=reason)
        self.assertEqual(reason['value'], 'stopped')

    def test_plan_mode_reports_stop_with_exit_code_three(self):
        keyboard = FakeKeyboard(stop_after=2)
        with patch.object(macro, 'WindowsKeyboard', return_value=keyboard), \
                patch.object(macro.time, 'sleep'), \
                patch('sys.argv', ['macro.py', '--count', '100', '--interval', '0.1',
                                   '--hours', '0', '--plan']), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(macro.main(), 3)
        self.assertEqual(keyboard.sent, 2)

    def test_plan_mode_completed_count_still_returns_zero(self):
        keyboard = FakeKeyboard()
        with patch.object(macro, 'WindowsKeyboard', return_value=keyboard), \
                patch.object(macro.time, 'sleep'), \
                patch('sys.argv', ['macro.py', '--count', '3', '--interval', '0.1',
                                   '--hours', '0', '--plan']), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(macro.main(), 0)
        self.assertEqual(keyboard.sent, 3)

    def test_main_starts_without_waiting_for_f6(self):
        keyboard = FakeKeyboard()
        with patch.object(macro, 'WindowsKeyboard', return_value=keyboard), \
                patch.object(macro.time, 'sleep'), \
                patch('sys.argv', ['macro.py', '--count', '3', '--interval', '0.1',
                                   '--hours', '0']), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(macro.main(), 0)
        self.assertEqual(keyboard.sent, 3)

    def test_main_ctrl_alt_still_stops_automatic_run(self):
        keyboard = FakeKeyboard(stop_after=2)
        with patch.object(macro, 'WindowsKeyboard', return_value=keyboard), \
                patch.object(macro.time, 'sleep'), \
                patch('sys.argv', ['macro.py', '--count', '100', '--interval', '0.1',
                                   '--hours', '0']), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(macro.main(), 0)
        self.assertEqual(keyboard.sent, 2)

    def test_check_mode_does_not_send_keys(self):
        keyboard = FakeKeyboard()
        with patch.object(macro, 'WindowsKeyboard', return_value=keyboard), \
                patch('sys.argv', ['macro.py', '--check']), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(macro.main(), 0)
        self.assertEqual(keyboard.sent, 0)

    def test_interface_stop_signal_prevents_key_presses(self):
        keyboard = FakeKeyboard()
        with tempfile.TemporaryDirectory() as folder:
            signal = Path(folder) / 'clicker.stop'
            signal.touch()
            with patch.object(macro, 'WindowsKeyboard', return_value=keyboard), \
                    patch('sys.argv', ['macro.py', '--count', '3', '--interval', '.1',
                                       '--hours', '0', '--stop-file', str(signal)]), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(macro.main(), 0)
        self.assertEqual(keyboard.sent, 0)

    def test_main_prompts_for_count_and_interval_before_sending(self):
        keyboard = FakeKeyboard()
        answers = iter(['3', '0.2', '0'])
        def answer(prompt):
            self.assertEqual(keyboard.sent, 0)
            return next(answers)
        with patch.object(macro, 'WindowsKeyboard', return_value=keyboard), \
                patch.object(macro.time, 'sleep') as sleep, \
                patch('builtins.input', side_effect=answer) as question, \
                patch('sys.argv', ['macro.py']), redirect_stdout(io.StringIO()):
            self.assertEqual(macro.main(), 0)
        self.assertEqual(question.call_count, 3)
        self.assertEqual(keyboard.sent, 3)
        self.assertAlmostEqual(sum(call.args[0] for call in sleep.call_args_list), 0.4)

    def test_cancel_in_either_prompt_sends_nothing(self):
        for answers in (['q'], ['3', 'q'], ['3', '0.2', 'q']):
            with self.subTest(answers=answers), \
                    patch.object(macro, 'WindowsKeyboard') as keyboard, \
                    patch('builtins.input', side_effect=answers), \
                    patch('sys.argv', ['macro.py']), redirect_stdout(io.StringIO()):
                self.assertEqual(macro.main(), 0)
                keyboard.assert_not_called()

    def test_missing_interval_is_prompted_when_count_is_supplied(self):
        keyboard = FakeKeyboard()
        with patch.object(macro, 'WindowsKeyboard', return_value=keyboard), \
                patch.object(macro.time, 'sleep'), \
                patch('builtins.input', side_effect=['0.1', '0']) as question, \
                patch('sys.argv', ['macro.py', '--count', '2']), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(macro.main(), 0)
        self.assertEqual(question.call_count, 2)
        self.assertEqual(keyboard.sent, 2)

    def test_invalid_command_line_values_exit_before_input_is_sent(self):
        for args in (['--count', '100001'],
                     ['--count', '0', '--interval', '0.1', '--hours', '0'],
                     ['--interval', 'nan'], ['--interval', 'inf'],
                     ['--interval', '0.00009'], ['--interval', '60.1'],
                     ['--hours', '-1'], ['--hours', '1000.1'], ['--hours', 'nan']):
            with self.subTest(args=args), \
                    patch.object(macro, 'WindowsKeyboard') as keyboard, \
                    patch('sys.argv', ['macro.py'] + args), \
                    patch('sys.stderr', new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as error:
                    macro.main()
                self.assertEqual(error.exception.code, 2)
                keyboard.assert_not_called()


class ParameterTests(unittest.TestCase):
    def test_count_limits(self):
        self.assertEqual(macro.parse_count('0'), 0)
        self.assertEqual(macro.parse_count('1'), 1)
        self.assertEqual(macro.parse_count('100000'), 100000)
        for value in ('-1', '100001', '1.5', 'nan', 'abc'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                macro.parse_count(value)

    def test_interval_limits_and_nonfinite_values(self):
        self.assertEqual(macro.parse_interval('0.02'), 0.02)
        self.assertEqual(macro.parse_interval('60'), 60)
        for value in ('0', '-1', '0.00009', '60.001', 'nan', 'inf', '-inf', 'abc'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                macro.parse_interval(value)

    def test_hours_limits_and_nonfinite_values(self):
        self.assertEqual(macro.parse_hours('0'), 0)
        self.assertEqual(macro.parse_hours('2'), 2)
        for value in ('-1', '1000.1', 'nan', 'inf', 'abc'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                macro.parse_hours(value)

    def test_invalid_input_reprompts_then_accepts_values(self):
        with patch('builtins.input', side_effect=['100001', '4', 'nan', '0.3', '0']), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(macro.prompt_parameters(), (4, 0.3, 0))

    def test_blank_input_uses_defaults(self):
        with patch('builtins.input', side_effect=['', '', '']), redirect_stdout(io.StringIO()):
            self.assertEqual(macro.prompt_parameters(), (2000, 0.1, 0))

    def test_closed_input_cancels(self):
        with patch('builtins.input', side_effect=EOFError), redirect_stdout(io.StringIO()):
            self.assertIsNone(macro.prompt_parameters())


class StopHotkeyTests(unittest.TestCase):
    def test_stop_hotkey_requires_ctrl_and_alt(self):
        keyboard = macro.WindowsKeyboard.__new__(macro.WindowsKeyboard)
        keyboard.user32 = Mock()
        keyboard.user32.GetAsyncKeyState.return_value = 0x8000
        self.assertTrue(keyboard.stop_pressed())
        polled = [call.args[0] for call in keyboard.user32.GetAsyncKeyState.call_args_list]
        self.assertEqual(polled, [0x11, 0x12])
        keyboard.user32.GetAsyncKeyState.side_effect = [0x8000, 0]
        self.assertFalse(keyboard.stop_pressed())


class FakeKeyboard:
    def __init__(self, stop_after=None):
        self.sent = 0
        self.stop_after = stop_after

    def pressed(self, vk):
        if vk not in (0x11, 0x12):
            raise AssertionError('Only the Ctrl+Alt stop keys should be polled.')
        return self.stop_after is not None and self.sent >= self.stop_after

    def stop_pressed(self):
        return self.pressed(0x11) and self.pressed(0x12)

    def send_f15(self):
        self.sent += 1


if __name__ == '__main__':
    unittest.main()
