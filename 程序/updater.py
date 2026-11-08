"""Fetch and stage a verified folder release for the Windows restart updater."""
from dataclasses import dataclass
import ctypes
from ctypes import wintypes
from hashlib import sha256
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import time
from threading import Event
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import ZipFile

RELEASES = 'https://github.com/NightyDead/wuwatoppa/releases'
MANIFEST = RELEASES + '/latest/download/update.json'
HOSTS = {'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}
APP = '桌宠助手'
EXE = APP + '.exe'
MAX_PACKAGE = 120 * 1024**2
MAX_UNPACKED = 512 * 1024**2
MAX_MANIFEST = 64 * 1024
PRESERVE = ('设置.json', '外观资源', '领取记录.json', 'runtime', 'collector/领取记录.json',
            'collector/auto_collect.log', 'collector/diagnostics', 'collector/preview.png',
            '启动错误.log')
EVENT_PREFIX = r'Local\WuwaToppaUpdate-'
STAGE_PREFIX = '.wuwatoppa-update-'


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    digest: str
    size: int
    notes: str


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9]\d{0,5})(\.(0|[1-9]\d{0,5})){2}', value, re.ASCII):
        raise ValueError('更新版本号格式无效。')
    return tuple(map(int, value.split('.')))


def package_url(version):
    return f'{RELEASES}/download/v{version}/desktop-pet-assistant-{version}.zip'


def parse_release(payload, current):
    data = json.loads(payload)
    if not isinstance(data, dict) or type(data.get('format')) is not int or data['format'] != 1:
        raise ValueError('更新清单格式无效。')
    version = data.get('version')
    if version_tuple(version) <= version_tuple(current):
        return None
    url, digest, size = data.get('url'), data.get('sha256'), data.get('size')
    if (url != package_url(version) or not isinstance(digest, str)
            or not re.fullmatch('[0-9a-fA-F]{64}', digest)
            or type(size) is not int or not 0 < size <= MAX_PACKAGE):
        raise ValueError('更新包地址、大小或校验值无效。')
    notes = data.get('notes')
    return Release(version, url, digest.lower(), size, notes[:4000] if isinstance(notes, str) else '')


class _ReleaseRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        url = urlsplit(newurl)
        if (url.scheme != 'https' or url.hostname not in HOSTS
                or url.port not in (None, 443) or url.username is not None):
            raise ValueError('更新链接重定向到了未授权的地址。')
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def _open(url):
    return build_opener(_ReleaseRedirects()).open(
        Request(url, headers={'User-Agent': 'WuwaToppa-Updater'}), timeout=12)


def _check_error_message(error):
    if isinstance(error, HTTPError):
        if error.code == 404:
            return 'GitHub 尚未发布可用的更新清单（HTTP 404）。请等待发布者上传 update.json。'
        if error.code in (403, 429):
            return f'GitHub 暂时拒绝更新请求（HTTP {error.code}），请稍后重试。'
        return f'GitHub 更新服务返回 HTTP {error.code}，请稍后重试。'
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        return '无法验证 GitHub 的安全证书，请检查系统时间和代理的证书配置。'
    if isinstance(reason, TimeoutError):
        return '连接 GitHub 更新服务超时，请检查网络或系统代理后重试。'
    code = getattr(reason, 'winerror', None) or getattr(reason, 'errno', None)
    if isinstance(reason, ConnectionResetError) or code == 10054:
        detail = f'（错误码 {code}）' if code is not None else ''
        return f'与 GitHub 更新服务的连接被中断{detail}，请稍后重试，并检查网络或系统代理。'
    return '无法连接 GitHub 更新服务，请检查网络或系统代理后重试。'


def check_release(current, stopped=None):
    stopped = stopped if stopped is not None else Event()
    for attempt in range(3):
        if stopped.is_set():
            raise ValueError('更新检查已取消。')
        try:
            with _open(MANIFEST) as response:
                content = response.read(MAX_MANIFEST + 1)
        except (URLError, OSError, HTTPException) as error:
            reason = error.reason if isinstance(error, URLError) else error
            retryable = (not isinstance(error, HTTPError)
                         or error.code in (408, 500, 502, 503, 504))
            if retryable and not isinstance(reason, ssl.SSLCertVerificationError) and attempt < 2:
                # Interrupt the backoff when the application is closing.
                if stopped.wait(0.5 * (attempt + 1)):
                    raise ValueError('更新检查已取消。') from error
                continue
            raise ValueError(_check_error_message(error)) from error
        break
    if stopped.is_set():
        raise ValueError('更新检查已取消。')
    if len(content) > MAX_MANIFEST:
        raise ValueError('更新清单过大。')
    return parse_release(content, current)


def _download(release, path, stopped):
    digest = sha256()
    size = 0
    deadline = time.monotonic() + 180
    with _open(release.url) as response, path.open('xb') as stream:
        while True:
            if stopped.is_set() or time.monotonic() >= deadline:
                raise ValueError('更新下载已取消或超时。')
            block = response.read1(128 * 1024)
            if not block:
                break
            size += len(block)
            if size > release.size:
                raise ValueError('更新包超过清单中的大小。')
            digest.update(block)
            stream.write(block)
    if size != release.size or digest.hexdigest() != release.digest:
        raise ValueError('更新包的大小或 SHA-256 校验失败。')


