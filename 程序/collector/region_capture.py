"""Capture a physical screen rectangle without first copying the whole desktop."""

import ctypes
from ctypes import wintypes
from PIL import Image


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wintypes.DWORD), ('biWidth', wintypes.LONG),
                ('biHeight', wintypes.LONG), ('biPlanes', wintypes.WORD),
                ('biBitCount', wintypes.WORD), ('biCompression', wintypes.DWORD),
                ('biSizeImage', wintypes.DWORD), ('biXPelsPerMeter', wintypes.LONG),
                ('biYPelsPerMeter', wintypes.LONG), ('biClrUsed', wintypes.DWORD),
                ('biClrImportant', wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', BITMAPINFOHEADER), ('bmiColors', wintypes.DWORD * 1)]


class GdiRegionCapture:
    def __init__(self, user32=None, gdi32=None):
        self.user = user32 if user32 is not None else ctypes.WinDLL('user32', use_last_error=True)
        self.gdi = gdi32 if gdi32 is not None else ctypes.WinDLL('gdi32', use_last_error=True)
        api = (
            (self.user.GetDC, [wintypes.HWND], wintypes.HDC),
            (self.user.ReleaseDC, [wintypes.HWND, wintypes.HDC], ctypes.c_int),
            (self.gdi.CreateCompatibleDC, [wintypes.HDC], wintypes.HDC),
            (self.gdi.CreateDIBSection, [wintypes.HDC, ctypes.POINTER(BITMAPINFO),
             wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE,
             wintypes.DWORD], wintypes.HBITMAP),
            (self.gdi.SelectObject, [wintypes.HDC, wintypes.HANDLE], wintypes.HANDLE),
            (self.gdi.BitBlt, [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
             ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD], wintypes.BOOL),
            (self.gdi.GdiFlush, [], wintypes.BOOL),
            (self.gdi.DeleteObject, [wintypes.HANDLE], wintypes.BOOL),
            (self.gdi.DeleteDC, [wintypes.HDC], wintypes.BOOL),
        )
        for function, arguments, result in api:
            function.argtypes, function.restype = arguments, result

    def grab(self, box):
        left, top, right, bottom = box
        width, height = right-left, bottom-top
        if width <= 0 or height <= 0:
            raise ValueError('局部截图范围不能为空。')
        source = self.user.GetDC(None)
        if not source:
            raise RuntimeError('无法取得屏幕截图设备。')
        destination = bitmap = old = None
        try:
            destination = self.gdi.CreateCompatibleDC(source)
            if not destination:
                raise RuntimeError('无法建立局部截图缓冲区。')
            info = BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            info.bmiHeader.biWidth = width
            info.bmiHeader.biHeight = -height  # Top-down, 32-bit BGRX.
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            bits = ctypes.c_void_p()
            bitmap = self.gdi.CreateDIBSection(source, ctypes.byref(info), 0,
                                              ctypes.byref(bits), None, 0)
            if not bitmap or not bits.value:
                raise RuntimeError('无法分配局部截图像素。')
            old = self.gdi.SelectObject(destination, bitmap)
            if not old or old == ctypes.c_void_p(-1).value:
                old = None
                raise RuntimeError('无法选用局部截图缓冲区。')
            # CAPTUREBLT includes layered desktop-pet windows.
            # https://learn.microsoft.com/windows/win32/api/wingdi/nf-wingdi-bitblt
            if not self.gdi.BitBlt(destination, 0, 0, width, height, source,
                                   left, top, 0x00CC0020 | 0x40000000):
                raise RuntimeError('局部屏幕截图失败。')
            # CreateDIBSection requires synchronization before accessing its pixels.
            if not self.gdi.GdiFlush():
                raise RuntimeError('局部截图像素同步失败。')
            pixels = ctypes.string_at(bits, width*height*4)
            return Image.frombytes('RGB', (width, height), pixels, 'raw', 'BGRX')
        finally:
            if old:
                self.gdi.SelectObject(destination, old)
            if bitmap:
                self.gdi.DeleteObject(bitmap)
            if destination:
                self.gdi.DeleteDC(destination)
            self.user.ReleaseDC(None, source)


_capture = None


def grab_region(box):
    global _capture
    if _capture is None:
        _capture = GdiRegionCapture()
    return _capture.grab(box)
