import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

import settings_transfer as transfer


class SettingsTransferTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_full_settings_round_trip_includes_wallpaper_and_font(self):
        (self.root / 'wall.jpg').write_bytes(b'wallpaper')
        (self.root / 'font.ttf').write_bytes(b'font')
        look = dict(background='wall.jpg', font_file='font.ttf')
        bundle = self.root / 'settings.zip'
        transfer.export_bundle(bundle, self.root, 'settings',
                               dict(count='10', plan=[{'kind': 'clicker'}],
                                    appearance=look, presets={'测试': look}))

        imported = transfer.read_transfer(bundle)
        settings = self.root / 'new' / '设置.json'
        settings.parent.mkdir()
        transfer.install(settings, imported.data, imported.assets)
        self.assertEqual(imported.kind, 'settings')
        self.assertEqual(imported.data['count'], '10')
        self.assertEqual(len(imported.assets), 2)
        self.assertEqual(imported.data, json.loads(settings.read_text(encoding='utf-8')))
        paths = imported.data['presets']['测试']
        self.assertEqual((settings.parent / paths['background']).read_bytes(), b'wallpaper')
        self.assertEqual((settings.parent / paths['font_file']).read_bytes(), b'font')

    def test_theme_round_trip_and_same_name_preserves_existing_theme(self):
        bundle = self.root / 'theme.zip'
        transfer.export_bundle(bundle, self.root, 'theme',
                               {'count': '5', 'presets': {'夜间': {'theme': 'dark'}}})
        imported = transfer.read_transfer(bundle)
        self.assertEqual(imported.kind, 'theme')
        self.assertNotIn('count', imported.data)
        original = {'夜间': {'theme': 'warm'}}
        merged, selected = transfer.merge_themes(original, imported.data['presets'])
        self.assertEqual(merged['夜间'], original['夜间'])
        self.assertEqual(merged[selected]['theme'], 'dark')
        self.assertEqual(transfer.merge_themes(merged, imported.data['presets'])[0], merged)

    def test_legacy_settings_copy_relative_wallpaper_and_report_missing_font(self):
        (self.root / '旧壁纸.jpg').write_bytes(b'wallpaper')
        settings = self.root / '设置.json'
        settings.write_text(json.dumps({'background': '旧壁纸.jpg',
                                        'presets': {'夜间': {'font_file': 'missing.ttf'}}}),
                            encoding='utf-8')
        imported = transfer.read_transfer(settings)
        self.assertEqual(imported.missing, 1)
        self.assertEqual(imported.data['presets']['夜间']['font_file'], '')
        self.assertEqual(imported.assets[imported.data['appearance']['background']], b'wallpaper')

    def test_archive_cannot_reference_local_or_traversal_paths(self):
        bundle = self.root / 'theme.zip'
        with ZipFile(bundle, 'w') as archive:
            archive.writestr(transfer.MANIFEST, json.dumps(dict(
                format=transfer.FORMAT, version=1, kind='theme',
                data={'presets': {'bad': {'background': '../private.jpg'}}})))
        with self.assertRaisesRegex(ValueError, '无效的资源路径'):
            transfer.read_transfer(bundle)