def _member_path(name):
    parts = name.removesuffix('/').split('/')
    for part in parts:
        if (not part or part in ('.', '..') or part[-1] in ' .'
                or re.search(r'[<>:"\\|?*\x00-\x1f]', part)
                or re.fullmatch(r'(?i:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³]|conin\$|conout\$)',
                                part.split('.')[0])):
            raise ValueError('更新包包含无效文件路径。')
    if parts[0] != APP:
        raise ValueError('更新包目录结构不正确。')
    return parts[1:]


def unpack_package(package, destination, stopped):
    protected = tuple(tuple(part.casefold() for part in name.split('/')) for name in PRESERVE)
    seen = set()
    with ZipFile(package) as archive:
        entries = archive.infolist()
        if len(entries) > 4096 or sum(item.file_size for item in entries) > MAX_UNPACKED:
            raise ValueError('更新包解压大小超过限制。')
        for item in entries:
            if stopped.is_set():
                raise ValueError('更新已取消。')
            parts = _member_path(item.orig_filename)
            if not parts and item.is_dir():
                continue
            if not parts or stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError('更新包包含不支持的文件。')
            key = tuple(part.casefold() for part in parts)
            if key in seen or (parts[0] != '_internal' and parts not in ([EXE], ['使用说明.txt'])):
                raise ValueError('更新包包含重复或未知的文件。')
            if parts[0] == '_internal' and any(key[1:1+len(prefix)] == prefix for prefix in protected):
                raise ValueError('更新包包含个人数据，已拒绝覆盖。')
            seen.add(key)
            path = destination.joinpath(*parts)
            if item.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as stream, path.open('xb') as output:
                    shutil.copyfileobj(stream, output, 128 * 1024)
    if not (destination / EXE).is_file() or not (destination / '_internal' / 'base_library.zip').is_file():
        raise ValueError('更新包缺少 EXE 或运行库。')


def is_folder_build(exe, root):
    return (getattr(sys, 'frozen', False) and exe.name == EXE
            and root == exe.parent / '_internal' and (root / 'update_install.ps1').is_file())


def _system_directory():
    api = ctypes.windll.kernel32
    api.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    api.GetSystemDirectoryW.restype = wintypes.UINT
    path = ctypes.create_unicode_buffer(32768)
    if not 0 < api.GetSystemDirectoryW(path, len(path)) < len(path):
        raise OSError('无法定位 Windows 系统目录。')
    return Path(path.value)


def stage_release(release, install, stopped):
    stage = Path(tempfile.mkdtemp(prefix=STAGE_PREFIX, dir=install.parent))
    try:
        # The elevated installer must not execute or apply files writable by
        # a lower-integrity process during the handoff.
        label = subprocess.run([str(_system_directory() / 'icacls.exe'), str(stage),
                                '/setintegritylevel', '(OI)(CI)H'],
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if label.returncode:
            raise OSError('无法保护更新临时目录，请检查安装位置和管理员权限。')
        package = stage / 'release.zip'
        _download(release, package, stopped)
        unpack_package(package, stage / 'new', stopped)
        package.unlink()
        return stage
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def start_installer(stage, exe, helper):
    script = stage / 'install.ps1'
    shutil.copyfile(helper, script)
    (stage / 'preserve.json').write_text(json.dumps(PRESERVE), encoding='utf-8')
    powershell = _system_directory() / 'WindowsPowerShell/v1.0/powershell.exe'
    subprocess.Popen([str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy',
                      'Bypass', '-File', str(script), '-Install', str(exe.parent),
                      '-ParentPid', str(os.getpid())], cwd=stage, close_fds=True,
                     creationflags=subprocess.CREATE_NO_WINDOW)


def signal_ready(token):
    if not re.fullmatch('[0-9a-f]{32}', token):
        return
    api = ctypes.windll.kernel32
    api.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    api.OpenEventW.restype = wintypes.HANDLE
    api.SetEvent.argtypes = [wintypes.HANDLE]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    event = api.OpenEventW(0x0002, False, EVENT_PREFIX + token)
    if event:
        try:
            api.SetEvent(event)
        finally:
            api.CloseHandle(event)


def write_manifest(version, path, notes=''):
    version_tuple(version)
    if path.name != f'desktop-pet-assistant-{version}.zip':
        raise ValueError('更新包文件名必须包含对应版本号。')
    size = path.stat().st_size
    if not 0 < size <= MAX_PACKAGE:
        raise ValueError('更新包大小超出限制。')
    with path.open('rb') as stream:
        digest = sha256()
        for block in iter(lambda: stream.read(128 * 1024), b''):
            digest.update(block)
    return dict(format=1, version=version, url=package_url(version),
                sha256=digest.hexdigest(), size=size, notes=notes)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='生成 GitHub Release 的 update.json。')
    parser.add_argument('version')
    parser.add_argument('package', type=Path)
    parser.add_argument('--notes', default='')
    parser.add_argument('--output', type=Path, default=Path('update.json'))
    args = parser.parse_args()
    manifest = write_manifest(args.version, args.package, args.notes)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
