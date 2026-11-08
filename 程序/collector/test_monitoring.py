import argparse
import unittest

from monitoring import MonitorControl, parse_hours, prompt_hours, run_monitor


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class FakeEvent:
    def __init__(self, clock, stop_at=None):
        self.clock = clock
        self.flag = False
        self.stop_at = stop_at

    def is_set(self):
        return self.flag

    def set(self):
        self.flag = True

    def wait(self, seconds):
        end = self.clock.value + seconds
        if self.stop_at is not None and self.clock.value <= self.stop_at <= end:
            self.clock.value = self.stop_at
            self.flag = True
        elif not self.flag:
            self.clock.value = end
        return self.flag


class MonitoringTests(unittest.TestCase):
    def create_control(self, seconds, stop_at=None):
        clock = FakeClock()
        event = FakeEvent(clock, stop_at=stop_at)
        return MonitorControl(event, seconds, clock=clock), clock

    def test_default_interval_is_fifteen_minutes(self):
        control, clock = self.create_control(1900)
        calls = []
        def attempt():
            calls.append(clock())
            return 'waiting'
        self.assertEqual(run_monitor(attempt, control), 'time-limit')
        self.assertEqual(calls, [0, 900, 1800])
        self.assertEqual(clock(), 1900)

    def test_chest_that_appears_three_seconds_late_is_claimed_in_same_round(self):
        control, clock = self.create_control(910)
        calls, claims = [], []

        def attempt():
            calls.append(clock())
            if clock() in (0, 903):
                claims.append(clock())
                return 'changed'
            return 'waiting'

        self.assertEqual(run_monitor(attempt, control, ready_rechecks=5), 'time-limit')
        self.assertEqual(claims, [0, 903])
        self.assertEqual([t for t in calls if 900 <= t <= 903], [900, 901, 902, 903])

    def test_late_chest_rechecks_are_bounded_and_keep_the_next_round(self):
        control, clock = self.create_control(902)
        calls = []

        def attempt():
            calls.append(clock())
            return 'waiting'

        self.assertEqual(run_monitor(attempt, control, ready_rechecks=5), 'time-limit')
        self.assertEqual(calls, [0, 1, 2, 3, 4, 5, 900, 901])

    def test_stop_interrupts_late_chest_recheck(self):
        control, clock = self.create_control(20, stop_at=1.5)
        calls = []

        def attempt():
            calls.append(clock())
            return 'waiting'

        self.assertEqual(run_monitor(attempt, control, ready_rechecks=5), 'stopped')
        self.assertEqual(calls, [0, 1])

    def test_once_does_not_wait_for_late_chests(self):
        control, clock = self.create_control(None)
        self.assertEqual(run_monitor(lambda: 'waiting', control, once=True,
                                     ready_rechecks=5), 'once')
        self.assertEqual(clock(), 0)

    def test_next_round_uses_last_successful_mouse_release(self):
        control, clock = self.create_control(40)
        checks, rounds = [], []
        clicked_at = None

        def attempt():
            nonlocal clicked_at
            checks.append(clock())
            if len(checks) <= 2:
                clock.value += 2
                clicked_at = clock()
                clock.value += 1  # Post-click verification.
                return 'changed'
            return 'waiting'

        def report(status, detail):
            if status == 'cycle':
                rounds.append(clock())

        self.assertEqual(run_monitor(attempt, control, interval=10, report=report,
                                     click_finished_at=lambda: clicked_at), 'time-limit')
        self.assertEqual(checks[:2], [0, 4])
        self.assertEqual(rounds, [0, 16, 26, 36])

    def test_unconfirmed_click_does_not_reset_check_interval(self):
        control, clock = self.create_control(12)
        rounds = []

        def attempt():
            clock.value += 2
            return 'unverified'

        def report(status, detail):
            if status == 'cycle':
                rounds.append(clock())

        self.assertEqual(run_monitor(attempt, control, interval=10, report=report,
                                     max_attempts=1, click_finished_at=clock), 'time-limit')
        self.assertEqual(rounds, [0, 10])

    def test_two_successful_claims_do_not_end_monitoring(self):
        control, clock = self.create_control(1810)
        calls = []
        def attempt():
            calls.append(clock())
            return 'changed'
        self.assertEqual(run_monitor(attempt, control), 'time-limit')
        self.assertEqual(calls, [0, 1, 900, 901, 1800, 1801])

    def test_repeated_unconfirmed_clicks_wait_for_next_round(self):
        control, clock = self.create_control(1900)
        calls = []
        def attempt():
            calls.append(clock())
            return 'unverified'
        self.assertEqual(run_monitor(attempt, control), 'time-limit')
        self.assertEqual(calls, [start + retry for start in (0, 900, 1800)
                                 for retry in range(5)])

    def test_failed_chest_is_attempted_at_most_five_times(self):
        control, clock = self.create_control(20)
        calls = []
        def attempt():
            calls.append(clock())
            return 'unverified'
        self.assertEqual(run_monitor(attempt, control), 'time-limit')
        self.assertEqual(calls, [0, 1, 2, 3, 4])

    def test_success_on_fifth_attempt_continues_to_second_chest(self):
        control, clock = self.create_control(20)
        statuses = iter(['unverified'] * 4 + ['changed', 'changed'])
        calls = []
        def attempt():
            calls.append(clock())
            return next(statuses)
        self.assertEqual(run_monitor(attempt, control), 'time-limit')
        self.assertEqual(calls, [0, 1, 2, 3, 4, 5])

    def test_retry_limit_resets_for_second_chest(self):
        control, clock = self.create_control(20)
        statuses = iter((['unverified'] * 4 + ['changed']) * 2)
        calls = []
        def attempt():
            calls.append(clock())
            return next(statuses)
        self.assertEqual(run_monitor(attempt, control), 'time-limit')
        self.assertEqual(calls, list(range(10)))

    def test_f7_interrupts_retry_wait(self):
        control, clock = self.create_control(1000, stop_at=1.5)
        calls = []
        def attempt():
            calls.append(clock())
            return 'unverified'
        self.assertEqual(run_monitor(attempt, control), 'stopped')
        self.assertEqual(calls, [0, 1])
        self.assertEqual(clock(), 1.5)

    def test_runtime_can_expire_before_five_attempts(self):
        control, clock = self.create_control(2.5)
        calls = []
        def attempt():
            calls.append(clock())
            return 'unverified'
        self.assertEqual(run_monitor(attempt, control), 'time-limit')
        self.assertEqual(calls, [0, 1, 2])
        self.assertEqual(clock(), 2.5)

    def test_occluded_pet_is_not_retried_immediately(self):
        control, clock = self.create_control(20)
        calls = []
        def attempt():
            calls.append(clock())
            return 'unknown'
        self.assertEqual(run_monitor(attempt, control), 'time-limit')
        self.assertEqual(calls, [0])

    def test_f7_interrupts_long_wait(self):
        control, clock = self.create_control(21600, stop_at=30)
        calls = []
        def attempt():
            calls.append(clock())
            return 'waiting'
        self.assertEqual(run_monitor(attempt, control), 'stopped')
        self.assertEqual(calls, [0])
        self.assertEqual(clock(), 30)

    def test_short_runtime_expires_before_next_poll(self):
        control, clock = self.create_control(180)
        calls = []
        def attempt():
            calls.append(clock())
            return 'waiting'
        self.assertEqual(run_monitor(attempt, control), 'time-limit')
        self.assertEqual(calls, [0])
        self.assertEqual(clock(), 180)

    def test_six_hours_runs_twenty_four_scheduled_rounds(self):
        control, clock = self.create_control(6 * 3600)
        calls = []
        def attempt():
            calls.append(clock())
            return 'waiting'
        self.assertEqual(run_monitor(attempt, control), 'time-limit')
        self.assertEqual(calls, list(range(0, 21600, 900)))
        self.assertEqual(clock(), 21600)

    def test_once_mode_attempts_only_one_chest(self):
        control, clock = self.create_control(None)
        calls = []
        def attempt():
            calls.append(clock())
            return 'changed'
        self.assertEqual(run_monitor(attempt, control, once=True), 'once')
        self.assertEqual(calls, [0])

    def test_wait_is_capped_by_remaining_runtime(self):
        control, clock = self.create_control(6)
        control.wait(900)
        self.assertEqual(clock(), 6)
        self.assertTrue(control.stopped())

    def test_transient_error_does_not_end_monitoring(self):
        control, clock = self.create_control(1000)
        calls = []
        def attempt():
            calls.append(clock())
            if len(calls) == 1:
                raise OSError('temporary capture failure')
            return 'waiting'
        self.assertEqual(run_monitor(attempt, control), 'time-limit')
        self.assertEqual(calls, [0, 900])


