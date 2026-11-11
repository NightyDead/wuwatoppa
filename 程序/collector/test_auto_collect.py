from pathlib import Path
import ctypes
import io
import tempfile
from contextlib import redirect_stdout
import unittest
from unittest.mock import patch
import numpy as np
import auto_collect
from PIL import Image, ImageEnhance, ImageDraw
from auto_collect import ChestDetector, Chest, attempt_claim, WindowsDesktop, INPUT

ASSETS = Path(__file__).parent / 'assets'


class DetectionTests(unittest.TestCase):
    def setUp(self):
        self.detector = ChestDetector()
        self.ready = Image.open(ASSETS / 'ready_reference.png').convert('RGB')

    def test_fft_correlation_matches_direct_summation(self):
        # The numpy FFT replaced scipy.signal.fftconvolve; keep the two
        # definitions of the NCC numerator pinned to each other.
        rng = np.random.default_rng(3)
        image = rng.random((11, 13))
        kernel = rng.random((3, 4)) - 0.5
        scores = auto_collect._correlate(image, kernel)
        self.assertEqual(scores.shape, (9, 10))
        for y in range(scores.shape[0]):
            for x in range(scores.shape[1]):
                expect = float((image[y:y+3, x:x+4] * kernel).sum())
                self.assertAlmostEqual(scores[y, x], expect, places=10)

    def test_reused_frame_matches_direct_normalized_scores_at_all_edges(self):
        rng = np.random.default_rng(17)
        image = rng.random((17, 19))
        frame = auto_collect._NccFrame(image)
        for height, width in ((3, 4), (10, 9), image.shape):
            template = rng.random((height, width))
            centered = template - template.mean()
            scores = auto_collect._ncc(image, template, frame)
            for y in range(scores.shape[0]):
                for x in range(scores.shape[1]):
                    region = image[y:y+height, x:x+width]
                    normalized = region - region.mean()
                    expected = (normalized * centered).sum() / (
                        np.linalg.norm(normalized) * np.linalg.norm(centered))
                    self.assertAlmostEqual(scores[y, x], expected, places=10)

    def test_template_spectrum_cache_preserves_scores_across_frames_and_eviction(self):
        rng = np.random.default_rng(37)
        pixels = [rng.random((5, 7)), rng.random((5, 7))]
        templates = [auto_collect._NccTemplate(template) for template in pixels]
        cache = auto_collect._TemplateSpectra(byte_limit=3000)
        cases = ((0, (12, 12)), (0, (12, 12)), (1, (12, 12)),
                 (0, (14, 14)), (0, (12, 12)), (0, (20, 24)))
        images = [rng.random(shape) for _, shape in cases]
        expected = [auto_collect._ncc(image, pixels[index])
                    for (index, _), image in zip(cases, images)]
        with patch.object(auto_collect, '_conjugate_fft',
                          wraps=auto_collect._conjugate_fft) as transform:
            for (index, shape), image, scores in zip(cases, images, expected):
                with self.subTest(index=index, shape=shape):
                    frame = auto_collect._NccFrame(image, cache)
                    np.testing.assert_array_equal(auto_collect._ncc(image, templates[index], frame), scores)
                    self.assertLessEqual(cache.byte_count, cache.byte_limit)
            # The second image reuses the first template. The changed shape
            # evicts earlier spectra, and the oversized spectrum stays uncached.
            self.assertEqual(transform.call_count, 5)
        self.assertEqual(cache.byte_count, sum(spectrum.nbytes for spectrum in cache.entries.values()))
        self.assertNotIn((templates[0], (20, 24)), cache.entries)

    def test_ready_reference_has_two_chests(self):
        found = sorted(self.detector.find(self.ready), key=lambda c: c.y)
        self.assertEqual(len(found), 2)
        self.assertLess(abs(found[0].x - 139), 4)
        self.assertLess(abs(found[0].y - 61), 4)
        self.assertLess(abs(found[1].x - 132), 4)
        self.assertLess(abs(found[1].y - 92), 4)

    def test_white_theme_screenshots_have_one_ready_chest(self):
        fixtures = Path(__file__).parent / 'test_fixtures'
        for name, center in (('white_theme_1.png', (51, 24)),
                             ('white_theme_2.png', (91, 37))):
            with self.subTest(name=name), Image.open(fixtures / name) as image:
                found = self.detector.find(image)
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0].slot, 1)
                self.assertLess(abs(found[0].x - center[0]), 5)
                self.assertLess(abs(found[0].y - center[1]), 5)

    def test_viewer_downscaled_chest_is_detected(self):
        with Image.open(Path(__file__).parent / 'test_fixtures' /
                        'viewer_scaled.png') as image:
            found = self.detector.find(image)
        self.assertEqual(len(found), 1)
        self.assertLess(abs(found[0].x - 116.5), 3)
        self.assertLess(abs(found[0].y - 72.5), 3)

    def test_white_chest_without_menu_is_ignored(self):
        image = Image.new('RGB', (200, 160), '#272936')
        with Image.open(ASSETS / 'chest_white.png') as chest:
            image.paste(chest, (90, 60))
        self.assertEqual(self.detector.find(image), [])

    def test_white_menu_without_ready_chest_is_ignored(self):
        with Image.open(Path(__file__).parent / 'test_fixtures' /
                        'white_theme_2.png') as source:
            image = source.convert('RGB')
        image.paste('#302014', (35, 0, 150, 74))
        self.assertTrue(self.detector.anchor_present(image))
        self.assertEqual(self.detector.find(image), [])

    def test_cooldown_digits_are_never_chests(self):
        cooldown = Image.open(ASSETS / 'cooldown_reference.png').convert('RGB')
        self.assertEqual(self.detector.find(cooldown), [])

    def test_follows_pet_when_it_moves(self):
        desktop = Image.new('RGB', (700, 450), '#35405c')
        desktop.paste(self.ready, (410, 240))
        found = sorted(self.detector.find(desktop), key=lambda c: c.y)
        self.assertEqual(len(found), 2)
        self.assertLess(abs(found[0].x - 549), 4)
        self.assertLess(abs(found[0].y - 301), 4)

    def test_scaled_pet_is_detected(self):
        desktop = Image.new('RGB', (700, 450), '#35405c')
        scaled = self.ready.resize((204, 163), Image.Resampling.BICUBIC)
        desktop.paste(scaled, (100, 120))
        found = self.detector.find(desktop)
        self.assertEqual(len(found), 2)

    def test_small_pet_with_weak_anchor_is_still_detected(self):
        # 桌宠缩到 60% 时状态栏的“≡”只剩十来像素，锚点分数会跌到 0.88 以下；
        # 宝箱气泡本身仍然清晰，不能因为锚点弱就报告“没有宝箱”。
        with Image.open(Path(__file__).parent / 'test_fixtures' /
                        'software_100.png') as source:
            pet = source.convert('RGB').resize((72, 108), Image.Resampling.LANCZOS)
        desktop = Image.new('RGB', (700, 450), '#35405c')
        desktop.paste(pet, (300, 240))
        found = self.detector.find(desktop)
        self.assertEqual(len(found), 2)
        self.assertTrue(all(chest.score > 0.90 for chest in found))

    def test_bubbles_larger_than_the_status_bar_icon_are_found(self):
        # 实机截图：状态栏的“≡”按 0.65 才最像，两个宝箱气泡却是 0.8 大小；
        # 只按锚点那一档缩放找宝箱，明明看得见也会一个都认不出来。
        with Image.open(Path(__file__).parent / 'test_fixtures' /
                        'pet_uneven_bubbles.png') as image:
            self.detector.clear_cache()
            found = sorted(self.detector.find(image), key=lambda c: c.y)
            self.detector.clear_cache()
        self.assertEqual([c.slot for c in found], [0, 1])
        self.assertLess(abs(found[0].x - 80), 4)
        self.assertLess(abs(found[0].y - 52), 4)
        self.assertLess(abs(found[1].x - 75), 4)
        self.assertLess(abs(found[1].y - 76), 4)

    def test_chest_icons_without_pet_anchor_are_ignored(self):
        desktop = Image.new('RGB', (400, 300), '#35405c')
        chest = Image.open(ASSETS / 'chest.png').convert('RGB')
        desktop.paste(chest, (150, 100))
        desktop.paste(chest, (180, 140))
        self.assertEqual(self.detector.find(desktop), [])

    def test_blank_frame_is_ignored(self):
        self.assertEqual(self.detector.find(Image.new('RGB', (400, 300), 'white')), [])

    def test_one_cooling_slot_does_not_prevent_other_chest(self):
        mixed = self.ready.copy()
        cooldown = Image.open(ASSETS / 'cooldown_reference.png').convert('RGB')
        box = (118, 43, 163, 78)
        mixed.paste(cooldown.crop(box), (118, 43))
        found = self.detector.find(mixed)
        self.assertEqual(len(found), 1)
        self.assertLess(abs(found[0].y - 92), 4)

    def test_occluded_status_bar_is_ignored(self):
        hidden = self.ready.copy()
        hidden.paste('white', (0, 105, hidden.width, hidden.height))
        self.assertEqual(self.detector.find(hidden), [])

    def test_repeated_search_uses_small_region_and_cached_scale(self):
        image = Image.new('RGB', (700, 450), '#35405c')
        image.paste(self.ready, (320, 200))
        first = self.detector.find(image)
        with patch.object(auto_collect, '_ncc', wraps=auto_collect._ncc) as match:
            second = self.detector.find(image)
        self.assertEqual({c.slot for c in second}, {0, 1})
        self.assertEqual([(c.x, c.y) for c in first], [(c.x, c.y) for c in second])
        self.assertTrue(match.called)
        self.assertTrue(all(call.args[0].size < 700 * 450 / 4
                            for call in match.call_args_list))
        self.assertLess(match.call_count, len(self.detector.variants))

    def test_cached_search_falls_back_when_pet_moves_far(self):
        for position in ((30, 30), (480, 250)):
            image = Image.new('RGB', (700, 450), '#35405c')
            image.paste(self.ready, position)
            found = self.detector.find(image)
            self.assertEqual(len(found), 2)
            self.assertTrue(all(c.x > position[0] + 120 for c in found))

    def test_cached_search_falls_back_when_size_changes(self):
        for scale in (1.0, 1.5):
            image = Image.new('RGB', (700, 450), '#35405c')
            pet = self.ready.resize((round(163*scale), round(130*scale)),
                                    Image.Resampling.BICUBIC)
            image.paste(pet, (150, 100))
            found = self.detector.find(image)
            self.assertEqual(len(found), 2)
            for chest in found:
                x, y = (139, 61) if chest.slot == 0 else (132, 92)
                self.assertLess(abs(chest.x-(150+x*scale)), 4)
                self.assertLess(abs(chest.y-(100+y*scale)), 4)

    def test_cooldown_with_cached_menu_does_not_search_full_screen(self):
        cooldown = Image.open(ASSETS / 'cooldown_reference.png').convert('RGB')
        # Model a cooldown transition without changing the menu's appearance.
        # The separate cooldown reference has different opacity, which correctly
        # triggers a full search when its menu falls below the match threshold.
        cooling_pet = self.ready.copy()
        box = (118, 43, 163, 104)
        cooling_pet.paste(cooldown.crop(box), box[:2])
        ready_canvas = Image.new('RGB', (500, 350), '#35405c')
        ready_canvas.paste(self.ready, (150, 100))
        self.detector.find(ready_canvas)
        cool_canvas = Image.new('RGB', ready_canvas.size, '#35405c')
        cool_canvas.paste(cooling_pet, (150, 100))
        with patch.object(auto_collect, '_ncc', wraps=auto_collect._ncc) as match:
            self.assertEqual(self.detector.find(cool_canvas), [])
        self.assertTrue(all(call.args[0].size < 500 * 350 / 4
                            for call in match.call_args_list))

    def test_local_recheck_does_not_accept_another_pet_far_away(self):
        before = Image.new('RGB', (700, 450), '#35405c')
        before.paste(self.ready, (30, 30))
        target = self.detector.find(before)[0]
        after = Image.new('RGB', before.size, '#35405c')
        after.paste(self.ready, (480, 250))
        chests, visible = self.detector.inspect_near(after, target, max_anchor_shift=3)
        self.assertEqual(chests, [])
        self.assertFalse(visible)

    def test_local_search_coordinates_are_correct_at_screen_edges(self):
        for name in ('white_theme_1.png', 'viewer_scaled.png'):
            with self.subTest(name=name), Image.open(
                    Path(__file__).parent / 'test_fixtures' / name) as image:
                first = self.detector.find(image)
                second = self.detector.find(image)
                self.assertEqual(len(second), 1)
                self.assertAlmostEqual(first[0].x, second[0].x)
                self.assertAlmostEqual(first[0].y, second[0].y)

    def test_software_size_reference_screenshots_have_two_chests(self):
        references = ((200, ((120, 70), (94, 190))),
                      (150, ((96, 55), (77, 145))),
                      (100, ((68, 40), (56, 100))),
                      (50, ((48, 26), (42, 56))))
        for percent, expected in references:
            with self.subTest(percent=percent), Image.open(
                    Path(__file__).parent / 'test_fixtures' / f'software_{percent}.png') as image:
                found = sorted(self.detector.find(image), key=lambda c: c.slot)
                self.assertEqual(len(found), 2)
                self.assertEqual([c.slot for c in found], [0, 1])
                for chest, (x, y) in zip(found, expected):
                    self.assertLess(abs(chest.x-x), 5)
                    self.assertLess(abs(chest.y-y), 5)

    def test_intermediate_software_sizes_and_cached_size_changes(self):
        with Image.open(Path(__file__).parent / 'test_fixtures' /
                        'software_100.png') as source:
            for percent in (55, 57, 65, 75, 83, 85, 95, 97, 110, 113, 125,
                            137, 140, 160, 163, 175, 187, 190, 198):
                with self.subTest(percent=percent):
                    scale = percent / 100
                    pet = source.resize((round(source.width*scale), round(source.height*scale)),
                                        Image.Resampling.BICUBIC)
                    image = Image.new('RGB', (700, 600), '#35405c')
                    image.paste(pet, (230, 160))
                    found = sorted(self.detector.find(image), key=lambda c: c.slot)
                    self.assertEqual(len(found), 2)
                    for chest, (x, y) in zip(found, ((68, 40), (56, 100))):
                        self.assertLess(abs(chest.x-(230+x*scale)), 5)
                        self.assertLess(abs(chest.y-(160+y*scale)), 5)

    def test_calibrated_chests_stay_local_after_initial_match(self):
        with Image.open(Path(__file__).parent / 'test_fixtures' /
                        'software_200.png') as source:
            image = Image.new('RGB', (700, 600), '#35405c')
            image.paste(source, (230, 160))
        first = self.detector.find(image)
        self.assertEqual(len(first), 2)
        with patch.object(auto_collect, '_ncc', wraps=auto_collect._ncc) as match:
            second, visible = self.detector.inspect_near(image, first[0])
        self.assertTrue(visible)
        self.assertEqual(len(second), 2)
        self.assertTrue(all(call.args[0].size < 700*600/2 for call in match.call_args_list))

    def test_anchor_recheck_only_scores_allowed_positions(self):
        image = Image.new('RGB', (700, 600), '#35405c')
        with Image.open(Path(__file__).parent / 'test_fixtures' / 'software_200.png') as pet:
            image.paste(pet, (230, 160))
        first = self.detector.find(image)
        target = first[0]
        anchor = self.detector.variants[target.variant][1]

        with patch.object(auto_collect, '_ncc', wraps=auto_collect._ncc) as match:
            found, visible = self.detector.inspect_near(image, target, max_anchor_shift=3)

        self.assertTrue(visible)
        self.assertEqual([(c.x, c.y, c.slot) for c in found],
                         [(c.x, c.y, c.slot) for c in first])
        self.assertEqual(match.call_args_list[0].args[0].shape,
                         (anchor.shape[0] + 6, anchor.shape[1] + 6))

    def test_calibrated_menus_with_timer_text_are_not_ready_chests(self):
        for percent, centers in ((200, ((120, 70), (94, 190))),
                                 (150, ((96, 55), (77, 145))),
                                 (100, ((68, 40), (56, 100))),
                                 (50, ((48, 26), (42, 56)))):
            with self.subTest(percent=percent), Image.open(
                    Path(__file__).parent / 'test_fixtures' / f'software_{percent}.png') as source:
                image = source.convert('RGB')
                scale = percent / 100
                draw = ImageDraw.Draw(image)
                for x, y in centers:
                    draw.rectangle((int(x-22*scale), int(y-17*scale),
                                    int(x+22*scale), int(y+17*scale)), fill='#ffd28b')
                    draw.text((int(x-10*scale), int(y-4*scale)), '07:59', fill='#8c5b3f')
                self.detector.clear_cache()
                self.assertEqual(self.detector.find(image), [])


