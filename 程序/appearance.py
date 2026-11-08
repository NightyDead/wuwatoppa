"""Validated appearance preferences and atomic settings storage."""
import copy
import json
import math
import os
from pathlib import Path
import re
import tempfile

# 背景默认使用程序自带的那张壁纸；改成 '' 就是纯色。
DEFAULTS = dict(version=1, theme='warm', accent='', panel_color='', panel_opacity=.88,
                window_opacity=1., background='背景/背景.png', background_mode='cover',
                background_brightness=1., font_family='文鼎方新书', font_file='',
                font_size='normal', density='standard', log_expanded=False,
                taskbar_icon=True)
PALETTES = {
    'warm': dict(background='#F3E8D8', panel='#FFF8EC', text='#49301E', muted='#78604A',
                 accent='#9A5B0B', border='#CFB28D'),
    'dark': dict(background='#18222D', panel='#263240', text='#EDF1F5', muted='#AFBBC8',
                 accent='#82B4F2', border='#526171'),
    'light': dict(background='#EEF1F4', panel='#FFFFFF', text='#26313D', muted='#5C6875',
                  accent='#245FA8', border='#B8C3CE')}
# 命名主题方案：每套是一份完整外观快照，数量与名称长度都要有上限。
PRESET_LIMIT = 24
PRESET_NAME_LIMIT = 24


def number(value, default, low, high):
    if isinstance(value, bool): return default
    try: value = float(value)
    except (TypeError, ValueError): return default
    return min(high, max(low, value)) if math.isfinite(value) else default


def normalize_appearance(raw):
    result = copy.deepcopy(DEFAULTS)
    if not isinstance(raw, dict): return result
    choices = dict(theme=PALETTES, background_mode=('cover', 'contain'),
                   font_size=('small', 'normal', 'large'), density=('standard', 'compact'))
    for key, options in choices.items():
        if isinstance(raw.get(key), str) and raw[key] in options: result[key] = raw[key]
    for key in ('accent', 'panel_color'):
        value = raw.get(key)
        if isinstance(value, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', value): result[key] = value
    for key, lo, hi in (('panel_opacity', .3, 1), ('window_opacity', .6, 1), ('background_brightness', .5, 1.5)):
        result[key] = number(raw.get(key), result[key], lo, hi)
    for key in ('background', 'font_family', 'font_file'):
        if isinstance(raw.get(key), str): result[key] = raw[key][:2048]
    if isinstance(raw.get('log_expanded'), bool): result['log_expanded'] = raw['log_expanded']
    if isinstance(raw.get('taskbar_icon'), bool): result['taskbar_icon'] = raw['taskbar_icon']
    return result


def normalize_presets(raw):
    """Named appearance snapshots; broken entries are dropped, not repaired."""
    result = {}
    if not isinstance(raw, dict):
        return result
    for name, value in raw.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            continue
        name = name.strip()[:PRESET_NAME_LIMIT]
        if not name or name in result:
            continue
        result[name] = normalize_appearance(value)
        if len(result) >= PRESET_LIMIT:
            break
    return result


def rgb(color): return tuple(int(color[i:i+2], 16) for i in (1, 3, 5))


def luminance(color):
    values = [v/255 for v in rgb(color)]
    values = [v/12.92 if v <= .04045 else ((v+.055)/1.055)**2.4 for v in values]
    return sum(v*w for v, w in zip(values, (.2126, .7152, .0722)))


def contrast(first, second):
    hi, lo = sorted((luminance(first), luminance(second)), reverse=True)
    return (hi+.05)/(lo+.05)


def foreground(color): return '#FFFFFF' if contrast('#FFFFFF', color) > contrast('#202020', color) else '#202020'


def blend(first, second, alpha):
    return '#%02x%02x%02x' % tuple(round(a*alpha+b*(1-alpha)) for a, b in zip(rgb(first), rgb(second)))


def resolve_palette(options):
    options = normalize_appearance(options)
    result = dict(PALETTES[options['theme']])
    if options['accent']: result['accent'] = options['accent']
    if options['panel_color']:
        result['panel'] = options['panel_color']
        result['text'] = foreground(result['panel'])
        result['muted'] = blend(result['text'], result['panel'], .72)
        result['border'] = blend(result['text'], result['panel'], .3)
    dark = luminance(result['panel']) < .3
    result.update(success='#74D6A2' if dark else '#176444', danger='#FFA79B' if dark else '#A22D27',
                  field=blend(result['panel'], '#000000' if dark else '#FFFFFF', .72),
                  selection=blend(result['accent'], result['panel'], .18),
                  accent_text=foreground(result['accent']))
    result['nav_accent'] = '#B9780D' if options['theme'] == 'warm' and not options['accent'] else result['accent']
    result['nav_selection'] = blend('#F2BD60', result['panel'], .35) if options['theme'] == 'warm' and not options['accent'] else result['selection']
    return result


def merge_settings(existing, business, appearance, window, presets=None):
    result = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    result.update(business)
    result['appearance'] = normalize_appearance(appearance)
    # None means "keep whatever the file already has", so plain saves do not
    # wipe the named themes that live next to the active appearance.
    presets = result.get('presets') if presets is None else presets
    if presets is not None:
        result['presets'] = normalize_presets(presets)
    result['window'] = dict(window)
    return result


def write_settings_atomic(path, settings):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=path.name+'.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(settings, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
