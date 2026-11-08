"""Portable theme and settings archives, with support for older local settings."""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

import appearance

MANIFEST = 'settings.json'
FORMAT = 'desktop-pet-assistant'
MAX_JSON = 1024 * 1024
MAX_FILE = 32 * 1024 * 1024
MAX_ASSETS = 96 * 1024 * 1024
SUFFIXES = {
    'background': {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif', '.ico', '.tif', '.tiff'},
    'font_file': {'.ttf', '.otf'},
}
IMPORT_FOLDER = '外观资源/导入'


@dataclass
class Transfer:
    kind: str
    data: dict
    assets: dict
    missing: int = 0


@contextmanager
def _atomic_destination(path):
    with NamedTemporaryFile(dir=path.parent, suffix='.tmp', delete=False) as handle:
        temporary = Path(handle.name)
    try:
        yield temporary
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_file(path, limit):
    with path.open('rb') as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f'文件超出大小限制：{path.name}')
    return content


def _read_json(content):
    data = json.loads(content.decode('utf-8-sig'))
    if not isinstance(data, dict):
        raise ValueError('设置文件必须包含一个 JSON 对象。')
    return data


def _rewrite(data, relocate, kind):
    result = dict(data) if kind == 'settings' else {}
    if kind == 'settings':
        raw = data.get('appearance')
        if not isinstance(raw, dict):
            raw = {'background': data.get('background', appearance.DEFAULTS['background'])}
        result['appearance'] = appearance.normalize_appearance(raw)
        result.pop('background', None)
    presets = appearance.normalize_presets(data.get('presets'))
    if kind == 'theme' and not presets:
        raise ValueError('分享包没有可用的主题。')
    result['presets'] = presets
    looks = list(presets.values())
    if kind == 'settings':
        looks.append(result['appearance'])
    for look in looks:
        for field in SUFFIXES:
            value = look[field]
            if value and not (field == 'background' and value == appearance.DEFAULTS[field]):
                look[field] = relocate(field, value)
    return result


def _add_asset(assets, name, content):
    if name not in assets:
        if len(content) + sum(map(len, assets.values())) > MAX_ASSETS:
            raise ValueError('主题资源合计超过 96 MiB。')
        assets[name] = content


def export_bundle(path, root, kind, data):
    assets = {}

    def relocate(field, value):
        resource = root / value
        suffix = resource.suffix.lower()
        if suffix not in SUFFIXES[field]:
            raise ValueError(f'不支持分享此资源格式：{resource.name}')
        content = _read_file(resource, MAX_FILE)
        name = f'assets/{sha256(content).hexdigest()}{suffix}'
        _add_asset(assets, name, content)
        return name

    payload = _rewrite(data, relocate, kind)
    manifest = json.dumps(dict(format=FORMAT, version=1, kind=kind, data=payload),
                          ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(manifest) > MAX_JSON:
        raise ValueError('设置内容超过 1 MiB。')
    with _atomic_destination(path) as temporary, ZipFile(temporary, 'w', ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST, manifest)
        for name, content in assets.items():
            archive.writestr(name, content)


def _read_bundle(path):
    assets = {}
    with ZipFile(path) as archive:
        if len(archive.infolist()) != len(set(archive.namelist())):
            raise ValueError('分享包存在重复文件。')

        def read(name, limit):
            try:
                entry = archive.getinfo(name)
            except KeyError:
                raise ValueError(f'分享包缺少文件：{name}') from None
            if entry.file_size > limit:
                raise ValueError(f'分享包中的文件超出大小限制：{name}')
            return archive.read(entry)

        manifest = _read_json(read(MANIFEST, MAX_JSON))
        kind, data = manifest.get('kind'), manifest.get('data')
        if (manifest.get('format') != FORMAT or manifest.get('version') != 1
                or kind not in ('theme', 'settings') or not isinstance(data, dict)):
            raise ValueError('分享包格式或版本不受支持。')

        def relocate(field, value):
            match = re.fullmatch(r'assets/([0-9a-f]{64})(\.[a-z]+)', value)
            if match is None or match[2] not in SUFFIXES[field]:
                raise ValueError('分享包包含无效的资源路径。')
            destination = f'{IMPORT_FOLDER}/{match[1]}{match[2]}'
            if destination not in assets:
                content = read(value, MAX_FILE)
                if sha256(content).hexdigest() != match[1]:
                    raise ValueError('分享包内的资源校验失败。')
                _add_asset(assets, destination, content)
            return destination

        return Transfer(kind, _rewrite(data, relocate, kind), assets)


def _read_legacy(path):
    data = _read_json(_read_file(path, MAX_JSON))
    if not data.keys() & {'appearance', 'presets', 'background', 'count', 'hours', 'plan'}:
        raise ValueError('没有找到可导入的旧版设置。')
    root = path.parent.resolve()
    assets = {}
    missing = 0

    def relocate(field, value):
        nonlocal missing
        resource = (root / value).resolve()
        suffix = resource.suffix.lower()
        if not resource.is_relative_to(root) or suffix not in SUFFIXES[field] or not resource.is_file():
            missing += 1
            return appearance.DEFAULTS[field]
        content = _read_file(resource, MAX_FILE)
        destination = f'{IMPORT_FOLDER}/{sha256(content).hexdigest()}{suffix}'
        _add_asset(assets, destination, content)
        return destination

    data = _rewrite(data, relocate, 'settings')
    return Transfer('settings', data, assets, missing)


def read_transfer(path):
    if path.suffix.lower() == '.json':
        return _read_legacy(path)
    try:
        return _read_bundle(path)
    except (BadZipFile, NotImplementedError, RuntimeError) as error:
        raise ValueError(f'无法读取分享包：{error}') from error


def merge_themes(existing, incoming):
    result = dict(existing)
    selected = None
    for original, theme in incoming.items():
        name = original
        number = 2
        while name in result and result[name] != theme:
            suffix = f' ({number})'
            name = original[:appearance.PRESET_NAME_LIMIT - len(suffix)] + suffix
            number += 1
        if name not in result and len(result) >= appearance.PRESET_LIMIT:
            raise ValueError(f'主题最多保存 {appearance.PRESET_LIMIT} 套，请先腾出空间。')
        result[name] = theme
        if selected is None:
            selected = name
    return result, selected


def install(settings_path, data, assets):
    for name, content in assets.items():
        destination = settings_path.parent / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_file() and _read_file(destination, MAX_FILE) == content:
            continue
        with _atomic_destination(destination) as temporary:
            temporary.write_bytes(content)
    appearance.write_settings_atomic(settings_path, data)