class ClickGuardTests(unittest.TestCase):
    def test_stable_ready_chest_is_clicked_once(self):
        desktop = FakeDesktop()
        detector = FakeDetector([[Chest(139, 61, 1, 1, 0)],
                                 [Chest(140, 61, 1, 1, 0)], []])
        result = attempt_claim(detector, desktop, lambda: False, lambda seconds: None)
        self.assertEqual(result, 'changed')
        self.assertEqual(desktop.clicks, [(40, 81)])
        self.assertEqual(detector.full_searches, 1)
        self.assertEqual(detector.local_searches, 2)

    def test_pet_moving_between_frames_prevents_click(self):
        desktop = FakeDesktop()
        detector = FakeDetector([[Chest(139, 61, 1, 1, 0)],
                                 [Chest(160, 61, 1, 1, 0)]])
        self.assertEqual(attempt_claim(detector, desktop, lambda: False,
                                      lambda seconds: None), 'moving')
        self.assertEqual(desktop.clicks, [])

    def test_another_app_at_point_prevents_click(self):
        desktop = FakeDesktop(owned=False)
        detector = FakeDetector([[Chest(139, 61, 1, 1, 0)]])
        self.assertEqual(attempt_claim(detector, desktop, lambda: False,
                                      lambda seconds: None), 'other-window')
        self.assertEqual(desktop.clicks, [])

    def test_f7_during_detection_prevents_click(self):
        desktop = FakeDesktop()
        stop = [False]
        detector = FakeDetector([[Chest(139, 61, 1, 1, 0)]])
        def wait(seconds):
            stop[0] = True
        self.assertEqual(attempt_claim(detector, desktop, lambda: stop[0], wait), 'stopped')
        self.assertEqual(desktop.clicks, [])

    def test_suppressed_click_is_not_reported_as_sent(self):
        desktop = FakeDesktop(click_allowed=False)
        detector = FakeDetector([[Chest(139, 61, 1, 1, 0)],
                                 [Chest(139, 61, 1, 1, 0)]])
        self.assertEqual(attempt_claim(detector, desktop, lambda: False,
                                      lambda seconds: None), 'busy')
        self.assertEqual(desktop.clicks, [])

    def test_unchanged_chest_is_not_reported_as_success(self):
        desktop = FakeDesktop()
        chest = Chest(139, 61, 1, 1, 0)
        detector = FakeDetector([[chest], [chest], [chest]])
        self.assertEqual(attempt_claim(detector, desktop, lambda: False,
                                      lambda seconds: None), 'unverified')
        self.assertEqual(len(desktop.clicks), 1)

    def test_hidden_pet_after_click_is_not_reported_as_success(self):
        desktop = FakeDesktop()
        chest = Chest(139, 61, 1, 1, 0)
        detector = FakeDetector([[chest], [chest], []], anchor_visible=False)
        self.assertEqual(attempt_claim(detector, desktop, lambda: False,
                                      lambda seconds: None), 'unknown')

    def test_retry_keeps_the_previously_clicked_slot(self):
        desktop = FakeDesktop()
        first = Chest(139, 61, 1, 1, 0)
        second = Chest(132, 92, 0.95, 1, 1)
        detector = FakeDetector([[first, second], [first, second], [first]])
        self.assertEqual(attempt_claim(detector, desktop, lambda: False,
                                      lambda seconds: None, preferred_slot=1), 'changed')
        self.assertEqual(desktop.clicks, [(32, 112)])

    def test_retry_does_not_click_other_slot_if_target_disappears(self):
        desktop = FakeDesktop()
        detector = FakeDetector([[Chest(139, 61, 1, 1, 0)]])
        self.assertEqual(attempt_claim(detector, desktop, lambda: False,
                                      lambda seconds: None, preferred_slot=1), 'waiting')
        self.assertEqual(desktop.clicks, [])


