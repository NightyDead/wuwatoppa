import ctypes
import unittest
from unittest.mock import Mock
from region_capture import GdiRegionCapture


class RegionCaptureTests(unittest.TestCase):
    def make_capture(self):
        user = Mock()
        gdi = Mock()
        user.GetDC.return_value = 10
        gdi.CreateCompatibleDC.return_value = 20
        gdi.SelectObject.return_value = 30
        gdi.BitBlt.return_value = True
        gdi.GdiFlush.return_value = True
        buffer = ctypes.create_string_buffer(bytes([0, 0, 255, 0, 0, 255, 0, 0]))
        def bitmap(dc, info, usage, bits, section, offset):
            self.assertEqual(info._obj.bmiHeader.biWidth, 2)
            self.assertEqual(info._obj.bmiHeader.biHeight, -1)
            bits._obj.value = ctypes.addressof(buffer)
            return 40
        gdi.CreateDIBSection.side_effect = bitmap
        return GdiRegionCapture(user, gdi), user, gdi, buffer

    def test_copies_only_requested_pixels_and_decodes_bgr(self):
        capture, user, gdi, buffer = self.make_capture()
        image = capture.grab((-100, -20, -98, -19))
        self.assertEqual(image.size, (2, 1))
        self.assertEqual(list(image.getdata()), [(255, 0, 0), (0, 255, 0)])
        gdi.BitBlt.assert_called_once_with(20, 0, 0, 2, 1, 10, -100, -20,
                                          0x00CC0020 | 0x40000000)
        gdi.GdiFlush.assert_called_once()
        gdi.DeleteObject.assert_called_once_with(40)
        gdi.DeleteDC.assert_called_once_with(20)
        user.ReleaseDC.assert_called_once_with(None, 10)

    def test_failed_copy_releases_resources(self):
        capture, user, gdi, buffer = self.make_capture()
        gdi.BitBlt.return_value = False
        with self.assertRaises(RuntimeError):
            capture.grab((0, 0, 2, 1))
        gdi.DeleteObject.assert_called_once_with(40)
        gdi.DeleteDC.assert_called_once_with(20)
        user.ReleaseDC.assert_called_once_with(None, 10)

    def test_failed_bitmap_allocation_releases_contexts(self):
        capture, user, gdi, buffer = self.make_capture()
        gdi.CreateDIBSection.side_effect = None
        gdi.CreateDIBSection.return_value = None
        with self.assertRaises(RuntimeError):
            capture.grab((0, 0, 2, 1))
        gdi.DeleteDC.assert_called_once_with(20)
        user.ReleaseDC.assert_called_once_with(None, 10)

    def test_failed_flush_releases_resources_without_reading_pixels(self):
        capture, user, gdi, buffer = self.make_capture()
        gdi.GdiFlush.return_value = False
        with self.assertRaises(RuntimeError):
            capture.grab((0, 0, 2, 1))
        gdi.DeleteObject.assert_called_once_with(40)
        gdi.DeleteDC.assert_called_once_with(20)
        user.ReleaseDC.assert_called_once_with(None, 10)


if __name__ == '__main__':
    unittest.main()
