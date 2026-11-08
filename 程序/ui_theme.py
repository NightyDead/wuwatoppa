"""Bounded wallpaper rendering and Win32 window helpers for the Qt launcher."""
from io import BytesIO
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import tempfile

from PIL import Image, ImageEnhance, ImageOps

from PySide6.QtGui import QImage, QPainterPath, QPixmap

MAX_PIXELS = 20_000_000
MAX_BYTES = 2 * 1024 * 1024
BACKGROUND_FOLDER = '外观资源'
BACKGROUND_NAME = '背景.jpg'
PRESET_FOLDER = '主题'

GA_ROOT = 2
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020


def import_background(path):
    """Read any picture once and normalise it to a bounded RGB image."""
    with Image.open(path) as source:
        if source.width * source.height > MAX_PIXELS:
            raise ValueError('图片超过 2000 万像素，请选择较小的图片。')
        source.seek(0)
        image = ImageOps.exif_transpose(source)
        image.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
        if image.mode in ('RGBA', 'LA') or 'transparency' in image.info:
            base = Image.new('RGBA', image.size, '#F3E8D8')
            base.alpha_composite(image.convert('RGBA'))
            return base.convert('RGB')
        return image.convert('RGB')


def preset_background(name):
    """Per-theme wallpaper path; hashing keeps odd names out of the file name."""
    digest = hashlib.sha1(name.encode('utf-8')).hexdigest()[:12]
    return f'{BACKGROUND_FOLDER}/{PRESET_FOLDER}/{digest}.jpg'


def save_background(path, image, commit):
    """Replace the app-owned picture, rolling back if the settings commit fails."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_bytes() if path.exists() else None
    temporary = None
    try:
        if image is not None:
            payload = BytesIO()
            for quality in (88, 80):
                payload.seek(0); payload.truncate()
                image.save(payload, 'JPEG', quality=quality, optimize=True)
                if payload.tell() <= MAX_BYTES: break
            if payload.tell() > MAX_BYTES:
                raise ValueError('压缩后的背景超过 2MB，请换一张图片。')
            fd, temporary = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
            with os.fdopen(fd, 'wb') as handle:
                handle.write(payload.getvalue()); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, path)
        else:
            path.unlink(missing_ok=True)
        try: commit()
        except Exception:
            if old is not None: path.write_bytes(old)
            else: path.unlink(missing_ok=True)
            raise
    finally:
        if temporary: Path(temporary).unlink(missing_ok=True)


def rgb(color):
    color = color.lstrip('#')
    return tuple(int(color[i:i+2], 16) for i in (0, 2, 4))


class Backdrop:
    """One wallpaper image, rendered once per size/mode/brightness and reused."""

    def __init__(self):
        self.source = None
        self._key = self._image = None

    def invalidate(self): self._key = self._image = None

    def render(self, size, color, mode, brightness):
        size = tuple(max(1, int(v)) for v in size)
        key = (size, color, mode, brightness, id(self.source))
        if key == self._key: return self._image
        base = Image.new('RGB', size, color)
        if self.source is not None:
            if mode == 'contain':
                image = ImageOps.contain(self.source, size, Image.Resampling.LANCZOS)
                image = ImageEnhance.Brightness(image).enhance(brightness)
                base.paste(image, ((size[0]-image.width)//2, (size[1]-image.height)//2))
            else:
                base = ImageEnhance.Brightness(
                    ImageOps.fit(self.source, size, Image.Resampling.LANCZOS)).enhance(brightness)
        self._key, self._image = key, base
        return base


def to_pixmap(image, ratio=1.0):
    data = image.tobytes('raw', 'RGB')
    qimage = QImage(data, image.width, image.height, image.width * 3,
                    QImage.Format_RGB888).copy()
    pixmap = QPixmap.fromImage(qimage)
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def taskbar(hwnd):
    """Keep the caption-less window listed on the task bar so it can come back."""
    try:
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        user32.SetWindowLongW.restype = ctypes.c_long
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        wintypes.UINT]
        user32.SetWindowPos.restype = wintypes.BOOL
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        wanted = (style | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW
        if wanted != style:
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, wanted)
        user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                            SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER
                            | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        return True
    except (AttributeError, OSError, ctypes.ArgumentError):
        return False


def rounded_path(rect, radius):
    path = QPainterPath()
    if radius <= 0:
        path.addRect(rect)
    else:
        path.addRoundedRect(rect, radius, radius)
    return path