class FakeDetector:
    def __init__(self, frames, anchor_visible=True):
        self.last_anchor = None
        self.frames = iter(frames)
        self.anchor_visible = anchor_visible
        self.full_searches = 0
        self.local_searches = 0

    def find(self, image):
        self.full_searches += 1
        return next(self.frames)

    def inspect_near(self, image, target, max_anchor_shift=None):
        self.local_searches += 1
        return next(self.frames), self.anchor_visible

    def clear_cache(self):
        pass

    def capture_region(self, target, bounds):
        return bounds

    def anchor_present(self, image):
        return self.anchor_visible


class FakeDesktop:
    def __init__(self, owned=True, click_allowed=True):
        self.owned = owned
        self.click_allowed = click_allowed
        self.clicks = []
        self.monitor_box = None

    def bounds(self):
        return (-100, 20, 400, 320)

    def pet_monitor_bounds(self):
        return self.monitor_box

    def pet_capture_bounds(self):
        return self.monitor_box, getattr(self, 'window_region', None)

    def capture(self, box=None):
        return None, (-100, 20)

    def owned_by_pet(self, x, y):
        return self.owned

    def click(self, x, y):
        if not self.click_allowed:
            return False
        self.clicks.append((x, y))
        return True


class PetMonitorTests(unittest.TestCase):
    def make_desktop(self, windows):
        desktop = WindowsDesktop.__new__(WindowsDesktop)
        desktop.pet_windows = lambda: [(hwnd, 10) for hwnd in windows]
        desktop.user32 = unittest.mock.Mock()
        desktop.user32.IsIconic.return_value = False
        desktop.user32.MonitorFromWindow.side_effect = lambda hwnd, _: windows[hwnd][0]

        def get_title(hwnd, buffer, size):
            buffer.value = windows[hwnd][1]
            return len(buffer.value)

        def get_monitor_info(monitor, pointer):
            pointer._obj.rcMonitor.left = -3840 if monitor == 1 else 0
            pointer._obj.rcMonitor.right = 0 if monitor == 1 else 2560
            pointer._obj.rcMonitor.bottom = 2160 if monitor == 1 else 1600
            return True

        desktop.user32.GetWindowTextW.side_effect = get_title
        desktop.user32.GetMonitorInfoW.side_effect = get_monitor_info
        return desktop

    def locate(self, windows):
        return self.make_desktop(windows).pet_monitor_bounds()

    def test_window_region_preserves_negative_coordinates_and_ignores_helpers(self):
        desktop = self.make_desktop({101: (1, 'Wuwa Tappo'), 102: (2, 'Helper')})
        def get_rect(hwnd, attribute, pointer, size):
            self.assertEqual((hwnd, attribute), (101, 9))
            rect = pointer._obj
            rect.left, rect.top, rect.right, rect.bottom = -2010, 790, -1850, 1010
            return 0
        desktop._window_attribute = unittest.mock.Mock(side_effect=get_rect)
        self.assertEqual(desktop.pet_capture_bounds(),
                         ((-3840, 0, 0, 2160), (-2138, 470, -1642, 1170)))

    def test_ambiguous_or_cross_screen_window_uses_monitor(self):
        monitor = (-3840, 0, 0, 2160)
        desktop = self.make_desktop({101: (1, 'Wuwa Tappo'), 102: (1, 'Wuwa Tappo')})
        desktop._window_attribute = unittest.mock.Mock()
        self.assertEqual(desktop.pet_capture_bounds(), (monitor, None))
        desktop._window_attribute.assert_not_called()

        desktop = self.make_desktop({101: (1, 'Wuwa Tappo')})
        def crosses_display(hwnd, attribute, pointer, size):
            rect = pointer._obj
            rect.left, rect.top, rect.right, rect.bottom = -100, 200, 25, 380
            return 0
        desktop._window_attribute = crosses_display
        self.assertEqual(desktop.pet_capture_bounds(), (monitor, None))

    def test_main_window_follows_pet_across_displays(self):
        self.assertEqual(self.locate({101: (2, 'Wuwa Tappo'), 102: (2, '')}),
                         (0, 0, 2560, 1600))
        self.assertEqual(self.locate({101: (1, 'Wuwa Tappo'), 102: (2, 'Helper')}),
                         (-3840, 0, 0, 2160))

    def test_unknown_title_only_falls_back_to_a_unique_display(self):
        self.assertEqual(self.locate({101: (1, 'Unknown')}), (-3840, 0, 0, 2160))
        self.assertIsNone(self.locate({101: (1, 'Unknown'), 102: (2, 'Helper')}))


