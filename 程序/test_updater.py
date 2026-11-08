from hashlib import sha256
import json
from pathlib import Path
import tempfile
import ssl
from threading import Event
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from zipfile import ZipFile
from io import BytesIO

import updater


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def package(self, files):
        path = self.root / 'desktop-pet-assistant-1.1.4.zip'
        with ZipFile(path, 'w') as archive:
            for name, content in files.items():
                archive.writestr(f'{updater.APP}/{name}', content)
        return path

    def test_manifest_round_trip_and_no_downgrade(self):
        package = self.package({updater.EXE: b'app'})
        manifest = updater.write_manifest('1.1.4', package, '更新内容')
        self.assertIsNone(updater.parse_release(json.dumps(manifest), '1.1.4'))
        self.assertIsNone(updater.parse_release(json.dumps(manifest), '1.1.10'))
        release = updater.parse_release(json.dumps(manifest), '1.1.3')
        self.assertEqual(release.digest, sha256(package.read_bytes()).hexdigest())
        manifest['url'] = 'https://example.org/package.zip'
        with self.assertRaises(ValueError):
            updater.parse_release(json.dumps(manifest), '1.1.3')

    def test_rejects_wrong_checksum(self):
        payload = b'package'
        path = self.root / 'file.zip'
        release = updater.Release('1.1.4', updater.package_url('1.1.4'),
                                  '0' * 64, len(payload), '')
        with patch.object(updater, '_open', return_value=BytesIO(payload)):
            with self.assertRaisesRegex(ValueError, 'SHA-256'):
                updater._download(release, path, Event())

    def test_unpack_valid_release_and_reject_personal_files_or_aliases(self):
        valid = {updater.EXE: b'app', '_internal/base_library.zip': b'runtime'}
        updater.unpack_package(self.package(valid), self.root / 'valid', Event())
        self.assertEqual((self.root / 'valid' / updater.EXE).read_bytes(), b'app')
        for name in ('../escape', '_internal/../escape', '_internal/设置.json',
                     '_internal/外观资源/image.jpg', '_internal/collector/领取记录.json',
                     '_internal/a:payload', '_internal/NUL.txt', '_internal/base_library.ZIP'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    updater.unpack_package(self.package(dict(valid, **{name: b'bad'})),
                                           self.root / f'invalid-{len(list(self.root.iterdir()))}', Event())

    def check_stop(self):
        return Mock(is_set=Mock(return_value=False), wait=Mock(return_value=False))

    def current_manifest(self):
        return BytesIO(b'{"format":1,"version":"1.1.4"}')

    def test_check_retries_reset_during_open_and_read(self):
        class ResetOnRead(BytesIO):
            def read(self, *args):
                raise ConnectionResetError(10054, 'reset')

        for first in (URLError(ConnectionResetError(10054, 'reset')), ResetOnRead()):
            with self.subTest(first=type(first).__name__):
                stopped = self.check_stop()
                with patch.object(updater, '_open', side_effect=[first, self.current_manifest()]) as request:
                    self.assertIsNone(updater.check_release('1.1.4', stopped))
                self.assertEqual(request.call_count, 2)
                stopped.wait.assert_called_once_with(0.5)

    def test_check_stops_after_three_connection_failures(self):
        stopped = self.check_stop()
        with patch.object(updater, '_open', side_effect=URLError(ConnectionResetError(10054, 'reset'))) as request:
            with self.assertRaisesRegex(ValueError, '连接被中断.*10054'):
                updater.check_release('1.1.4', stopped)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(stopped.wait.call_count, 2)

    def test_check_retries_timeout_and_temporary_server_error(self):
        for error in (URLError(TimeoutError()), HTTPError(updater.MANIFEST, 503, 'Unavailable', {}, None)):
            with self.subTest(error=error):
                with patch.object(updater, '_open', side_effect=[error, self.current_manifest()]) as request:
                    self.assertIsNone(updater.check_release('1.1.4', self.check_stop()))
                self.assertEqual(request.call_count, 2)

    def test_check_does_not_retry_missing_manifest_rate_limit_or_certificate_error(self):
        failures = [
            (HTTPError(updater.MANIFEST, 404, 'Not Found', {}, None), '尚未发布'),
            (HTTPError(updater.MANIFEST, 403, 'Forbidden', {}, None), '拒绝'),
            (HTTPError(updater.MANIFEST, 429, 'Too Many Requests', {}, None), '拒绝'),
            (URLError(ssl.SSLCertVerificationError(1, 'certificate verify failed')), '安全证书'),
            (ValueError('未授权的地址'), '未授权'),
        ]
        for error, message in failures:
            with self.subTest(message=message):
                stopped = self.check_stop()
                with patch.object(updater, '_open', side_effect=error) as request:
                    with self.assertRaisesRegex(ValueError, message):
                        updater.check_release('1.1.4', stopped)
                self.assertEqual(request.call_count, 1)
                stopped.wait.assert_not_called()

    def test_check_cancellation_prevents_request_and_retry(self):
        stopped = Event()
        stopped.set()
        with patch.object(updater, '_open') as request:
            with self.assertRaisesRegex(ValueError, '取消'):
                updater.check_release('1.1.4', stopped)
        request.assert_not_called()
        stopped = self.check_stop()
        stopped.wait.return_value = True
        with patch.object(updater, '_open', side_effect=ConnectionResetError(10054, 'reset')) as request:
            with self.assertRaisesRegex(ValueError, '取消'):
                updater.check_release('1.1.4', stopped)
        self.assertEqual(request.call_count, 1)

    def test_check_keeps_manifest_limit_and_validation_after_retry(self):
        for payload in (b'x' * (updater.MAX_MANIFEST + 1), b'not json',
                        b'{"format":1,"version":"1.1.5","url":"https://example.org/bad"}'):
            with self.subTest(length=len(payload)):
                with patch.object(updater, '_open', side_effect=[ConnectionResetError(), BytesIO(payload)]) as request:
                    with self.assertRaises(ValueError):
                        updater.check_release('1.1.4', self.check_stop())
                self.assertEqual(request.call_count, 2)

    def test_check_uses_the_fixed_github_manifest_url(self):
        with patch.object(updater, '_open', return_value=BytesIO(
                b'{"format":1,"version":"1.1.4"}')) as open_url:
            self.assertIsNone(updater.check_release('1.1.4'))
        open_url.assert_called_once_with(updater.MANIFEST)