class GapTests(unittest.TestCase):
    def create_control(self, seconds, stop_at=None):
        clock = FakeClock()
        event = FakeEvent(clock, stop_at=stop_at)
        return MonitorControl(event, seconds, clock=clock), clock

    def test_gap_work_runs_after_every_round(self):
        control, clock = self.create_control(1900)
        attempts, gaps = [], []

        def attempt():
            attempts.append(clock())
            return 'waiting'

        def gap(remaining):
            gaps.append((clock(), remaining))
            control.wait(100)

        self.assertEqual(run_monitor(attempt, control, gap=gap), 'time-limit')
        self.assertEqual(attempts, [0, 900, 1800])
        self.assertEqual(gaps, [(0, 900), (900, 900), (1800, 900)])

    def test_gap_work_runs_once_per_round_not_per_chest(self):
        control, clock = self.create_control(1810)
        attempts, gaps = [], []

        def attempt():
            attempts.append(clock())
            return 'changed'

        def gap(remaining):
            gaps.append((clock(), remaining))
            control.wait(10)

        self.assertEqual(run_monitor(attempt, control, gap=gap), 'time-limit')
        self.assertEqual(attempts, [0, 1, 900, 901, 1800, 1801])
        self.assertEqual(gaps, [(1, 899), (901, 899), (1801, 899)])

    def test_gap_work_is_skipped_for_single_rounds(self):
        control, clock = self.create_control(None)
        gaps = []
        self.assertEqual(run_monitor(lambda: 'changed', control, once=True,
                                     gap=gaps.append), 'once')
        self.assertEqual(gaps, [])

    def test_gap_overrun_never_reports_negative_wait(self):
        control, clock = self.create_control(2000)
        details = []

        def gap(remaining):
            control.wait(950)

        def report(status, detail=None):
            details.append((status, detail))

        self.assertEqual(run_monitor(lambda: 'waiting', control, report=report,
                                     gap=gap), 'time-limit')
        waits = [detail for status, detail in details if status == 'sleeping']
        self.assertTrue(waits)
        self.assertEqual(waits[0], 0)
        self.assertTrue(all(value >= 0 for value in waits))

    def test_gap_burst_never_overlaps_the_next_round(self):
        # 间隙连点跑在主循环上：这一轮没点完，下一轮连截图都不会开始。
        control, clock = self.create_control(4 * 900)
        active = []
        overlaps = []

        def attempt():
            if active:
                overlaps.append(clock())
            return 'waiting'

        def gap(remaining):
            active.append(clock())
            control.wait(1200)  # 比检查间隔更久，逼出“连点没完就到下一轮”的场景
            active.pop()

        self.assertEqual(run_monitor(attempt, control, gap=gap), 'time-limit')
        self.assertEqual(overlaps, [])


class RuntimeInputTests(unittest.TestCase):
    def test_hours_can_be_fractional(self):
        self.assertEqual(parse_hours('6'), 6)
        self.assertEqual(parse_hours('0.5'), 0.5)

    def test_invalid_and_unbounded_hours_are_rejected(self):
        for value in ('0', '-1', 'abc', 'nan', 'inf', '1e308'):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    parse_hours(value)

    def test_prompt_retries_until_valid_hours_entered(self):
        values = iter(('', '0', 'abc', '6'))
        self.assertEqual(prompt_hours(read=lambda text: next(values), write=lambda text: None), 6)

    def test_prompt_allows_cancel(self):
        self.assertIsNone(prompt_hours(read=lambda text: 'q', write=lambda text: None))


if __name__ == '__main__':
    unittest.main()