class RegionObservationTests(unittest.TestCase):
    def make_state_tracker(self):
        with Image.open(ASSETS / 'ready_reference.png') as source:
            ready = source.convert('RGB')
        with Image.open(ASSETS / 'cooldown_reference.png') as source:
            cooldown = source.convert('RGB')
        desktop = ScreenshotDesktop(Image.new('RGB', (700, 450)), (-800, -200))

        def show(slots, dx=0):
            pet = ready.copy()
            for slot, box in enumerate(((118, 43, 163, 78), (112, 79, 163, 104))):
                if slot not in slots:
                    pet.paste(cooldown.crop(box), box)
            desktop.image = Image.new('RGB', (700, 450), '#35405c')
            desktop.image.paste(pet, (150+dx, 100))

        show({0, 1})
        return auto_collect.PetObserver(ChestDetector(), desktop), desktop, show

    def test_cooling_pet_follows_movement_and_recovers_each_slot_immediately(self):
        observer, desktop, show = self.make_state_tracker()
        self.assertEqual({c.slot for c in observer.locate(lambda: False).chests}, {0, 1})
        for dx in range(0, 31, 3):
            show(set(), dx)
            frame = observer.locate(lambda: False)
            self.assertEqual(frame.chests, [])
            self.assertTrue(frame.anchor_visible)
            self.assertIsNotNone(observer.anchor)
        for slots in ({1}, {0}, {0, 1}):
            show(slots, 30)
            frame = observer.locate(lambda: False)
            self.assertEqual({c.slot for c in frame.chests}, slots)
            for chest in frame.chests:
                x, y = ((139, 61), (132, 92))[chest.slot]
                self.assertAlmostEqual(chest.x, -800+180+x, delta=4)
                self.assertAlmostEqual(chest.y, -200+100+y, delta=4)
        self.assertEqual(desktop.requests.count(None), 1)

    def test_unowned_strong_anchor_is_not_retained_during_cooldown(self):
        observer, desktop, show = self.make_state_tracker()
        observer.locate(lambda: False)
        show(set())
        observer.locate(lambda: False)
        self.assertIsNotNone(observer.anchor)
        desktop.owned_by_pet = lambda x, y: False
        observer.locate(lambda: False)
        self.assertIsNotNone(observer.detector.last_anchor)
        self.assertIsNone(observer.anchor)

    def test_ambiguous_rescan_discards_preliminary_anchor(self):
        observer, desktop, show = self.make_state_tracker()
        observer.locate(lambda: False)
        show(set())

        def inspect(image, target, max_anchor_shift=None):
            observer.detector.last_anchor = auto_collect.Anchor(
                target.anchor_x, target.anchor_y, target.scale, target.variant, 0.99)
            return [], True

        def rescan(image, target):
            self.assertIsNotNone(observer.anchor)
            observer.detector.last_anchor = None  # Broader search cannot identify one menu.
            return []

        with patch.object(observer.detector, 'inspect_near', side_effect=inspect), \
                patch.object(observer.detector, 'rescan_region', side_effect=rescan):
            observer.locate(lambda: False)
        self.assertIsNone(observer.anchor)

    def test_cooldown_anchor_is_cleared_after_occlusion_and_display_change(self):
        observer, desktop, show = self.make_state_tracker()
        observer.locate(lambda: False)
        show(set())
        observer.locate(lambda: False)
        self.assertIsNotNone(observer.anchor)
        desktop.image.paste('#35405c', (0, 0, 700, 450))
        self.assertEqual(observer.locate(lambda: False).chests, [])
        self.assertIsNone(observer.anchor)
        self.assertIsNone(observer.target)
        show({0, 1})
        self.assertEqual({c.slot for c in observer.locate(lambda: False).chests}, {0, 1})
        show(set())
        observer.locate(lambda: False)
        self.assertIsNotNone(observer.anchor)
        desktop.origin = (-1600, -200)
        show({0, 1}, 120)
        frame = observer.locate(lambda: False)
        self.assertIsNone(desktop.requests[-1])
        self.assertEqual({c.slot for c in frame.chests}, {0, 1})
        for chest in frame.chests:
            x, y = ((139, 61), (132, 92))[chest.slot]
            self.assertAlmostEqual(chest.x, -1600+270+x, delta=4)
            self.assertAlmostEqual(chest.y, -200+100+y, delta=4)

    def make_tracker(self, origin=(0, 0)):
        image = Image.new('RGB', (700, 600), '#35405c')
        with Image.open(Path(__file__).parent / 'test_fixtures' / 'software_100.png') as pet:
            image.paste(pet, (230, 160))
        desktop = ScreenshotDesktop(image, origin)
        observer = auto_collect.PetObserver(ChestDetector(), desktop)
        return observer, desktop

    def test_interface_stop_signal_exits_without_capture(self):
        with tempfile.TemporaryDirectory() as folder:
            signal = Path(folder) / 'collector.stop'
            signal.touch()
            with patch.object(auto_collect, 'WindowsDesktop') as desktop_class, \
                    patch.object(auto_collect, 'ChestDetector'), \
                    patch('sys.argv', ['auto_collect.py', '--hours', '6', '--stop-file', str(signal)]), \
                    redirect_stdout(io.StringIO()):
                desktop = desktop_class.return_value
                desktop.stop_pressed.return_value = False
                self.assertEqual(auto_collect.main(), 0)
                desktop.capture.assert_not_called()
                desktop.click.assert_not_called()

    def test_preview_captures_only_pet_display(self):
        box = (-1920, 0, 0, 1080)
        with patch.object(auto_collect, 'WindowsDesktop') as desktop_class, \
                patch.object(auto_collect, 'ChestDetector') as detector_class, \
                patch.object(auto_collect, 'report_elevation'), \
                patch.object(auto_collect, 'display_scale', return_value=1), \
                patch.object(Image.Image, 'save'), \
                patch('sys.argv', ['auto_collect.py', '--preview']), \
                redirect_stdout(io.StringIO()):
            desktop = desktop_class.return_value
            desktop.pet_monitor_bounds.return_value = box
            desktop.capture.return_value = Image.new('RGB', (100, 100)), box[:2]
            detector_class.return_value.find.return_value = [Chest(30, 40, 0.9, 1, 0)]

            self.assertEqual(auto_collect.main(), 0)

            desktop.capture.assert_called_once_with(box)
            desktop.owned_by_pet.assert_called_once_with(-1890, 40)
            desktop.click.assert_not_called()

    def test_preview_without_unique_pet_display_does_not_capture(self):
        with patch.object(auto_collect, 'WindowsDesktop') as desktop_class, \
                patch.object(auto_collect, 'ChestDetector'), \
                patch.object(auto_collect, 'report_elevation'), \
                patch('sys.argv', ['auto_collect.py', '--preview']):
            desktop = desktop_class.return_value
            desktop.pet_monitor_bounds.return_value = None

            with self.assertRaisesRegex(RuntimeError, '无法确定桌宠所在的屏幕'):
                auto_collect.main()

            desktop.capture.assert_not_called()

    def test_first_capture_uses_pet_monitor_then_local_region(self):
        observer, desktop = self.make_tracker(origin=(-800, -200))
        desktop.monitor_box = (-650, -100, -290, 210)
        first = observer.locate(lambda: False)
        second = observer.locate(lambda: False)

        self.assertEqual(desktop.requests[0], desktop.monitor_box)
        self.assertEqual(len(desktop.requests), 2)
        self.assertIsNotNone(desktop.requests[1])
        self.assertEqual(first.origin, (-650, -100))
        self.assertEqual(len(first.chests), 2)
        upper = next(c for c in first.chests if c.slot == 0)
        self.assertAlmostEqual(upper.x, -502, delta=2)
        self.assertAlmostEqual(upper.y, 0, delta=2)
        self.assertEqual([(c.x, c.y) for c in first.chests],
                         [(c.x, c.y) for c in second.chests])

    def test_window_capture_matches_monitor_detection_and_uses_physical_coordinates(self):
        baseline, desktop = self.make_tracker(origin=(-800, -200))
        desktop.monitor_box = desktop.bounds()
        expected = baseline.locate(lambda: False).chests
        desktop.requests.clear()
        desktop.window_region = (-680, -160, -350, 180)
        observer = auto_collect.PetObserver(ChestDetector(), desktop)
        frame = observer.locate(lambda: False)
        self.assertEqual(desktop.requests, [desktop.window_region])
        self.assertEqual(frame.origin, desktop.window_region[:2])
        self.assertEqual([(c.x, c.y, c.slot) for c in frame.chests],
                         [(c.x, c.y, c.slot) for c in expected])
        np.testing.assert_allclose([c.score for c in frame.chests],
                                   [c.score for c in expected], atol=1e-12, rtol=0)

    def test_incomplete_or_unowned_window_result_falls_back_to_current_monitor(self):
        for region, owned in (((120, 220, 450, 380), True),
                              ((120, 40, 450, 380), False)):
            with self.subTest(region=region, owned=owned):
                observer, desktop = self.make_tracker()
                desktop.monitor_box = desktop.bounds()
                desktop.window_region = region
                desktop.owned_by_pet = lambda x, y: owned
                frame = observer.locate(lambda: False)
                self.assertEqual(desktop.requests, [region, desktop.monitor_box])
                self.assertEqual({c.slot for c in frame.chests}, {0, 1})

    def test_window_crop_at_chest_search_edge_falls_back_even_with_two_slots(self):
        observer, desktop = self.make_tracker()
        desktop.monitor_box = desktop.bounds()
        # The fixture itself has both chests, but cropping to its edges
        # truncates the surrounding search needed for alternate bubble sizes.
        desktop.window_region = (230, 160, 350, 340)
        image = desktop.image.crop(desktop.window_region)
        self.assertEqual({c.slot for c in ChestDetector().find(image)}, {0, 1})
        frame = observer.locate(lambda: False)
        self.assertEqual(desktop.requests, [desktop.window_region, desktop.monitor_box])
        self.assertEqual({c.slot for c in frame.chests}, {0, 1})

    def test_window_move_during_capture_reloads_monitor_for_fallback(self):
        observer, desktop = self.make_tracker()
        desktop.monitor_box = (0, 0, 210, 600)
        desktop.window_region = (10, 20, 190, 290)
        capture = desktop.capture

        def capture_after_move(box):
            if box == desktop.window_region:
                desktop.monitor_box = (210, 0, 700, 600)
            return capture(box)

        desktop.capture = capture_after_move
        frame = observer.locate(lambda: False)
        self.assertEqual(desktop.requests, [desktop.window_region, desktop.monitor_box])
        self.assertEqual({c.slot for c in frame.chests}, {0, 1})

    def test_known_monitor_without_chests_does_not_capture_other_displays(self):
        observer, desktop = self.make_tracker()
        desktop.monitor_box = (0, 0, 180, 600)

        frame = observer.locate(lambda: False)

        self.assertEqual(frame.chests, [])
        self.assertEqual(desktop.requests, [desktop.monitor_box])

    def test_first_capture_is_full_then_only_region_is_captured(self):
        observer, desktop = self.make_tracker()
        first = observer.locate(lambda: False)
        second = observer.locate(lambda: False)
        self.assertEqual(desktop.requests[0], None)
        self.assertEqual(len(desktop.requests), 2)
        self.assertIsNotNone(desktop.requests[1])
        self.assertLess(second.image.width*second.image.height, 700*600/2)
        self.assertEqual(len(first.chests), 2)
        self.assertEqual([(c.x, c.y) for c in first.chests],
                         [(c.x, c.y) for c in second.chests])

    def test_missing_pet_in_region_triggers_full_capture(self):
        observer, desktop = self.make_tracker()
        observer.locate(lambda: False)
        desktop.image.paste('#35405c', (0, 0, 700, 600))
        with Image.open(Path(__file__).parent / 'test_fixtures' / 'software_100.png') as pet:
            desktop.image.paste(pet, (470, 280))
        found = observer.locate(lambda: False)
        self.assertEqual(len(desktop.requests), 3)
        self.assertIsNotNone(desktop.requests[1])
        self.assertIsNone(desktop.requests[2])
        self.assertEqual(len(found.chests), 2)
        self.assertTrue(all(c.x > 500 for c in found.chests))

    def test_region_coordinates_preserve_negative_screen_origin(self):
        observer, desktop = self.make_tracker(origin=(-800, -200))
        first = observer.locate(lambda: False)
        second = observer.locate(lambda: False)
        for frame in (first, second):
            upper = next(c for c in frame.chests if c.slot == 0)
            self.assertAlmostEqual(upper.x, -502, delta=2)
            self.assertAlmostEqual(upper.y, 0, delta=2)
        self.assertLess(second.origin[0], 0)

    def test_display_layout_change_forces_full_capture(self):
        observer, desktop = self.make_tracker()
        observer.locate(lambda: False)
        desktop.origin = (-700, 0)
        frame = observer.locate(lambda: False)
        self.assertEqual(desktop.requests, [None, None])
        self.assertTrue(all(c.x < 0 for c in frame.chests))

    def test_stop_after_local_failure_prevents_full_fallback(self):
        observer, desktop = self.make_tracker()
        observer.locate(lambda: False)
        desktop.image.paste('#35405c', (0, 0, 700, 600))
        result = observer.locate(lambda: len(desktop.requests) >= 2)
        self.assertIsNone(result)
        self.assertEqual(len(desktop.requests), 2)

    def test_actual_claim_uses_local_captures_and_screen_coordinates(self):
        observer, desktop = self.make_tracker(origin=(-800, -200))
        result = attempt_claim(observer.detector, desktop, lambda: False,
                               lambda seconds: None, observer=observer)
        self.assertEqual(result, 'unverified')
        self.assertEqual(len(desktop.requests), 3)
        self.assertIsNone(desktop.requests[0])
        self.assertTrue(all(box is not None for box in desktop.requests[1:]))
        x, y = desktop.clicks[0]
        self.assertLess(x, 0)
        self.assertLess(abs(x+502), 2)
        self.assertLess(abs(y), 2)

    def test_windows_region_capture_bypasses_full_capture_and_clips(self):
        desktop = WindowsDesktop.__new__(WindowsDesktop)
        desktop.bounds = lambda: (-800, -200, 800, 600)
        with patch.object(auto_collect.ImageGrab, 'grab') as full, \
                patch.object(auto_collect, 'grab_region', return_value=Image.new('RGB', (100, 150))) as region:
            image, origin = desktop.capture((-900, -250, -700, -50))
        full.assert_not_called()
        region.assert_called_once_with((-800, -200, -700, -50))
        self.assertEqual(origin, (-800, -200))
        self.assertEqual(image.size, (100, 150))

    def test_two_claims_and_cooldown_keep_capturing_only_region(self):
        observer, desktop = self.make_tracker()
        def claim(x, y):
            desktop.clicks.append((x, y))
            lx, ly = round(x-desktop.origin[0]), round(y-desktop.origin[1])
            draw = ImageDraw.Draw(desktop.image)
            draw.rectangle((lx-23, ly-18, lx+23, ly+18), fill='#ffd28b')
            draw.text((lx-10, ly-4), '07:59', fill='#8c5b3f')
            return True
        desktop.click = claim
        for _ in range(2):
            self.assertEqual(attempt_claim(observer.detector, desktop, lambda: False,
                                           lambda seconds: None, observer=observer), 'changed')
        frame = observer.locate(lambda: False)
        self.assertEqual(frame.chests, [])
        self.assertTrue(frame.anchor_visible)
        self.assertEqual(sum(box is None for box in desktop.requests), 1)
        self.assertEqual(len(desktop.clicks), 2)

    def test_region_capture_covers_both_chests_at_all_reference_sizes(self):
        for percent in (50, 100, 150, 200):
            with self.subTest(percent=percent), Image.open(
                    Path(__file__).parent / 'test_fixtures' / f'software_{percent}.png') as source:
                image = Image.new('RGB', (700, 600), '#35405c')
                image.paste(source, (230, 160))
                desktop = ScreenshotDesktop(image, (-800, -200))
                observer = auto_collect.PetObserver(ChestDetector(), desktop)
                first = observer.locate(lambda: False)
                second = observer.locate(lambda: False)
                self.assertEqual(len(first.chests), 2)
                self.assertEqual(len(second.chests), 2)
                self.assertIsNotNone(desktop.requests[1])

    def test_diagnostic_callback_receives_physical_target_and_crop_origin(self):
        observer, desktop = self.make_tracker(origin=(-800, -200))
        recorded = []
        def record(tag, image, target, origin):
            self.assertTrue(0 <= target.x-origin[0] < image.width)
            self.assertTrue(0 <= target.y-origin[1] < image.height)
            recorded.append((tag, round(target.x), round(target.y)))
        attempt_claim(observer.detector, desktop, lambda: False, lambda seconds: None,
                      record=record, observer=observer)
        self.assertEqual([entry[0] for entry in recorded], ['before', 'after'])
        self.assertEqual(recorded[0][1:], desktop.clicks[0])


