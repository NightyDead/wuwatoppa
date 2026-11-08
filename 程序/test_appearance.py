import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import appearance as a
import launcher
import ui_theme


class AppearanceTests(unittest.TestCase):
    def test_old_settings_and_unknown_fields_survive(self):
        value = a.merge_settings({'hours': '1', 'note': 'keep'}, {'count': '2000'},
                                 a.normalize_appearance(None), {})
        self.assertEqual(value['hours'], '1')
        self.assertEqual(value['note'], 'keep')
        self.assertEqual(value['appearance']['theme'], 'warm')

    def test_invalid_numbers_and_colors(self):
        value = a.normalize_appearance({'window_opacity': float('nan'), 'panel_opacity': True,
                                        'accent': 'red', 'theme': 'broken'})
        self.assertEqual(value['window_opacity'], 1)
        self.assertEqual(value['panel_opacity'], .88)
        self.assertEqual(value['accent'], '')
        self.assertEqual(value['theme'], 'warm')

    def test_taskbar_switch_defaults_to_visible_and_only_takes_booleans(self):
        self.assertTrue(a.normalize_appearance(None)['taskbar_icon'])
        self.assertFalse(a.normalize_appearance({'taskbar_icon': False})['taskbar_icon'])
        self.assertTrue(a.normalize_appearance({'taskbar_icon': 'no'})['taskbar_icon'])

    def test_palettes_and_custom_colors_are_readable(self):
        for theme in a.PALETTES:
            colors = a.resolve_palette(a.normalize_appearance({'theme': theme}))
            self.assertGreaterEqual(a.contrast(colors['text'], colors['panel']), 4.5)
        colors = a.resolve_palette(a.normalize_appearance({'panel_color': '#101010'}))
        self.assertGreaterEqual(a.contrast(colors['text'], colors['panel']), 4.5)

    def test_presets_drop_broken_entries_and_keep_readable_values(self):
        presets = a.normalize_presets({
            ' 暗夜 ': {'theme': 'dark', 'accent': '#123456', 'panel_color': '#101820'},
            'broken': 'x',
            '': {'theme': 'light'},
            'x' * 40: {'theme': 'light'},
        })
        self.assertEqual(list(presets), ['暗夜', 'x' * a.PRESET_NAME_LIMIT])
        self.assertEqual(presets['暗夜']['theme'], 'dark')
        self.assertEqual(presets['暗夜']['accent'], '#123456')
        self.assertEqual(presets['暗夜']['panel_color'], '#101820')

    def test_preset_count_is_bounded(self):
        raw = {f'主题{n}': {'theme': 'dark'} for n in range(a.PRESET_LIMIT + 8)}
        self.assertEqual(len(a.normalize_presets(raw)), a.PRESET_LIMIT)

    def test_plain_saves_keep_presets_and_presets_can_be_replaced(self):
        saved = a.merge_settings({}, {}, a.normalize_appearance(None), {},
                                 {'A': {'theme': 'dark', 'accent': '#111111'}})
        self.assertIn('A', saved['presets'])
        kept = a.merge_settings(saved, {'count': '10'}, a.normalize_appearance(None), {})
        self.assertEqual(kept['count'], '10')
        self.assertIn('A', kept['presets'])
        replaced = a.merge_settings(saved, {}, a.normalize_appearance(None), {}, {'B': {}})
        self.assertEqual(list(replaced['presets']), ['B'])

    def test_preset_wallpaper_paths_are_stable_and_distinct(self):
        first = ui_theme.preset_background('暗夜')
        self.assertEqual(first, ui_theme.preset_background('暗夜'))
        self.assertTrue(first.startswith(f'{ui_theme.BACKGROUND_FOLDER}/'
                                         f'{ui_theme.PRESET_FOLDER}/'))
        self.assertTrue(first.endswith('.jpg'))
        self.assertNotEqual(first, ui_theme.preset_background('暗夜2'))

    def test_atomic_failure_preserves_previous_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'settings.json'
            path.write_text('{"hours":"1"}')
            with patch.object(a.os, 'replace', side_effect=OSError('denied')):
                with self.assertRaises(OSError): a.write_settings_atomic(path, {'hours': '8'})
            self.assertEqual(json.loads(path.read_text()), {'hours': '1'})
            self.assertEqual(len(list(Path(folder).iterdir())), 1)


class AwakeTests(unittest.TestCase):
    def test_running_tasks_block_sleep_and_screen_off(self):
        flags = launcher.awake_flags(True)
        self.assertTrue(flags & launcher.ES_CONTINUOUS)
        self.assertTrue(flags & launcher.ES_SYSTEM_REQUIRED)
        self.assertTrue(flags & launcher.ES_DISPLAY_REQUIRED)

    def test_idle_app_releases_the_request(self):
        self.assertEqual(launcher.awake_flags(False), launcher.ES_CONTINUOUS)


if __name__ == '__main__':
    unittest.main()
