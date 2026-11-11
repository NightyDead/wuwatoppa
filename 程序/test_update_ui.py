from concurrent.futures import Future
from threading import Event
from types import SimpleNamespace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import launcher
import updater


class UpdateUiTests(unittest.TestCase):
    def window(self, automatic=False, result=None, error=None):
        task = Future()
        if error:
            task.set_exception(error)
        else:
            task.set_result(result)
        return SimpleNamespace(
            closing=False, _update_task=task, _update_release=None,
            _update_automatic=automatic, _update_stop=Event(),
            _update_executor=Mock(), update_action=Mock(), auto_update_action=Mock(),
            workers=SimpleNamespace(active=False), plan=SimpleNamespace(running=False),
            set_notice=Mock(), append=Mock(), error=Mock(), request_close=Mock(),
            check_for_updates=Mock())

    def release(self, notes='修复跨屏识别\n减少冷却期间的重复计算'):
        return updater.Release('1.1.5', updater.package_url('1.1.5'), 'a'*64, 1024, notes)

    def test_startup_respects_switch(self):
        for enabled in (False, True):
            w = self.window()
            w.auto_update_action.isChecked.return_value = enabled
            launcher.MainWindow._check_startup_update(w)
            self.assertEqual(w.check_for_updates.call_count, int(enabled))
            if enabled:
                w.check_for_updates.assert_called_once_with(automatic=True)

    def test_manual_check_works_with_switch_off_and_prevents_duplicate_requests(self):
        w = self.window()
        w._update_task = None
        w.auto_update_action.isChecked.return_value = False
        launcher.MainWindow.check_for_updates(w)
        launcher.MainWindow.check_for_updates(w, automatic=True)
        w._update_executor.submit.assert_called_once_with(
            updater.check_release, launcher.APP_VERSION, w._update_stop)
        self.assertFalse(w._update_automatic)

    def test_no_update_and_network_failure_are_silent_only_for_automatic_checks(self):
        for automatic in (False, True):
            for error in (None, ValueError('network unavailable')):
                with self.subTest(automatic=automatic, error=error):
                    w = self.window(automatic, error=error)
                    with patch.object(launcher, 'QMessageBox') as dialog:
                        launcher.MainWindow._poll_update(w)
                        dialog.assert_not_called()
                    self.assertEqual(w.error.call_count, int(error is not None and not automatic))
                    self.assertEqual(w.set_notice.call_count, int(error is None and not automatic))

    def test_new_version_dialog_shows_notes_and_declining_never_downloads(self):
        for automatic in (False, True):
            for notes in ('修复跨屏识别\n减少重复计算', ''):
                release = self.release(notes)
                w = self.window(automatic, result=release)
                buttons = launcher.QMessageBox.StandardButton
                with patch.object(launcher, 'QMessageBox') as dialog:
                    dialog.StandardButton = buttons
                    dialog.return_value.exec.return_value = buttons.No
                    launcher.MainWindow._poll_update(w)
                    message = dialog.return_value.setText.call_args.args[0]
                    self.assertIn('最新版本：v1.1.5', message)
                    self.assertIn('当前版本：v' + launcher.APP_VERSION, message)
                    self.assertIn(notes or '此版本未提供更新日志。', message)
                    dialog.return_value.setDefaultButton.assert_called_once_with(buttons.No)
                w._update_executor.submit.assert_not_called()
                w.request_close.assert_not_called()

    def test_accepting_downloads_only_when_idle_and_folder_build(self):
        for active, folder in ((False, True), (True, True), (False, False)):
            w = self.window(True, result=self.release())
            w.workers.active = active
            buttons = launcher.QMessageBox.StandardButton
            with patch.object(launcher, 'QMessageBox') as dialog, \
                    patch.object(updater, 'is_folder_build', return_value=folder):
                dialog.StandardButton = buttons
                dialog.return_value.exec.return_value = buttons.Yes
                launcher.MainWindow._poll_update(w)
            self.assertEqual(w._update_executor.submit.call_count, int(folder and not active))
            w.request_close.assert_not_called()
            if folder and not active:
                self.assertEqual(w._update_release.version, '1.1.5')
                self.assertFalse(w._update_automatic)

    def test_switch_is_saved_with_existing_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            w = self.window()
            w.settings_path = Path(folder) / 'settings.json'
            w.saved_settings = {'custom': 'preserved'}
            w.values, w.plan_steps, w.saved_appearance, w.presets = {}, [], {}, {}
            w.window_settings = lambda: {}
            w._business = lambda: launcher.MainWindow._business(w)
            for enabled in (True, False):
                w.auto_update_action.isChecked.return_value = enabled
                launcher.MainWindow.save(w)
                saved = json.loads(w.settings_path.read_text(encoding='utf-8'))
                self.assertIs(saved['auto_check_updates'], enabled)
                self.assertEqual(saved['custom'], 'preserved')


if __name__ == '__main__':
    unittest.main()