class ScreenshotDesktop:
    def __init__(self, image, origin):
        self.image, self.origin = image, origin
        self.requests = []
        self.clicks = []
        self.monitor_box = None

    def bounds(self):
        x, y = self.origin
        return (x, y, x+self.image.width, y+self.image.height)

    def pet_monitor_bounds(self):
        return self.monitor_box

    def pet_capture_bounds(self):
        return self.monitor_box, getattr(self, 'window_region', None)

    def capture(self, box=None):
        self.requests.append(box)
        if box is None:
            return self.image.copy(), self.origin
        left, top, right, bottom = box
        ox, oy = self.origin
        return self.image.crop((left-ox, top-oy, right-ox, bottom-oy)), (left, top)

    def owned_by_pet(self, x, y):
        return True

    def click(self, x, y):
        self.clicks.append((x, y))
        return True


class FrameBasedInput:
    """Models an app sampling button state and cursor position once per frame."""
    def __init__(self):
        self.cursor = (500, 400)
        self.app_cursor = self.cursor
        self.needs_input_move = False
        self.origin = (0, 0)
        self.screen_size = (1920, 1080)
        self.down = False
        self.saw_down_on_frame = False
        self.clicks = []
        self.events = []
        self.stop = False

    def GetAsyncKeyState(self, key):
        return 0

    def GetCursorPos(self, pointer):
        pointer._obj.x, pointer._obj.y = self.cursor
        return True

    def GetSystemMetrics(self, metric):
        return {76: self.origin[0], 77: self.origin[1],
                78: self.screen_size[0], 79: self.screen_size[1]}[metric]

    def SetCursorPos(self, x, y):
        self.cursor = (x, y)
        return True

    def SendInput(self, count, pointer, size):
        events = ctypes.cast(pointer, ctypes.POINTER(INPUT))
        for i in range(count):
            flag = events[i].mi.dwFlags
            self.events.append(flag)
            if flag & 0x0001:
                self.cursor = (
                    self.origin[0] + round(events[i].mi.dx * (self.screen_size[0]-1) / 65535),
                    self.origin[1] + round(events[i].mi.dy * (self.screen_size[1]-1) / 65535),
                )
                self.app_cursor = self.cursor
            if flag & 0x0002:
                self.down = True
            elif flag & 0x0004:
                if self.saw_down_on_frame:
                    self.clicks.append(self.app_cursor if self.needs_input_move else self.cursor)
                self.down = False
        return count

    def wait(self, seconds):
        if self.down:
            self.saw_down_on_frame = True


