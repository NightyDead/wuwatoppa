"""Bundled fonts, loaded privately into this process only; nothing is installed."""
from pathlib import Path
import struct

from PySide6.QtGui import QFontDatabase

FALLBACKS = ('microsoft yahei ui', 'microsoft yahei', 'simhei')


def family_key(family):
    value = ''.join(c for c in family.casefold() if c.isalnum())
    return 'fangxinshu' if any(v in value for v in ('方新书', '方新書', 'fangxinshu')) else value


def font_display_name(family):
    return '文鼎方新书' if family_key(family) == 'fangxinshu' else family


def font_choices(families):
    return sorted({'文鼎方新书'} | {font_display_name(name) for name in families if not name.startswith('@')}, key=str.casefold)


def resolve_font_path(path, root):
    value = Path(path)
    return (value if value.is_absolute() else Path(root)/value).resolve()


def find_bundled_font(family, root):
    folder = Path(root)/'fonts'
    if not folder.is_dir(): return None
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() not in ('.ttf', '.otf'): continue
        try:
            if family_key(font_family(path)) == family_key(family): return path
        except (ValueError, OSError): continue
    return None


def font_family(path):
    """Read the family name straight from the TTF/OTF name table."""
    path = Path(path)
    if path.suffix.lower() not in ('.ttf', '.otf'): raise ValueError('请选择 TTF 或 OTF 字体。')
    data = path.read_bytes()
    try:
        if data[:4] not in (b'\x00\x01\x00\x00', b'OTTO', b'true'): raise ValueError()
        count = struct.unpack_from('>H', data, 4)[0]
        names = []
        for index in range(count):
            tag, _, offset, length = struct.unpack_from('>4sIII', data, 12+16*index)
            if offset+length > len(data): raise ValueError()
            if tag != b'name': continue
            table = data[offset:offset+length]
            _, records, strings = struct.unpack_from('>HHH', table)
            for i in range(records):
                platform, encoding, language, name_id, size, start = struct.unpack_from('>6H', table, 6+12*i)
                if name_id not in (1, 16): continue
                start += strings
                if start+size > len(table): raise ValueError()
                raw = table[start:start+size]
                name = raw.decode('utf-16-be' if platform in (0, 3) else 'mac_roman', errors='strict').strip('\0 ')
                if name: names.append(((name_id == 16, language == 0x409), name))
        if names: return max(names)[1]
    except (struct.error, UnicodeError, IndexError): pass
    raise ValueError('字体文件损坏，或无法读取字体家族名称。')


class FontManager:
    """Preview a font family; commit keeps it for the session, cancel unloads it."""

    def __init__(self, resource_root=None):
        self.resource_root = Path(resource_root) if resource_root is not None else Path(__file__).resolve().parent
        self.registered = {}
        self.saved = self.current = ''

    def available_families(self): return QFontDatabase.families()

    @staticmethod
    def _lookup(family):
        wanted = family_key(family)
        for name in QFontDatabase.families():
            if not name.startswith('@') and family_key(name) == wanted: return name
        return None

    @staticmethod
    def _fallback():
        lookup = {name.casefold(): name for name in QFontDatabase.families()}
        for key in FALLBACKS:
            if key in lookup: return lookup[key]
        return lookup.get('microsoft yahei ui', 'Microsoft YaHei UI')

    def _prune(self):
        for path in tuple(set(self.registered) - {self.saved, self.current}):
            QFontDatabase.removeApplicationFont(self.registered.pop(path))

    def preview(self, family, file_path=''):
        self.current = ''
        hint = ''
        if not file_path:
            packaged = find_bundled_font(family, self.resource_root)
            if packaged is not None: file_path = str(packaged)
        if file_path:
            try:
                path = str(resolve_font_path(file_path, self.resource_root))
                declared = font_family(path)
                if path not in self.registered:
                    font_id = QFontDatabase.addApplicationFont(path)
                    if font_id < 0: raise ValueError('Qt 未能加载字体。')
                    self.registered[path] = font_id
                self.current = path
                loaded = QFontDatabase.applicationFontFamilies(self.registered[path])
                family = loaded[0] if loaded else declared
            except (ValueError, OSError) as error:
                hint = f'自定义字体不可用：{error}；'
        actual = self._lookup(family)
        if actual is None:
            actual = self._fallback()
            hint += f'{font_display_name(family)}未找到，当前使用微软雅黑。'
            self.current = ''
        self._prune()
        return actual, hint

    def commit(self): self.saved = self.current; self._prune()

    def cancel(self): self.current = self.saved; self._prune()

    def close(self):
        for font_id in tuple(self.registered.values()): QFontDatabase.removeApplicationFont(font_id)
        self.registered.clear()
