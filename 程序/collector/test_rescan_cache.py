from pathlib import Path
import math
import unittest
from unittest.mock import patch

from PIL import Image
import auto_collect


class RescanCacheTests(unittest.TestCase):
    def setUp(self):
        self.detector = auto_collect.ChestDetector()
        with Image.open(Path(__file__).parent / 'assets' / 'ready_reference.png') as image:
            self.pet = image.convert('RGB')
        self.image = self.canvas()
        self.target = self.detector.find(self.image)[0]

    def canvas(self, dx=0, dy=0):
        image = Image.new('RGB', (700, 450), '#35405c')
        image.paste(self.pet, (150+dx, 100+dy))
        return image

    def test_identical_pixels_reuse_results_and_translate_coordinates(self):
        detector = self.detector
        with patch.object(detector, '_scan', wraps=detector._scan) as scan:
            first = detector.rescan_region(self.image, self.target)
            anchor = detector.last_anchor
            expected = list(first)
            first.clear()  # Callers must not be able to alter the saved result.
            self.assertEqual(detector.rescan_region(self.image.copy(), self.target), expected)
            shifted = detector.rescan_region(self.canvas(17, -11),
                                             auto_collect.shift_match(self.target, 17, -11))
            self.assertEqual(shifted, [auto_collect.shift_match(c, 17, -11) for c in expected])
            self.assertEqual(detector.last_anchor, auto_collect.shift_match(anchor, 17, -11))
            self.assertEqual(scan.call_count, 1)
        width, height = detector._rescan_cache[0][0]
        self.assertEqual(len(detector._rescan_cache[1]), width*height)

    def test_single_changed_pixel_forces_a_new_complete_search(self):
        detector = self.detector
        changed = self.image.copy()
        left, top, _, _ = detector._region(self.image, self.target, extra=math.ceil(40*self.target.scale))
        changed.putpixel((left, top), (255, 255, 255))
        with patch.object(detector, '_scan', wraps=detector._scan) as scan:
            detector.rescan_region(self.image, self.target)
            result = detector.rescan_region(changed, self.target)
            self.assertEqual({chest.slot for chest in result}, {0, 1})
            self.assertEqual(scan.call_count, 2)

    def test_each_threshold_change_and_explicit_reset_invalidate_cache(self):
        detector = self.detector
        with patch.object(detector, '_scan', wraps=detector._scan) as scan:
            detector.rescan_region(self.image, self.target)
            for index, name in enumerate(('anchor_threshold', 'anchor_floor',
                                          'chest_threshold', 'chest_fallback'), start=2):
                setattr(detector, name, getattr(detector, name) + 0.001)
                detector.rescan_region(self.image, self.target)
                self.assertEqual(scan.call_count, index)
            detector.clear_cache()
            self.assertIsNone(detector.last_anchor)
            detector.rescan_region(self.image, self.target)
            self.assertEqual(scan.call_count, 6)

    def test_equal_pixel_bytes_with_different_dimensions_do_not_match(self):
        detector = self.detector
        images = [Image.new('L', size, 42) for size in ((100, 60), (120, 50))]
        self.assertEqual(images[0].tobytes(), images[1].tobytes())
        with patch.object(detector, '_region', side_effect=lambda image, *args, **kw:
                          (0, 0, image.width, image.height)), \
                patch.object(detector, '_scan', wraps=detector._scan) as scan:
            for image in images:
                detector.rescan_region(image, self.target)
            self.assertEqual(scan.call_count, 2)

    def test_oversized_regions_are_searched_but_never_retained(self):
        detector = self.detector
        small = Image.new('L', (16, 16), 42)
        large = Image.new('L', (1025, 1025), 42)
        with patch.object(detector, '_region', side_effect=lambda image, *args, **kw:
                          (0, 0, image.width, image.height)), \
                patch.object(detector, '_scan', return_value=([], False)) as scan:
            detector.rescan_region(small, self.target)
            self.assertLessEqual(len(detector._rescan_cache[1]), 1024**2)
            for _ in range(2):
                detector.rescan_region(large, self.target)
                self.assertIsNone(detector._rescan_cache)
            self.assertEqual(scan.call_count, 3)


if __name__ == '__main__':
    unittest.main()