class InputTimingTests(unittest.TestCase):
    def test_stop_hotkey_requires_ctrl_and_alt(self):
        desktop = WindowsDesktop.__new__(WindowsDesktop)
        from unittest.mock import Mock
        desktop.user32 = Mock()
        desktop.user32.GetAsyncKeyState.return_value = 0x8000
        self.assertTrue(desktop.stop_pressed())
        polled = [call.args[0] for call in desktop.user32.GetAsyncKeyState.call_args_list]
        self.assertEqual(polled, [0x11, 0x12])
        desktop.user32.GetAsyncKeyState.side_effect = [0x8000, 0]
        self.assertFalse(desktop.stop_pressed())

    def test_plan_mode_reports_stop_with_exit_code_three(self):
        with tempfile.TemporaryDirectory() as folder:
            signal = Path(folder) / 'collector.stop'
            signal.touch()
            with patch.object(auto_collect, 'WindowsDesktop') as desktop_class, \
                    patch.object(auto_collect, 'ChestDetector'), \
                    patch('sys.argv', ['auto_collect.py', '--hours', '6',
                                       '--stop-file', str(signal), '--plan']), \
                    redirect_stdout(io.StringIO()):
                desktop = desktop_class.return_value
                desktop.stop_pressed.return_value = False
                self.assertEqual(auto_collect.main(), 3)

    def make_desktop(self):
        user32 = FrameBasedInput()
        desktop = WindowsDesktop.__new__(WindowsDesktop)
        desktop.user32 = user32
        desktop.owned_by_pet = lambda x, y: True
        desktop.stop_pressed = lambda: user32.stop
        desktop.wait = user32.wait
        return desktop, user32

    def test_app_can_observe_button_down_before_release(self):
        desktop, user32 = self.make_desktop()
        self.assertTrue(desktop.click(139, 61))
        self.assertEqual(user32.clicks, [(139, 61)])
        self.assertEqual(user32.cursor, (500, 400))

    def test_stop_during_hold_releases_button(self):
        desktop, user32 = self.make_desktop()
        def wait(seconds):
            user32.wait(seconds)
            if user32.down:
                user32.stop = True
        desktop.wait = wait
        desktop.click(139, 61)
        self.assertFalse(user32.down)
        self.assertEqual(user32.events[-1], 0x0004)

    def test_input_driven_app_receives_new_mouse_position(self):
        desktop, user32 = self.make_desktop()
        user32.needs_input_move = True
        desktop.click(139, 61)
        self.assertEqual(user32.clicks, [(139, 61)])

    def test_absolute_mouse_move_supports_negative_monitor_origin(self):
        desktop, user32 = self.make_desktop()
        user32.needs_input_move = True
        user32.origin = (-1920, 0)
        user32.screen_size = (3840, 1080)
        desktop.click(-1000, 500)
        self.assertEqual(user32.clicks, [(-1000, 500)])

    def test_single_attempt_can_leave_pointer_at_target_for_observation(self):
        desktop, user32 = self.make_desktop()
        desktop.restore_pointer = False
        desktop.click(139, 61)
        self.assertEqual(user32.cursor, (139, 61))

    def test_runtime_expiring_before_press_prevents_new_click(self):
        desktop, user32 = self.make_desktop()
        expired = [False]
        desktop.stop_requested = lambda: expired[0]
        def wait(seconds):
            user32.wait(seconds)
            expired[0] = True
        desktop.wait = wait
        self.assertFalse(desktop.click(139, 61))
        self.assertFalse(any(flag & 0x0002 for flag in user32.events))


class GapClickTests(unittest.TestCase):
    class Keyboard:
        def __init__(self):
            self.sent = 0

        def send_f15(self):
            self.sent += 1

    def test_burst_sends_the_configured_count(self):
        keyboard = self.Keyboard()
        sent, reason = auto_collect.run_gap_clicks(
            keyboard, 25, 0.0001, lambda: False, lambda seconds: None)
        self.assertEqual((sent, reason), (25, 'count'))
        self.assertEqual(keyboard.sent, 25)

    def test_burst_stops_with_the_monitor_stop_signal(self):
        keyboard = self.Keyboard()
        sent, reason = auto_collect.run_gap_clicks(
            keyboard, 10, 0.0001, lambda: keyboard.sent >= 3, lambda seconds: None)
        self.assertEqual((sent, reason), (3, 'stopped'))


if __name__ == '__main__':
    unittest.main()
