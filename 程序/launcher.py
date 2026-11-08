"""One window for the keyboard macro and chest collector."""

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import queue
import runpy
import shutil
import sys
from threading import Event
import traceback

from PySide6.QtCore import (QEasingCurve, QPointF, QPropertyAnimation, QRect, QRectF,
                            Qt, QTimer)
from PySide6.QtGui import (QColor, QFont, QGuiApplication, QIcon, QPainter, QPen,
                           QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup,
                               QColorDialog, QDialog, QFileDialog, QFrame,
                               QGridLayout, QHBoxLayout, QHeaderView, QInputDialog,
                               QLabel, QLineEdit,
                               QMenu, QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QSystemTrayIcon, QTableWidget,
                               QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

import appearance
import settings_transfer
import ui_theme
import updater
from font_support import FontManager, font_choices, font_display_name
from jobs import (Workers, ROOT, COLLECTOR, SCRIPTS,
                  PlanRunner, PLAN_FIELDS, plan_options, step_options, step_summary,
                  read_claim_history, claim_summary, CLAIM_STATUS)
from ui_state import MonitorState
from ui_widgets import (Divider, DragFilter, NavItem, NoWheelComboBox, NoWheelSlider,
                        Panel, WindowButton, app_qss)

NAMES = {'clicker': '连点器', 'collector': '自动领取', 'preview': '识别预览',
         'test': '单次领取', 'check_clicker': '连点器检查', 'check_collector': '领取环境检查'}
APP_VERSION = '1.1.4'
DEFAULT_VALUES = {'count': '2000', 'interval': '0.1', 'duration': '0',
                  'hours': '6', 'minutes': '15', 'idle': '0', 'idle_interval': '0.1'}
PAGES = [('clicker', '连点器'), ('collector', '自动领取'), ('plan', '运行计划'),
         ('history', '领取记录'), ('appearance', '外观设置')]
THEMES = {'金色': 'warm', '深色': 'dark', '浅色': 'light'}
FONT_SIZES = {'小': 'small', '标准': 'normal', '大': 'large'}
FONT_POINTS = {'small': 9, 'normal': 10, 'large': 12}
DENSITIES = {'标准': 'standard', '紧凑': 'compact'}
BACKGROUND_MODES = {'填充': 'cover', '适应': 'contain'}
STEP_FIELDS = {
    'clicker': [('操作次数', 'count', '次'), ('操作间隔', 'interval', '秒'),
                ('运行时长', 'duration', '小时')],
    'collector': [('运行时长', 'hours', '小时'), ('检查间隔', 'minutes', '分钟'),
                  ('间隙连点', 'idle', '次'), ('连点间隔', 'idle_interval', '秒')],
}
STEP_HINTS = {
    'clicker': '次数 0～100000（0 表示不限）；间隔 0.0001～60 秒。\n'
               '时长 0～1000 小时（0 表示不限）；两项都填时先到先停。',
    'collector': '运行时长必须大于 0 小时；检查间隔至少 0.5 秒（按分钟填写）。\n'
                 '间隙连点为每轮领取结束后发送的按键次数，0 表示不连点；间隔 0.0001～60 秒。',
}
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
RESIZE_EDGE = 6
MIN_WIDTH, MIN_HEIGHT = 660, 520
RESIZE_CURSORS = {'n': Qt.CursorShape.SizeVerCursor,
                  's': Qt.CursorShape.SizeVerCursor,
                  'e': Qt.CursorShape.SizeHorCursor,
                  'w': Qt.CursorShape.SizeHorCursor,
                  'ne': Qt.CursorShape.SizeBDiagCursor,
                  'sw': Qt.CursorShape.SizeBDiagCursor,
                  'nw': Qt.CursorShape.SizeFDiagCursor,
                  'se': Qt.CursorShape.SizeFDiagCursor}


def awake_flags(enabled):
    """Execution-state flags: keep both the machine and the screen awake."""
    if not enabled:
        return ES_CONTINUOUS
    return ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED


def keep_awake(enabled):
    """Stop sleep and the screen saver while tasks run, or the shots go stale."""
    try:
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
        kernel32.SetThreadExecutionState.restype = wintypes.DWORD
        return bool(kernel32.SetThreadExecutionState(awake_flags(enabled)))
    except (AttributeError, OSError):
        return False


class MainWindow(QWidget):
    """Frameless wallpaper window with five pages, a sidebar and a status bar."""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle(f'桌宠助手 v{APP_VERSION}')
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
        self.setMouseTracking(True)

        self.settings_path = ROOT / '设置.json'
        self.workers = Workers()
        self.plan = PlanRunner(self.workers)
        self.state = MonitorState()
        self.closing = False
        self._update_executor = ThreadPoolExecutor(max_workers=1)
        self._update_stop = Event()
        self._update_task = None
        self._update_release = None
        self.awake = False
        self.maximized = False
        self._resizing = None
        self._cursor = None
        self._fade = None
        self._tray = None
        self._tray_hint = False
        self._wallpaper = None
        self._wallpaper_key = None
        self._metric_jobs = []
        self._sized = []
        self._row_metrics = []
        self._drag_widgets = []
        self._status = {'clicker': '尚未启动', 'collector': '尚未启动'}
        self.status_labels = {}
        self.fields = {}
        self.inputs = {'clicker': [], 'collector': []}
        self.buttons = {}
        self.plan_steps = []
        self._appearance_widgets = {}
        self.slider_labels = {}
        self._font_key = None
        self._appearance_timer = QTimer(self)
        self._appearance_timer.setSingleShot(True)
        self._appearance_timer.timeout.connect(self.preview_options)
        self._wallpaper_timer = QTimer(self)
        self._wallpaper_timer.setSingleShot(True)
        self._wallpaper_timer.timeout.connect(self._rebuild_wallpaper)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self.save)

        self._load_settings()
        self._prepare_metrics()
        self._build()
        self.apply_style()
        self._restore_geometry(self.saved_settings.get('window'))
        self.layout_()
        self._build_tray()
        self.show_last_summary()
        self.append('助手', '就绪。切换页面不影响后台任务；Ctrl + Alt 全部停止。')
        self.refresh()
        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self.tick)
        self._tick_timer.start(100)

    # ---------------------------------------------------------------- 设置与度量

    def _load_settings(self):
        self.saved_settings = {}
        try:
            loaded = json.loads(self.settings_path.read_text(encoding='utf-8'))
            if isinstance(loaded, dict):
                self.saved_settings = loaded
        except (OSError, ValueError):
            pass
        saved = self.saved_settings
        self.values = {key: str(saved.get(key, default)) if isinstance(
            saved.get(key, default), (str, int, float)) else default
            for key, default in DEFAULT_VALUES.items()}
        raw = saved.get('appearance')
        if not isinstance(raw, dict):
            # 旧版设置只在顶层存了背景路径，迁移时不要丢掉用户换过的壁纸。
            raw = {'background': saved.get('background', appearance.DEFAULTS['background'])}
        self.saved_appearance = appearance.normalize_appearance(raw)
        self.presets = appearance.normalize_presets(saved.get('presets'))
        self.options = copy.deepcopy(self.saved_appearance)
        self.fonts = FontManager(ROOT)
        self.family, self.font_hint = self.fonts.preview(
            self.options['font_family'], self.options['font_file'])
        self.fonts.commit()
        self._font_key = (self.options['font_family'], self.options['font_file'])
        self.backdrop = ui_theme.Backdrop()
        self.saved_image = self._load_background(self.options['background'])
        self.candidate = None
        self.candidate_changed = False
        self.backdrop.source = self.saved_image
        self._log_expanded = self.options['log_expanded']
        self.page = saved.get('window', {}).get('page', 'collector')
        if self.page not in {key for key, _ in PAGES}:
            self.page = 'collector'
        raw_plan = saved.get('plan')
        if isinstance(raw_plan, list):
            for step in raw_plan:
                cleaned = self._clean_step(step)
                if cleaned is not None:
                    self.plan_steps.append(cleaned)

    def _prepare_metrics(self):
        self.palette = appearance.resolve_palette(self.options)
        self.palette['panel_opacity'] = self.options['panel_opacity']
        self.point = FONT_POINTS[self.options['font_size']]
        self.scale = self.point / 10
        self.spacing = .85 if self.options['density'] == 'compact' else 1

    def px(self, value):
        return round(value * self.scale)

    def _load_background(self, path):
        if not path:
            return None
        try:
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = ROOT / candidate
            return ui_theme.import_background(candidate)
        except (OSError, ValueError):
            return None

    def _restore_geometry(self, saved):
        saved = saved if isinstance(saved, dict) else {}
        def integer(key, default):
            value = saved.get(key)
            return value if type(value) is int else default
        screen = QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen is not None else QRect(0, 0, 1280, 800)
        width = min(max(MIN_WIDTH, integer('width', 1020)), area.width())
        height = min(max(MIN_HEIGHT, integer('height', 760)), area.height())
        x = min(max(integer('x', area.x() + 60), area.x()), area.right() + 1 - width)
        y = min(max(integer('y', area.y() + 60), area.y()), area.bottom() + 1 - height)
        self._normal_geometry = QRect(x, y, width, height)
        self.setGeometry(self._normal_geometry)
        if saved.get('maximized'):
            QTimer.singleShot(0, self.toggle_max)

    def window_settings(self):
        g = self._normal_geometry
        return dict(width=g.width(), height=g.height(), x=g.x(), y=g.y(),
                    maximized=self.maximized, page=self.page)

    def _business(self):
        data = {key: self.values.get(key, '') for key in DEFAULT_VALUES}
        data['plan'] = self.plan_steps
        return data

    def save(self):
        try:
            data = appearance.merge_settings(self.saved_settings, self._business(),
                                             self.saved_appearance,
                                             self.window_settings(), self.presets)
            appearance.write_settings_atomic(self.settings_path, data)
            self.saved_settings = data
        except (OSError, ValueError) as error:
            self.append('助手', f'本次设置未保存：{error}')

    # ------------------------------------------------------------------ 搭建界面

    def _build(self):
        self.setStyleSheet('')
        self.drag_filter = DragFilter(self)
        self.side = Panel(self.palette, 0, self)
        self.content = Panel(self.palette, self.px(8), self)
        self.footer = Panel(self.palette, 0, self)

        self.brand = QLabel('桌宠助手', self.side)
        self.brand.setObjectName('brand')
        self.brand.setCursor(Qt.CursorShape.OpenHandCursor)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav = {}
        for key, title in PAGES:
            item = NavItem(key, title, self.side)
            item.clicked.connect(lambda checked=False, page=key: self.show_page(page))
            self.nav_group.addButton(item)
            self.nav[key] = item
        self.nav[self.page].setChecked(True)
        self.tools_button = QToolButton(self.side)
        self.tools_button.setText('工具 ▾')
        self.tools_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tools_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.tools_menu = QMenu(self.tools_button)
        self.tools_menu.addAction('一键同时启动', self.start_both)
        self.tools_menu.addAction('识别预览（不点击）', lambda: self.run('preview'))
        self.tools_menu.addSeparator()
        self.tools_menu.addAction('测试连点 10 次',
                                  lambda: self.run('clicker', ['--count', '10',
                                                               '--interval', '0.1',
                                                               '--hours', '0']))
        self.tools_menu.addAction('测试领取一个宝箱', lambda: self.run('test'))
        self.tools_menu.addAction('检查运行环境', self.check_environment)
        self.tools_menu.addAction('打开诊断图片目录', self.open_diagnostics)
        self.tools_menu.addSeparator()
        self.tools_menu.addAction('分享主题…', self.export_theme)
        self.tools_menu.addAction('导出完整设置…', self.export_settings)
        self.tools_menu.addAction('导入主题或设置…', self.import_settings)
        self.tools_menu.addSeparator()
        self.update_action = self.tools_menu.addAction('检查更新…', self.check_for_updates)
        self.tools_button.setMenu(self.tools_menu)

        self.min_button = WindowButton('min', self.content)
        self.min_button.clicked.connect(self.hide_to_tray)
        self.max_button = WindowButton('max', self.content)
        self.max_button.clicked.connect(self.toggle_max)
        self.close_button = WindowButton('close', self.content)
        self.close_button.clicked.connect(self.request_close)

        self.scroll = QScrollArea(self.content)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.viewport().setAutoFillBackground(False)
        self.page_widgets = {}
        self._build_pages()
        self.scroll.setWidget(self.page_widgets[self.page])

        self.log_toggle = QToolButton(self)
        self.log_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.log_toggle.clicked.connect(self.toggle_log)
        self.latest = QLabel(self)
        self.latest.setObjectName('muted')
        self.log = QPlainTextEdit(self)
        self.log.setObjectName('log')
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1200)

        self.footer_status = QLabel(self.footer)
        self.footer_status.setObjectName('muted')
        self.stop_all = QPushButton('全部停止', self.footer)
        self.stop_all.setObjectName('danger')
        self.stop_all.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_all.clicked.connect(self.stop_everything)
        self.shortcut = QLabel('Ctrl + Alt', self.footer)
        self.shortcut.setObjectName('muted')
        self.version_label = QLabel(f'v{APP_VERSION}', self.footer)
        self.version_label.setObjectName('muted')
        for widget in (self.brand, self.side, self.footer):
            widget.installEventFilter(self.drag_filter)
        self.sync_appearance_widgets()
        self.set_notice('填写参数后启动；两项功能可以同时运行。')

    def _build_pages(self):
        for key, _ in PAGES:
            page = QWidget()
            page.setObjectName('page')
            getattr(self, f'_build_{key}_page')(page)
            self.page_widgets[key] = page

    def _page_head(self, page, title, subtitle):
        body = QVBoxLayout(page)
        body.setContentsMargins(self.px(16), self.px(10), self.px(16), self.px(12))
        body.setSpacing(0)
        name = QLabel(title)
        name.setObjectName('pageTitle')
        sub = QLabel(subtitle)
        sub.setObjectName('muted')
        sub.setWordWrap(True)
        body.addWidget(name)
        body.addSpacing(self.px(2))
        body.addWidget(sub)
        body.addSpacing(self.px(22))
        self._metric_jobs.append(lambda: body.setContentsMargins(
            self.px(16), self.px(10), self.px(16), self.px(12)))
        for label in (name, sub):
            label.installEventFilter(self.drag_filter)
        return body

    def _field(self, parent, key, title, unit=''):
        holder = QWidget(parent)
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(self.px(5))
        label = QLabel(f'{title}   {unit}' if unit else title, holder)
        label.setObjectName('muted')
        edit = QLineEdit(self.values.get(key, ''), holder)
        edit.textChanged.connect(lambda text, name=key: self.values.__setitem__(name, text))
        self.fields[key] = edit
        self._sized.append((edit, None, 36))
        column.addWidget(label)
        column.addWidget(edit)
        column.addStretch(1)
        self._metric_jobs.append(lambda: column.setSpacing(self.px(5)))
        return holder, edit

    def _grid(self, parent):
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(self.px(24))
        grid.setVerticalSpacing(0)
        self._metric_jobs.append(lambda: grid.setHorizontalSpacing(self.px(24)))
        return grid

    def _button(self, parent, text, command, width=140, accent=False):
        button = QPushButton(text, parent)
        if accent:
            button.setObjectName('accent')
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda checked=False: command())
        self._sized.append((button, width, 38))
        return button

    def _button_row(self, body, actions):
        row = QHBoxLayout()
        row.setSpacing(self.px(12))
        widgets = []
        for text, command, width, accent in actions:
            button = self._button(body.parentWidget(), text, command, width, accent)
            row.addWidget(button)
            widgets.append(button)
        row.addStretch(1)
        body.addLayout(row)
        self._metric_jobs.append(lambda: row.setSpacing(self.px(12)))
        return widgets

    def _status_label(self, parent, body):
        label = QLabel('尚未启动', parent)
        label.setObjectName('status')
        label.setWordWrap(True)
        body.addWidget(label)
        return label

    def _build_clicker_page(self, page):
        body = self._page_head(page, '连点器', '发送 F15 按键，按次数或时长结束。')
        grid = self._grid(page)
        fields = [('操作次数', 'count', '次'), ('操作间隔', 'interval', '秒'),
                  ('运行时长', 'duration', '小时')]
        for index, (title, key, unit) in enumerate(fields):
            holder, edit = self._field(page, key, title, unit)
            grid.addWidget(holder, index // 2, index % 2)
            self.inputs['clicker'].append(edit)
        for row in range(2):
            self._row_metrics.append((grid, row, 90))
        body.addLayout(grid)
        body.addSpacing(self.px(10))
        body.addWidget(Divider(page))
        body.addSpacing(self.px(24))
        rules = QLabel('次数 0～100000；时长 0～1000 小时。\n'
                       '0 表示不限，两项至少填一项，先到先停。\n间隔 0.0001～60 秒。', page)
        rules.setObjectName('muted')
        rules.setWordWrap(True)
        body.addWidget(rules)
        body.addSpacing(self.px(18))
        self.status_labels['clicker'] = self._status_label(page, body)
        body.addSpacing(self.px(12))
        self.buttons['clicker'] = self._button_row(body, [
            ('开始连点', lambda: self.run('clicker'), 150, True),
            ('停止连点', lambda: self.stop('clicker'), 130, False)])
        body.addStretch(1)

    def _build_collector_page(self, page):
        body = self._page_head(page, '自动领取', '按设定间隔检查并领取宝箱。')
        grid = self._grid(page)
        for index, (title, key, unit) in enumerate([('运行时长', 'hours', '小时'),
                                                     ('检查间隔', 'minutes', '分钟')]):
            holder, edit = self._field(page, key, title, unit)
            grid.addWidget(holder, 0, index)
            self.inputs['collector'].append(edit)
        self._row_metrics.append((grid, 0, 90))
        body.addLayout(grid)
        body.addSpacing(self.px(10))
        body.addWidget(Divider(page))
        body.addSpacing(self.px(22))
        self.count_label = QLabel('已领取 0 个', page)
        self.count_label.setObjectName('countTitle')
        body.addWidget(self.count_label)
        body.addSpacing(self.px(10))
        self.next_label = QLabel('', page)
        self.next_label.setWordWrap(True)
        body.addWidget(self.next_label)
        self.remaining_label = QLabel('', page)
        self.remaining_label.setObjectName('muted')
        body.addWidget(self.remaining_label)
        body.addSpacing(self.px(10))
        self.advanced_button = self._button(page, '高级设置 ▾', self.toggle_advanced, 160)
        body.addWidget(self.advanced_button, 0, Qt.AlignmentFlag.AlignLeft)
        body.addSpacing(self.px(14))
        self.advanced_box = QWidget(page)
        advanced = QVBoxLayout(self.advanced_box)
        advanced.setContentsMargins(0, 0, 0, 0)
        advanced.setSpacing(0)
        advanced_grid = self._grid(self.advanced_box)
        for index, (title, key, unit) in enumerate([('间隙连点', 'idle', '次；0 关闭'),
                                                     ('连点间隔', 'idle_interval', '秒')]):
            holder, edit = self._field(self.advanced_box, key, title, unit)
            advanced_grid.addWidget(holder, 0, index)
            self.inputs['collector'].append(edit)
        self._row_metrics.append((advanced_grid, 0, 90))
        advanced.addLayout(advanced_grid)
        hint = QLabel('每轮检查后发送按键，与截图和领取依次进行。', self.advanced_box)
        hint.setObjectName('muted')
        hint.setWordWrap(True)
        advanced.addSpacing(self.px(8))
        advanced.addWidget(hint)
        self.advanced_box.setVisible(False)
        body.addWidget(self.advanced_box)
        self.advanced = False
        body.addSpacing(self.px(14))
        self.status_labels['collector'] = self._status_label(page, body)
        body.addSpacing(self.px(12))
        self.buttons['collector'] = self._button_row(body, [
            ('开始监测', lambda: self.run('collector'), 150, True),
            ('停止监测', lambda: self.stop('collector'), 130, False)])
        body.addSpacing(self.px(10))
        note = QLabel('按领取后图标变化计数，实际领取以桌宠内显示为准。', page)
        note.setObjectName('muted')
        note.setWordWrap(True)
        body.addWidget(note)
        body.addStretch(1)

    def _make_table(self, parent, columns):
        table = QTableWidget(0, len(columns), parent)
        table.setHorizontalHeaderLabels([column[1] for column in columns])
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(self.px(30))
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setShowGrid(False)
        table.setAlternatingRowColors(False)
        header = table.horizontalHeader()
        for index, (_, _, width) in enumerate(columns):
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.Interactive)
            table.setColumnWidth(index, self.px(width))
        header.setStretchLastSection(True)
        return table

    def _build_plan_page(self, page):
        body = self._page_head(page, '运行计划', '按顺序执行；停止或出错后取消后续步骤。')
        self.plan_table = self._make_table(page, [('index', '序号', 60),
                                                  ('kind', '任务', 100),
                                                  ('options', '参数', 550)])
        self._sized.append((self.plan_table, None, 220))
        self.plan_table.itemSelectionChanged.connect(self.refresh)
        body.addWidget(self.plan_table)
        body.addSpacing(self.px(16))
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(self.px(12))
        grid.setVerticalSpacing(self.px(10))
        self._metric_jobs.append(lambda: grid.setHorizontalSpacing(self.px(12)))
        actions = [('添加连点', lambda: self.add_plan_step('clicker')),
                   ('添加领取', lambda: self.add_plan_step('collector')),
                   ('编辑', self.edit_plan_step), ('上移', lambda: self.move_plan_step(-1)),
                   ('下移', lambda: self.move_plan_step(1)), ('删除', self.delete_plan_step),
                   ('清空', self.clear_plan), ('执行计划', self.start_plan),
                   ('停止计划', self.stop_plan)]
        self.plan_actions = {}
        for index, (text, command) in enumerate(actions):
            accent = text == '执行计划'
            button = self._button(page, text, command, 92, accent)
            grid.addWidget(button, index // 5, index % 5)
            self.plan_actions[text] = button
        grid.setColumnStretch(5, 1)
        body.addLayout(grid)
        body.addSpacing(self.px(14))
        self.plan_status = QLabel('按顺序执行；手动停止或出错后不再继续。', page)
        self.plan_status.setObjectName('muted')
        self.plan_status.setWordWrap(True)
        body.addWidget(self.plan_status)
        body.addStretch(1)

    def _build_history_page(self, page):
        body = self._page_head(page, '领取记录', '最近 20 次监测；记录在领取后及结束时保存。')
        self.history_table = self._make_table(page, [('start', '开始时间', 180),
                                                     ('end', '结束时间', 180),
                                                     ('count', '宝箱数', 70),
                                                     ('status', '状态', 130)])
        self._sized.append((self.history_table, None, 280))
        body.addWidget(self.history_table)
        body.addSpacing(self.px(16))
        self._button_row(body, [('刷新记录', self.refresh_history, 140, False)])
        body.addSpacing(self.px(14))
        hint = QLabel('“未记录结束”可能仍在运行，也可能关闭前未写入结束状态。\n'
                      '不会连续保存截图或无限累积记录。', page)
        hint.setObjectName('muted')
        hint.setWordWrap(True)
        body.addWidget(hint)
        body.addStretch(1)

    def _build_appearance_page(self, page):
        body = self._page_head(page, '外观设置', '即时预览；保存后恢复到下次启动。')
        preset_row = QHBoxLayout()
        preset_row.setSpacing(self.px(12))
        preset_label = QLabel('主题方案', page)
        preset_label.setObjectName('muted')
        preset_label.setFixedWidth(self.px(100))
        self.preset_combo = NoWheelComboBox(page)
        self.preset_combo.setFixedHeight(self.px(36))
        preset_row.addWidget(preset_label)
        preset_row.addWidget(self.preset_combo, 1)
        body.addLayout(preset_row)
        self._metric_jobs.append(lambda: preset_label.setFixedWidth(self.px(100)))
        body.addSpacing(self.px(12))
        buttons = self._button_row(body, [
            ('应用主题', self.apply_preset, 110, True),
            ('另存为主题', self.save_preset, 120, False),
            ('删除主题', self.delete_preset, 110, False)])
        self.preset_buttons = dict(apply=buttons[0], save=buttons[1], delete=buttons[2])
        body.addSpacing(self.px(10))
        preset_hint = QLabel('主题会保留壁纸。可通过「工具」分享主题、备份设置，或导入旧版设置。',
                             page)
        preset_hint.setObjectName('muted')
        preset_hint.setWordWrap(True)
        body.addWidget(preset_hint)
        body.addSpacing(self.px(18))
        body.addWidget(Divider(page))
        body.addSpacing(self.px(22))
        self._refresh_presets()
        rows = [('主题', 'theme', list(THEMES)), ('字号', 'font_size', list(FONT_SIZES)),
                ('布局密度', 'density', list(DENSITIES)),
                ('运行日志', 'log_expanded', ['收起', '展开']),
                ('任务栏图标', 'taskbar_icon', ['显示', '隐藏'])]
        for title, key, choices in rows:
            row = QHBoxLayout()
            row.setSpacing(self.px(12))
            label = QLabel(title, page)
            label.setObjectName('muted')
            label.setFixedWidth(self.px(100))
            combo = NoWheelComboBox(page)
            combo.addItems(choices)
            combo.setFixedHeight(self.px(36))
            combo.currentTextChanged.connect(lambda text: self._appearance_changed())
            if key == 'taskbar_icon':
                combo.setToolTip('只影响最小化：选“隐藏”就收进通知区域，任务栏不留按钮。')
            row.addWidget(label)
            row.addWidget(combo, 1)
            body.addLayout(row)
            body.addSpacing(self.px(15))
            self._appearance_widgets[key] = combo
            self._metric_jobs.append(lambda l=label: l.setFixedWidth(self.px(100)))

        for key, title in (('accent', '强调色'), ('panel_color', '面板底色')):
            label = QLabel(title, page)
            label.setObjectName('muted')
            body.addWidget(label)
            body.addSpacing(self.px(8))
            self._button_row(body, [('选择颜色', lambda k=key: self.choose_color(k), 110, False),
                                    ('跟随主题', lambda k=key: self.reset_color(k), 110, False)])
            body.addSpacing(self.px(16))

        body.addWidget(Divider(page))
        body.addSpacing(self.px(22))
        for title, key, low, high in (('面板不透明度', 'panel_opacity', 30, 100),
                                      ('整窗不透明度', 'window_opacity', 60, 100),
                                      ('背景亮度', 'background_brightness', 50, 150)):
            label = QLabel(title, page)
            body.addWidget(label)
            body.addSpacing(self.px(6))
            slider = NoWheelSlider(Qt.Orientation.Horizontal, page)
            slider.setRange(low, high)
            slider.valueChanged.connect(lambda value: self._appearance_changed())
            body.addWidget(slider)
            body.addSpacing(self.px(22))
            self._appearance_widgets[key] = slider
            self.slider_labels[key] = label

        hint = QLabel('100% 为不透明。面板设置只影响底色；整窗设置也影响文字。', page)
        hint.setObjectName('muted')
        hint.setWordWrap(True)
        body.addWidget(hint)
        body.addSpacing(self.px(18))
        body.addWidget(Divider(page))
        body.addSpacing(self.px(20))

        self.background_title = QLabel('背景图片', page)
        self.background_title.setStyleSheet('font-weight: bold;')
        body.addWidget(self.background_title)
        body.addSpacing(self.px(10))
        self._button_row(body, [('选择背景图片', self.choose_background, 160, False),
                                ('移除背景', self.remove_background, 120, False)])
        body.addSpacing(self.px(12))
        mode_row = QHBoxLayout()
        mode_row.setSpacing(self.px(12))
        mode_label = QLabel('显示方式', page)
        mode_label.setObjectName('muted')
        mode_label.setFixedWidth(self.px(100))
        mode = NoWheelComboBox(page)
        mode.addItems(list(BACKGROUND_MODES))
        mode.setFixedHeight(self.px(36))
        mode.currentTextChanged.connect(lambda text: self._appearance_changed())
        mode_row.addWidget(mode_label)
        mode_row.addWidget(mode, 1)
        body.addLayout(mode_row)
        self._appearance_widgets['background_mode'] = mode
        self._metric_jobs.append(lambda: mode_label.setFixedWidth(self.px(100)))
        body.addSpacing(self.px(18))
        body.addWidget(Divider(page))
        body.addSpacing(self.px(20))

        font_title = QLabel('字体', page)
        font_title.setStyleSheet('font-weight: bold;')
        body.addWidget(font_title)
        body.addSpacing(self.px(12))
        self.font_combo = NoWheelComboBox(page)
        self.font_combo.addItems(font_choices(self.fonts.available_families()))
        self.font_combo.setFixedHeight(self.px(36))
        self.font_combo.currentTextChanged.connect(lambda text: self._font_changed())
        body.addWidget(self.font_combo)
        body.addSpacing(self.px(14))
        self._button_row(body, [('加载本地字体', self.choose_font, 160, False),
                                ('使用已安装字体', self.clear_font, 165, False)])
        self.actual_font = QLabel('', page)
        self.actual_font.setObjectName('muted')
        self.actual_font.setWordWrap(True)
        body.addWidget(self.actual_font)
        self.font_hint_label = QLabel('', page)
        self.font_hint_label.setObjectName('muted')
        self.font_hint_label.setWordWrap(True)
        body.addWidget(self.font_hint_label)
        body.addSpacing(self.px(12))
        self.font_sample = QLabel('字体预览：桌宠助手 · 自动领取\n数字预览：114514', page)
        self.font_sample.setWordWrap(True)
        body.addWidget(self.font_sample)
        body.addSpacing(self.px(20))
        body.addWidget(Divider(page))
        body.addSpacing(self.px(20))

        self._button_row(body, [('保存外观', self.save_appearance, 125, True),
                                ('取消修改', self.cancel_appearance, 125, False)])
        body.addSpacing(self.px(12))
        self._button_row(body, [('恢复默认', self.reset_appearance, 125, False)])
        body.addStretch(1)

    # ------------------------------------------------------------------ 外观与样式

    def apply_style(self):
        self._prepare_metrics()
        self.setStyleSheet(app_qss(self.palette, self.family, self.point, self.scale))
        application = QApplication.instance()
        if application is not None:
            application.setFont(QFont(self.family, self.point))
        for panel in (self.side, self.content, self.footer):
            panel.set_palette(self.palette)
        collapsed = self.width() < self.px(820)
        for item in self.nav.values():
            item.set_style(self.palette, self.family, self.point, self.scale, collapsed)
        for button in (self.min_button, self.max_button, self.close_button):
            button.set_style(self.palette, self.scale)
        sample = QFont(self.family, self.point + 3)
        self.font_sample.setFont(sample)
        self.actual_font.setText('当前实际字体：' + font_display_name(self.family))
        self.font_hint_label.setText(self.font_hint)
        self.setWindowOpacity(self.options['window_opacity'])
        self._rebuild_wallpaper()
        self.layout_()
        if hasattr(self, 'plan_table'):
            self._refresh_plan_tree(self.selected_step())
        if hasattr(self, 'history_table'):
            self.refresh_history()

    def _rebuild_wallpaper(self):
        self._wallpaper_timer.stop()
        size = (self.width(), self.height())
        if size[0] < 2 or size[1] < 2:
            return
        ratio = self.devicePixelRatioF()
        key = (size, self.options['background_mode'],
               self.options['background_brightness'], id(self.backdrop.source), ratio)
        image = self.backdrop.render((round(size[0] * ratio), round(size[1] * ratio)),
                                     self.palette['background'],
                                     self.options['background_mode'],
                                     self.options['background_brightness'])
        self._wallpaper = ui_theme.to_pixmap(image, ratio)
        self._wallpaper_key = key
        self.update()

    def _appearance_changed(self):
        self._appearance_timer.start(90)

    def _font_changed(self):
        family = self.font_combo.currentText()
        if family:
            self.options['font_family'] = family
            self.options['font_file'] = ''
            self._font_key = (family, '')
        self._appearance_changed()

    def _read_appearance(self):
        options = dict(self.options)
        widgets = self._appearance_widgets
        options['theme'] = THEMES.get(widgets['theme'].currentText(), options['theme'])
        options['font_size'] = FONT_SIZES.get(widgets['font_size'].currentText(),
                                              options['font_size'])
        options['density'] = DENSITIES.get(widgets['density'].currentText(), options['density'])
        options['log_expanded'] = widgets['log_expanded'].currentText() == '展开'
        options['taskbar_icon'] = widgets['taskbar_icon'].currentText() == '显示'
        options['background_mode'] = BACKGROUND_MODES.get(
            widgets['background_mode'].currentText(), options['background_mode'])
        options['panel_opacity'] = widgets['panel_opacity'].value() / 100
        options['window_opacity'] = widgets['window_opacity'].value() / 100
        options['background_brightness'] = widgets['background_brightness'].value() / 100
        if options['theme'] != self.options['theme']:
            options['accent'] = options['panel_color'] = ''
        return options

    def preview_options(self):
        if self._appearance_timer.isActive():
            self._appearance_timer.stop()
        options = appearance.normalize_appearance(self._read_appearance())
        self.options = options
        self._log_expanded = options['log_expanded']
        font_key = (options['font_family'], options['font_file'])
        if font_key != self._font_key:
            self._font_key = font_key
            self.family = 'Microsoft YaHei UI'
            self.family, self.font_hint = self.fonts.preview(options['font_family'],
                                                             options['font_file'])
            if self.fonts.current:
                options['font_family'] = font_display_name(self.family)
        self.apply_style()
        self.sync_appearance_widgets()
        self.set_notice('外观预览中，尚未保存。')

    def sync_appearance_widgets(self):
        if not self._appearance_widgets:
            return
        options = self.options
        widgets = self._appearance_widgets
        values = dict(theme=next((name for name, key in THEMES.items()
                                  if key == options['theme']), '金色'),
                      font_size=next((name for name, key in FONT_SIZES.items()
                                      if key == options['font_size']), '标准'),
                      density=next((name for name, key in DENSITIES.items()
                                    if key == options['density']), '标准'),
                      log_expanded='展开' if options['log_expanded'] else '收起',
                      taskbar_icon='显示' if options['taskbar_icon'] else '隐藏',
                      background_mode='适应' if options['background_mode'] == 'contain' else '填充')
        for key, text in values.items():
            combo = widgets[key]
            combo.blockSignals(True)
            combo.setCurrentText(text)
            combo.blockSignals(False)
        for key in ('panel_opacity', 'window_opacity', 'background_brightness'):
            slider = widgets[key]
            slider.blockSignals(True)
            slider.setValue(round(options[key] * 100))
            slider.blockSignals(False)
        for key, title in (('panel_opacity', '面板不透明度'),
                           ('window_opacity', '整窗不透明度'),
                           ('background_brightness', '背景亮度')):
            self.slider_labels[key].setText(f'{title}   {round(options[key] * 100)}%')
        path = options['background']
        if path.startswith(f'{ui_theme.BACKGROUND_FOLDER}/{ui_theme.PRESET_FOLDER}/'):
            name = '主题壁纸'
        else:
            name = Path(path).name if path else '未选择（纯色）'
        self.background_title.setText('背景图片  ' + name)
        family = font_display_name(options['font_family'])
        self.font_combo.blockSignals(True)
        if self.font_combo.findText(family) < 0:
            self.font_combo.addItem(family)
        self.font_combo.setCurrentText(family)
        self.font_combo.blockSignals(False)
        self.actual_font.setText('当前实际字体：' + font_display_name(self.family))
        self.font_hint_label.setText(self.font_hint)

    def _refresh_presets(self, selected=None):
        combo = self.preset_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(list(self.presets))
        if selected and combo.findText(selected) >= 0:
            combo.setCurrentText(selected)
        combo.blockSignals(False)
        self.preset_buttons['apply'].setEnabled(bool(self.presets))
        self.preset_buttons['delete'].setEnabled(bool(self.presets))

    def check_for_updates(self):
        if self.closing or self._update_task is not None:
            return
        self.update_action.setEnabled(False)
        self.set_notice('正在检查 GitHub 更新…')
        self._update_stop.clear()
        self._update_task = self._update_executor.submit(
            updater.check_release, APP_VERSION, self._update_stop)

    def _poll_update(self):
        if self._update_task is None or not self._update_task.done():
            return
        task, self._update_task = self._update_task, None
        downloading = self._update_release is not None
        release, self._update_release = self._update_release, None
        self.update_action.setEnabled(not self.closing)
        try:
            result = task.result()
        except Exception as error:
            if not self.closing:
                self.error(f'更新失败：{error}')
            return
        if downloading:
            if self.closing or self.workers.active or self.plan.running:
                shutil.rmtree(result)
                if not self.closing:
                    self.error('请先停止任务，再检查并安装更新。')
                return
            exe = Path(sys.executable)
            try:
                updater.start_installer(result, exe, ROOT / 'update_install.ps1')
            except OSError as error:
                shutil.rmtree(result)
                self.error(f'无法启动更新器：{error}')
                return
            self.request_close()
            return
        if self.closing:
            return
        release = result
        if release is None:
            self.set_notice(f'当前已是最新版本 v{APP_VERSION}。')
            return
        exe = Path(sys.executable)
        if not updater.is_folder_build(exe, ROOT):
            self.error(f'发现 v{release.version}。请从 GitHub Releases 下载新版文件夹版。')
            return
        if self.workers.active or self.plan.running:
            self.error(f'发现 v{release.version}。请先停止任务，再检查并安装更新。')
            return
        message = (f'发现 v{release.version}，更新包约 {release.size / 1024**2:.1f} MiB。'
                   '\n下载校验后会自动重启助手，保留设置、主题和领取记录。')
        if release.notes:
            message += '\n\n' + release.notes
        dialog = QMessageBox(self)
        dialog.setWindowTitle('安装更新')
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(message)
        dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        dialog.setDefaultButton(QMessageBox.StandardButton.No)
        if dialog.exec() != QMessageBox.StandardButton.Yes:
            self.set_notice('已取消本次更新。')
            return
        self._update_stop.clear()
        self._update_release = release
        self.update_action.setEnabled(False)
        self.set_notice(f'正在下载并校验 v{release.version}…')
        self._update_task = self._update_executor.submit(
            updater.stage_release, release, exe.parent, self._update_stop)

    def _export_transfer(self, kind, data, name):
        file, _ = QFileDialog.getSaveFileName(self, '保存分享包', name, '分享包 (*.zip)')
        if not file:
            return
        path = Path(file)
        if path.suffix.lower() != '.zip':
            path = path.with_name(path.name + '.zip')
            if path.exists() and QMessageBox.question(
                    self, '覆盖分享包', f'要覆盖 {path.name} 吗？') != QMessageBox.StandardButton.Yes:
                return
        try:
            settings_transfer.export_bundle(path, ROOT, kind, data)
        except (OSError, ValueError) as error:
            self.error(f'导出失败：{error}')
            return
        self.set_notice(f'已导出 {path.name}。')

    def export_theme(self):
        current = '当前已保存外观'
        choices = {current: (current, self.saved_appearance)}
        choices.update({f'主题：{name}': (name, look) for name, look in self.presets.items()})
        selected = f'主题：{self.preset_combo.currentText()}'
        items = list(choices)
        choice, accepted = QInputDialog.getItem(
            self, '分享主题', '选择要分享的外观', items,
            items.index(selected) if selected in choices else 0, False)
        if accepted:
            name, look = choices[choice]
            self._export_transfer('theme', {'presets': {name: look}}, '桌宠助手-主题.zip')

    def export_settings(self):
        data = dict(self._business(), appearance=self.saved_appearance, presets=self.presets)
        self._export_transfer('settings', data, '桌宠助手-设置.zip')

    def import_settings(self):
        file, _ = QFileDialog.getOpenFileName(
            self, '导入主题或设置', '', '分享包或旧版设置 (*.zip *.json)')
        if not file:
            return
        try:
            transfer = settings_transfer.read_transfer(Path(file))
            if transfer.kind == 'theme':
                presets, selected = settings_transfer.merge_themes(self.presets, transfer.data['presets'])
                data = appearance.merge_settings(self.saved_settings, self._business(),
                                                 self.saved_appearance, self.window_settings(), presets)
                settings_transfer.install(self.settings_path, data, transfer.assets)
                self.saved_settings, self.presets = data, presets
                self._refresh_presets(selected)
                self.show_page('appearance')
                self.set_notice(f'已导入主题“{selected}”。点「应用主题」即可切换。')
                return

            if self.workers.active or self.plan.running:
                self.error('请先停止运行中的任务，再导入完整设置。')
                return
            message = '导入将替换当前运行参数、运行计划、外观和主题列表。继续吗？'
            if transfer.missing:
                message += f'\n旧版有 {transfer.missing} 处资源无法找到，将使用默认资源。'
            if QMessageBox.question(self, '导入完整设置', message) != QMessageBox.StandardButton.Yes:
                return
            incoming = transfer.data
            values = {}
            for key, default in DEFAULT_VALUES.items():
                value = incoming.get(key, default)
                values[key] = str(value) if type(value) in (str, int, float) else default
            plan = incoming.get('plan')
            steps = [] if not isinstance(plan, list) else [
                cleaned for step in plan if (cleaned := self._clean_step(step)) is not None]
            presets = incoming['presets']
            data = appearance.merge_settings(self.saved_settings, dict(values, plan=steps),
                                             incoming['appearance'], self.window_settings(), presets)
            settings_transfer.install(self.settings_path, data, transfer.assets)
        except (OSError, ValueError) as error:
            self.error(f'导入失败：{error}')
            return

        self.saved_settings, self.presets = data, presets
        self.saved_appearance = copy.deepcopy(data['appearance'])
        self.saved_image = self._load_background(self.saved_appearance['background'])
        self.values, self.plan_steps = values, steps
        for key, value in values.items():
            self.fields[key].setText(value)
        self._refresh_presets()
        self.cancel_appearance()
        self.fonts.commit()
        self.refresh()
        self.set_notice('已导入设置。' + ('部分旧版资源已使用默认项。' if transfer.missing else ''))

    def _write_presets(self):
        """Store the scheme list next to the appearance that is already saved."""
        data = appearance.merge_settings(self.saved_settings, self._business(),
                                         self.saved_appearance, self.window_settings(),
                                         self.presets)
        appearance.write_settings_atomic(self.settings_path, data)
        self.saved_settings = data

    def _preset_snapshot(self, name):
        """Copy the current look, taking a private copy of the wallpaper."""
        preset = copy.deepcopy(self.options)
        source = preset['background']
        folder = ui_theme.BACKGROUND_FOLDER + '/'
        if source and not Path(source).is_absolute() and source.startswith(folder):
            image = self.candidate if self.candidate_changed else self.saved_image
            if image is not None:
                # 壁纸在磁盘上只有一份，主题各存一份才不会互相覆盖。
                preset['background'] = ui_theme.preset_background(name)
                ui_theme.save_background(ROOT / preset['background'], image, lambda: None)
        return appearance.normalize_appearance(preset)

    def save_preset(self):
        if self._appearance_timer.isActive():
            self._appearance_timer.stop()
            self.preview_options()
        name, accepted = QInputDialog.getText(self, '另存为主题', '给这套外观起个名字：')
        if not accepted:
            return
        name = ' '.join(name.split())[:appearance.PRESET_NAME_LIMIT]
        if not name:
            self.error('主题名称不能为空。')
            return
        if name in self.presets:
            answer = QMessageBox.question(self, '覆盖主题', f'主题“{name}”已存在，要覆盖吗？')
            if answer != QMessageBox.StandardButton.Yes:
                return
        previous = self.presets.get(name)
        try:
            self.presets[name] = self._preset_snapshot(name)
            self._write_presets()
        except (OSError, ValueError) as error:
            if previous is None:
                self.presets.pop(name, None)
            else:
                self.presets[name] = previous
            self.error('主题未保存：' + str(error))
            return
        self._refresh_presets(selected=name)
        old_path = previous.get('background') if previous else None
        if (old_path and old_path != self.presets[name]['background']
                and old_path == ui_theme.preset_background(name)):
            # 覆盖成一个不用壁纸的方案时，旧的主题壁纸就没人引用了。
            try:
                (ROOT / old_path).unlink(missing_ok=True)
            except OSError:
                pass
        self.set_notice(f'已保存主题“{name}”；选中后点“应用主题”即可切回。')

    def apply_preset(self):
        name = self.preset_combo.currentText()
        preset = self.presets.get(name)
        if preset is None:
            self.error('请先在下拉框里选择要应用的主题。')
            return
        if self._appearance_timer.isActive():
            self._appearance_timer.stop()
        self.options = appearance.normalize_appearance(copy.deepcopy(preset))
        self.candidate = None
        self.candidate_changed = False
        self.saved_image = self._load_background(self.options['background'])
        self.backdrop.source = self.saved_image
        self.backdrop.invalidate()
        self._log_expanded = self.options['log_expanded']
        self.family, self.font_hint = self.fonts.preview(self.options['font_family'],
                                                         self.options['font_file'])
        self._font_key = (self.options['font_family'], self.options['font_file'])
        data = appearance.merge_settings(self.saved_settings, self._business(),
                                         self.options, self.window_settings(), self.presets)
        try:
            appearance.write_settings_atomic(self.settings_path, data)
        except (OSError, ValueError) as error:
            self.error('主题未能应用：' + str(error))
            return
        self.saved_appearance = copy.deepcopy(self.options)
        self.saved_settings = data
        self.fonts.commit()
        self.sync_appearance_widgets()
        self.apply_style()
        self.set_notice(f'已应用主题“{name}”。')

    def delete_preset(self):
        name = self.preset_combo.currentText()
        preset = self.presets.get(name)
        if preset is None:
            self.error('请先在下拉框里选择要删除的主题。')
            return
        answer = QMessageBox.question(self, '删除主题',
                                      f'确定删除主题“{name}”吗？当前外观不会改变。')
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.presets.pop(name)
        try:
            self._write_presets()
        except (OSError, ValueError) as error:
            self.presets[name] = preset
            self.error('删除主题失败：' + str(error))
            return
        try:
            if preset.get('background') == ui_theme.preset_background(name):
                (ROOT / preset['background']).unlink(missing_ok=True)
        except OSError:
            pass
        self._refresh_presets()
        self.set_notice(f'已删除主题“{name}”。')

    def choose_color(self, key):
        current = self.options[key] or self.palette['accent' if key == 'accent' else 'panel']
        color = QColorDialog.getColor(QColor(current), self, '选择颜色')
        if color.isValid():
            self.options[key] = color.name().lower()
            self._appearance_changed()

    def reset_color(self, key):
        self.options[key] = ''
        self._appearance_changed()

    def choose_background(self):
        path, _ = QFileDialog.getOpenFileName(
            self, '选择背景图片', str(ROOT),
            '图片文件 (*.png *.jpg *.jpeg *.webp);;所有文件 (*.*)')
        if not path:
            return
        try:
            image = ui_theme.import_background(path)
        except (OSError, ValueError) as error:
            self.error(f'无法使用这张图片作为背景：{error}')
            return
        self.candidate = image
        self.candidate_changed = True
        self.backdrop.source = image
        self.backdrop.invalidate()
        folder = ui_theme.BACKGROUND_FOLDER + '/'
        current = self.options['background']
        imported = current.startswith(settings_transfer.IMPORT_FOLDER + '/')
        if imported or not (current and not Path(current).is_absolute() and current.startswith(folder)):
            # 第一次换图才落到公共壁纸；编辑主题时直接替换主题自己的那一份。
            self.options['background'] = f'{ui_theme.BACKGROUND_FOLDER}/{ui_theme.BACKGROUND_NAME}'
        self._appearance_changed()

    def remove_background(self):
        self.candidate = None
        self.candidate_changed = True
        self.backdrop.source = None
        self.backdrop.invalidate()
        self.options['background'] = ''
        self._appearance_changed()

    def choose_font(self):
        path, _ = QFileDialog.getOpenFileName(
            self, '选择字体文件', str(ROOT), '字体文件 (*.ttf *.otf);;所有文件 (*.*)')
        if not path:
            return
        self.options['font_file'] = path
        self._font_key = (self.options['font_family'], path)
        self._appearance_changed()

    def clear_font(self):
        self.options['font_file'] = ''
        self._font_key = (self.options['font_family'], '')
        self._appearance_changed()

    def _background_target(self):
        """Write a new wallpaper into the scheme that is being edited."""
        path = self.options['background']
        folder = ui_theme.BACKGROUND_FOLDER + '/'
        if path and not Path(path).is_absolute() and path.startswith(folder):
            return ROOT / path
        return ROOT / ui_theme.BACKGROUND_FOLDER / ui_theme.BACKGROUND_NAME

    def save_appearance(self):
        if self._appearance_timer.isActive():
            self._appearance_timer.stop()
            self.preview_options()
        data = appearance.merge_settings(self.saved_settings, self._business(),
                                         self.options, self.window_settings(), self.presets)
        try:
            commit = lambda: appearance.write_settings_atomic(self.settings_path, data)
            if self.candidate_changed:
                ui_theme.save_background(self._background_target(), self.candidate, commit)
            else:
                commit()
        except (OSError, ValueError) as error:
            self.error('外观未保存：' + str(error))
            return
        self.saved_appearance = copy.deepcopy(self.options)
        self.saved_settings = data
        self.fonts.commit()
        if self.candidate_changed:
            self.saved_image = self.candidate
        self.candidate = None
        self.candidate_changed = False
        self.set_notice('外观已保存，下次启动自动恢复。')

    def cancel_appearance(self):
        if self._appearance_timer.isActive():
            self._appearance_timer.stop()
        self.family = 'Microsoft YaHei UI'
        self.fonts.cancel()
        self.options = copy.deepcopy(self.saved_appearance)
        self.candidate = None
        self.candidate_changed = False
        self.backdrop.source = self.saved_image
        self.backdrop.invalidate()
        self._log_expanded = self.options['log_expanded']
        self.family, self.font_hint = self.fonts.preview(self.options['font_family'],
                                                         self.options['font_file'])
        self._font_key = (self.options['font_family'], self.options['font_file'])
        self.sync_appearance_widgets()
        self.apply_style()
        self.set_notice('已取消未保存的外观修改。')

    def reset_appearance(self):
        self.options = appearance.normalize_appearance(None)
        self.candidate = None
        self.candidate_changed = True
        self.saved_image = self._load_background(self.options['background'])
        self.backdrop.source = self.saved_image
        self.backdrop.invalidate()
        self._log_expanded = False
        self.family, self.font_hint = self.fonts.preview(self.options['font_family'])
        self._font_key = (self.options['font_family'], self.options['font_file'])
        self.sync_appearance_widgets()
        self.apply_style()
        self.set_notice('已预览默认外观，点击保存后生效。')

    def toggle_log(self):
        self._log_expanded = not self._log_expanded
        self.options['log_expanded'] = self._log_expanded
        self.sync_appearance_widgets()
        if self.page != 'appearance':
            self.saved_appearance['log_expanded'] = self._log_expanded
            self._save_timer.start(300)
        self.layout_()

    def toggle_advanced(self):
        self.advanced = not self.advanced
        self.advanced_button.setText('高级设置 ▴' if self.advanced else '高级设置 ▾')
        self.advanced_box.setVisible(self.advanced)
        self.layout_()

    # ------------------------------------------------------------------ 页面布局

    def layout_(self):
        width, height = self.width(), self.height()
        if width < 50 or height < 50:
            return
        side = self.px(64) if width < self.px(820) else self.px(170)
        margin = self.px(20)
        collapsed = side < self.px(100)
        footer_height = self.px(94) if width < self.px(820) else self.px(58)
        content_x, content_y = side + margin, self.px(18)
        bottom = height - footer_height
        log_height = min(self.px(150), max(80, height // 4)) if self._log_expanded else 0
        content_width = max(120, width - content_x - margin)
        content_height = max(100, bottom - content_y - self.px(42) - log_height - self.px(12))

        self.side.setGeometry(0, 0, side, height)
        self.content.setGeometry(content_x, content_y, content_width, content_height)
        self.footer.setGeometry(0, bottom, width, footer_height)
        self.brand.setText('宠' if collapsed else '桌宠助手')
        self.brand.setGeometry(self.px(8), self.px(26), side - self.px(16), self.px(28))
        self.brand.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        collapsed = side < self.px(100)
        for index, (key, _) in enumerate(PAGES):
            item = self.nav[key]
            item.collapsed = collapsed
            item.set_style(self.palette, self.family, self.point, self.scale, collapsed)
            item.setGeometry(0, self.px(88 + index * 57), side, self.px(46))
        self.tools_button.setText('⋯' if collapsed else '工具 ▾')
        self.tools_button.setGeometry(self.px(8), bottom - self.px(52),
                                      side - self.px(16), self.px(36))

        inset = self.px(12)
        self.scroll.setGeometry(inset, self.px(8), content_width - inset * 2,
                                content_height - self.px(16))
        button_width = self.close_button.width()
        self.close_button.move(content_width - self.px(12) - button_width, self.px(10))
        self.max_button.move(self.close_button.x() - button_width - self.px(2),
                             self.px(10))
        self.min_button.move(self.max_button.x() - button_width - self.px(2), self.px(10))
        for button in (self.min_button, self.max_button, self.close_button):
            button.raise_()

        self.log_toggle.setText('收起日志 ▴' if self._log_expanded else '运行日志 ▾')
        self.log_toggle.setGeometry(content_x, content_y + content_height + self.px(7),
                                    self.px(112), self.px(29))
        self.latest.setGeometry(content_x + self.px(122),
                                content_y + content_height + self.px(7),
                                max(20, content_width - self.px(122)), self.px(30))
        self.log.setGeometry(content_x, bottom - log_height,
                             content_width - self.px(12), log_height - self.px(5))
        self.log.setVisible(bool(self._log_expanded))
        if self._log_expanded:
            self.log.raise_()

        if width < self.px(820):
            self.footer_status.setGeometry(self.px(12), self.px(4),
                                           width - self.px(24), self.px(30))
            self.stop_all.setGeometry(width - self.px(150), self.px(43),
                                      self.px(135), self.px(36))
            self.shortcut.setGeometry(self.px(12), self.px(48), self.px(120), self.px(28))
            self.version_label.setGeometry(self.px(142), self.px(48), self.px(80), self.px(28))
        else:
            self.footer_status.setGeometry(self.px(16), self.px(12),
                                           max(130, width - self.px(360)), self.px(30))
            self.version_label.setGeometry(width - self.px(335), self.px(13),
                                           self.px(80), self.px(28))
            self.stop_all.setGeometry(width - self.px(240), self.px(9),
                                      self.px(120), self.px(36))
            self.shortcut.setGeometry(width - self.px(110), self.px(13),
                                      self.px(100), self.px(28))
        self._apply_metrics()

    def _apply_metrics(self):
        for job in self._metric_jobs:
            try:
                job()
            except RuntimeError:
                pass
        for widget, width, height in self._sized:
            if width is not None:
                widget.setFixedWidth(self.px(width))
            if height is not None:
                widget.setFixedHeight(self.px(height))
        for grid, row, base in self._row_metrics:
            grid.setRowMinimumHeight(row, self.px(base * self.spacing))
        self.plan_table.verticalHeader().setDefaultSectionSize(self.px(30))
        self.history_table.verticalHeader().setDefaultSectionSize(self.px(30))

    def show_page(self, key):
        if key not in self.page_widgets:
            return
        self.page = key
        self.nav[key].setChecked(True)
        if self.scroll.widget() is not self.page_widgets[key]:
            self.scroll.takeWidget()
            self.scroll.setWidget(self.page_widgets[key])
        if key == 'history':
            self.refresh_history()
        if key == 'plan':
            self._refresh_plan_tree(self.selected_step())
        self.refresh()
        self.layout_()

    # ------------------------------------------------------------------ 绘制与窗口

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        radius = 0 if self.maximized else self.px(8)
        rect = QRectF(self.rect())
        path = ui_theme.rounded_path(rect, radius)
        painter.setClipPath(path)
        if self._wallpaper is None:
            painter.fillRect(self.rect(), QColor(self.palette['background']))
        elif self._wallpaper_key and self._wallpaper_key[0] == (self.width(), self.height()):
            painter.drawPixmap(0, 0, self._wallpaper)
        else:
            painter.drawPixmap(self.rect(), self._wallpaper)
        painter.setClipping(False)
        painter.setPen(QPen(QColor(255, 255, 255, 34), 1))
        painter.drawPath(ui_theme.rounded_path(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius))
        if not self.maximized:
            painter.setPen(QPen(QColor(self.palette['muted']), 1))
            right, bottom = self.width() - 9, self.height() - 9
            for step in range(3):
                offset = step * 4
                painter.drawLine(QPointF(right - 12 + offset, bottom),
                                 QPointF(right, bottom - 12 + offset))
        painter.end()

    def showEvent(self, event):
        super().showEvent(event)
        # 每次显示都确认在任务栏里；从通知区域恢复后按钮也要回来。
        ui_theme.taskbar(int(self.winId()))
        if self._wallpaper is None:
            self._rebuild_wallpaper()

    def start_fade(self):
        """Grow the opacity in a few steps so the wallpaper does not pop in."""
        self._fade = QPropertyAnimation(self, b'windowOpacity', self)
        self._fade.setDuration(360)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(self.options['window_opacity'])
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.finished.connect(
            lambda: self.setWindowOpacity(self.options['window_opacity']))
        self._fade.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self.maximized and self._resizing is None and self.isVisible():
            if self.width() >= MIN_WIDTH and self.height() >= MIN_HEIGHT:
                self._normal_geometry = QRect(self.pos(), self.size())
        self.layout_()
        if self._resizing is not None:
            self._wallpaper_timer.start(140)
        else:
            self._rebuild_wallpaper()

    def moveEvent(self, event):
        super().moveEvent(event)
        if not self.maximized and self._resizing is None and self.isVisible():
            if self.width() >= MIN_WIDTH and self.height() >= MIN_HEIGHT:
                self._normal_geometry = QRect(self.pos(), self.size())

    def _edge_at(self, pos):
        if self.maximized:
            return ''
        width, height = self.width(), self.height()
        if width < 2 * RESIZE_EDGE or height < 2 * RESIZE_EDGE:
            return ''
        side = ('w' if pos.x() <= RESIZE_EDGE
                else 'e' if pos.x() >= width - RESIZE_EDGE else '')
        end = ('n' if pos.y() <= RESIZE_EDGE
               else 's' if pos.y() >= height - RESIZE_EDGE else '')
        return end + side

    def _update_cursor(self, pos):
        cursor = RESIZE_CURSORS.get(self._edge_at(pos))
        if cursor == self._cursor:
            return
        self._cursor = cursor
        if cursor is None:
            self.unsetCursor()
        else:
            self.setCursor(cursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            edge = self._edge_at(event.position().toPoint())
            if edge:
                self._resizing = (edge, event.globalPosition().toPoint(), self.geometry())
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resizing is not None:
            edge, start, geometry = self._resizing
            delta = event.globalPosition().toPoint() - start
            left, top = geometry.left(), geometry.top()
            right, bottom = geometry.right() + 1, geometry.bottom() + 1
            if 'w' in edge:
                left = min(left + delta.x(), right - MIN_WIDTH)
            if 'n' in edge:
                top = min(top + delta.y(), bottom - MIN_HEIGHT)
            if 'e' in edge:
                right = max(right + delta.x(), left + MIN_WIDTH)
            if 's' in edge:
                bottom = max(bottom + delta.y(), top + MIN_HEIGHT)
            self.setGeometry(left, top, right - left, bottom - top)
            event.accept()
            return
        self._update_cursor(event.position().toPoint())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._resizing is not None:
            self._resizing = None
            self._rebuild_wallpaper()
            self._save_timer.start(400)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        self._cursor = None
        self.unsetCursor()
        super().leaveEvent(event)

    def toggle_max(self):
        if self.closing:
            return
        if self.maximized:
            self.maximized = False
            self._wallpaper = None
            self.setGeometry(self._normal_geometry)
        else:
            self._normal_geometry = QRect(self.pos(), self.size())
            screen = self.screen() or QGuiApplication.primaryScreen()
            area = screen.availableGeometry()
            self.maximized = True
            self._wallpaper = None
            self.setGeometry(area)
        self.max_button.maximized = self.maximized
        self.max_button.setToolTip('还原' if self.maximized else '最大化')
        self.max_button.update()
        self._rebuild_wallpaper()
        self.layout_()
        self.update()

    def hide_to_tray(self):
        if self.closing:
            return
        if self._tray is None or self.options.get('taskbar_icon', True):
            # 没有通知区域，或用户选择最小化后保留任务栏图标。
            self.showMinimized()
            return
        self.hide()
        if not self._tray_hint:
            self._tray_hint = True
            self._tray.showMessage('桌宠助手', '已收进通知区域，单击图标恢复窗口。',
                                   QSystemTrayIcon.MessageIcon.Information, 3000)

    def restore_window(self):
        if self.closing:
            return
        if not self.isVisible():
            self.show()
            self.setWindowOpacity(self.options['window_opacity'])
        elif self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        event.ignore()
        self.request_close()

    def request_close(self):
        if self.closing:
            return
        self.closing = True
        self._update_stop.set()
        if self._fade is not None:
            self._fade.stop()
            self._fade = None
        for timer in (self._appearance_timer, self._wallpaper_timer, self._save_timer):
            if timer.isActive():
                timer.stop()
        self.stop_everything()
        self.set_notice('正在安全停止任务，完成后关闭窗口…')
        self.refresh()

    # ------------------------------------------------------------------ 运行计划

    @staticmethod
    def _clean_step(step):
        if not isinstance(step, dict) or not isinstance(step.get('kind'), str) or step['kind'] not in PLAN_FIELDS:
            return None
        candidate = {'kind': step['kind']}
        for key in PLAN_FIELDS[step['kind']]:
            value = step.get(key)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                candidate[key] = str(value)
        try:
            step_options(candidate)
        except (ValueError, KeyError, TypeError):
            return None
        return candidate

    def selected_step(self):
        row = self.plan_table.currentRow()
        return row if 0 <= row < len(self.plan_steps) else None

    def _refresh_plan_tree(self, select=None):
        if not hasattr(self, 'plan_table'):
            return
        self.plan_table.blockSignals(True)
        self.plan_table.setRowCount(0)
        for index, step in enumerate(self.plan_steps):
            try:
                detail = step_summary(step)
            except (ValueError, KeyError, TypeError):
                detail = '参数无效'
            self.plan_table.insertRow(index)
            for column, value in enumerate((index + 1, NAMES[step['kind']], detail)):
                item = QTableWidgetItem(str(value))
                item.setForeground(QColor(self.palette['text']))
                if column == 0:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                else:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignLeft
                                          | Qt.AlignmentFlag.AlignVCenter)
                self.plan_table.setItem(index, column, item)
        self.plan_table.blockSignals(False)
        if select is not None and 0 <= select < len(self.plan_steps):
            self.plan_table.selectRow(select)
            item = self.plan_table.item(select, 0)
            if item is not None:
                self.plan_table.scrollToItem(item)

    def _ask_step(self, kind, step=None):
        defaults = {key: self.values.get(key, '') for key in DEFAULT_VALUES}
        if step is not None:
            defaults['idle'], defaults['idle_interval'] = '0', '0.1'
        dialog = QDialog(self)
        dialog.setWindowTitle(('编辑' if step else '添加') + NAMES[kind] + '步骤')
        dialog.setModal(True)
        body = QGridLayout(dialog)
        body.setContentsMargins(20, 20, 20, 20)
        body.setHorizontalSpacing(12)
        body.setVerticalSpacing(12)
        variables = {}
        for row, (label, key, unit) in enumerate(STEP_FIELDS[kind]):
            field_label = QLabel(label, dialog)
            field_label.setObjectName('muted')
            body.addWidget(field_label, row, 0)
            value = (step or {}).get(key, defaults.get(key, ''))
            edit = QLineEdit(str(value), dialog)
            edit.setMinimumWidth(180)
            body.addWidget(edit, row, 1)
            unit_label = QLabel(unit, dialog)
            unit_label.setObjectName('muted')
            body.addWidget(unit_label, row, 2)
            variables[key] = edit
        hint = QLabel(STEP_HINTS[kind], dialog)
        hint.setObjectName('hint')
        hint.setWordWrap(True)
        hint.setMaximumWidth(380)
        body.addWidget(hint, len(variables), 0, 1, 3)
        result = {}

        def confirm():
            candidate = {'kind': kind, **{key: edit.text()
                                          for key, edit in variables.items()}}
            try:
                step_options(candidate)
            except (ValueError, KeyError, TypeError) as error:
                QMessageBox.warning(dialog, '参数错误', str(error))
                return
            result.update(candidate)
            dialog.accept()

        cancel = QPushButton('取消', dialog)
        cancel.clicked.connect(dialog.reject)
        confirm_button = QPushButton('确定', dialog)
        confirm_button.setObjectName('accent')
        confirm_button.clicked.connect(confirm)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(confirm_button)
        body.addLayout(buttons, len(variables) + 1, 0, 1, 3)
        dialog.exec()
        return result or None

    def add_plan_step(self, kind):
        if self.closing or self.plan.running:
            return
        step = self._ask_step(kind)
        if step is None:
            return
        self.plan_steps.append(step)
        self.save()
        self._refresh_plan_tree(len(self.plan_steps) - 1)
        self.plan_status.setText(f'已添加第 {len(self.plan_steps)} 步。')
        self.refresh()

    def edit_plan_step(self):
        index = self.selected_step()
        if index is None or self.plan.running:
            return
        step = self._ask_step(self.plan_steps[index]['kind'], self.plan_steps[index])
        if step is None:
            return
        self.plan_steps[index] = step
        self.save()
        self._refresh_plan_tree(index)
        self.refresh()

    def move_plan_step(self, delta):
        index = self.selected_step()
        if index is None or self.plan.running:
            return
        target = index + delta
        if not 0 <= target < len(self.plan_steps):
            return
        self.plan_steps[index], self.plan_steps[target] = \
            self.plan_steps[target], self.plan_steps[index]
        self.save()
        self._refresh_plan_tree(target)
        self.refresh()

    def delete_plan_step(self):
        index = self.selected_step()
        if index is None or self.plan.running:
            return
        del self.plan_steps[index]
        self.save()
        select = min(index, len(self.plan_steps) - 1) if self.plan_steps else None
        self._refresh_plan_tree(select)
        self.refresh()

    def clear_plan(self):
        if self.plan.running or not self.plan_steps:
            return
        self.plan_steps.clear()
        self.save()
        self._refresh_plan_tree()
        self.plan_status.setText('计划已清空。')
        self.refresh()

    # ------------------------------------------------------------------ 任务控制

    def job_options(self, kind):
        """Worker arguments for one single feature.

        Deliberately not named ``options``: that attribute holds the appearance
        dict, and a method of the same name would be shadowed by it.
        """
        return step_options(dict(self.values, kind=kind)) if kind in PLAN_FIELDS else []


    def _gap_clicks(self):
        try:
            return int(self.values['idle'].strip() or '0')
        except ValueError:
            return 0

    def run(self, kind, options=None):
        if self.closing:
            return
        if self.plan.running:
            self.error('计划正在运行；请先停止计划，或等待计划完成。')
            return
        try:
            options = self.job_options(kind) if options is None else options
            if (kind == 'collector' and self._gap_clicks()
                    and self.workers.running('clicker')):
                raise ValueError('连点器正在运行；请先停止它，再启动带间隙连点的监测，'
                                 '避免重复发送按键。')
            if (kind == 'clicker' and self.workers.running('collector')
                    and self.workers.gap_clicks('collector')):
                raise ValueError('监测正在执行间隙连点；无需再启动连点器，避免重复发送按键。')
            self.workers.start(kind, options)
            self.save()
            if kind in self.status_labels:
                self.set_status(kind, '运行中')
            if kind == 'collector':
                worker = self.workers.active.get('collector')
                if worker is not None and worker.ui_run_id:
                    self.state.begin(worker.ui_run_id)
            self.set_notice(f'{NAMES[kind]}已启动。')
            self.append(NAMES[kind], '已启动。')
        except (ValueError, OSError) as error:
            self.error(str(error))
        except Exception:
            # 槽函数里的异常只会打到 stderr，界面上看起来像按钮没反应。
            self.error(f'{NAMES.get(kind, kind)}启动失败，原因见运行记录。')
            self.append('助手', traceback.format_exc().strip().splitlines()[-1])
        self.refresh()
        self._refresh_live()

    def start_both(self):
        if self.closing:
            return
        if self.plan.running:
            self.error('计划正在运行；请先停止计划，或等待计划完成。')
            return
        started = []
        try:
            if self._gap_clicks() or (self.workers.running('collector')
                                      and self.workers.gap_clicks('collector')):
                raise ValueError('间隙连点已启用；请只启动监测，'
                                 '避免连点器与监测重复发送按键。')
            plans = [(kind, self.job_options(kind)) for kind in ('clicker', 'collector')
                     if not self.workers.running(kind)]
            if self.workers.running('test'):
                raise ValueError('请等待单次领取测试结束，再同时启动。')
            for kind, options in plans:
                self.workers.start(kind, options)
                started.append(kind)
                self.set_status(kind, '运行中')
                self.append(NAMES[kind], '已启动。')
            worker = self.workers.active.get('collector')
            if worker is not None and worker.ui_run_id:
                self.state.begin(worker.ui_run_id)
            self.save()
            self.set_notice('连点与宝箱监测同时运行；任一任务结束不会停止另一项。')
        except (ValueError, OSError) as error:
            for kind in started:
                self.workers.stop(kind)
            self.error(str(error))
        except Exception:
            for kind in started:
                self.workers.stop(kind)
            self.error('一键启动失败，原因见运行记录。')
            self.append('助手', traceback.format_exc().strip().splitlines()[-1])
        self.refresh()

    def plan_entries(self):
        entries = []
        for number, step in enumerate(self.plan_steps, 1):
            try:
                entries.append((step['kind'], plan_options(step)))
            except (ValueError, KeyError, TypeError) as error:
                raise ValueError(f'计划第 {number} 步参数无效：{error}') from None
        if not entries:
            raise ValueError('计划中还没有任务。')
        return entries

    def start_plan(self):
        if self.closing or self.plan.running:
            return
        if self.workers.running('test'):
            self.error('请等待单次领取测试结束，再执行计划。')
            return
        if any(self.workers.running(kind) for kind in ('clicker', 'collector')):
            self.error('请先停止正在运行的连点或领取任务，再执行计划。')
            return
        try:
            entries = self.plan_entries()
            self.plan.start(entries)
        except (ValueError, OSError) as error:
            self.error(str(error))
            self.refresh()
            return
        self.save()
        first = self.plan.current()[0]
        self.set_status(first, '运行中')
        self.plan_status.setText(f'计划运行中：第 1/{len(entries)} 步（{NAMES[first]}）。')
        self.append('计划', f'第 1 步已启动：{NAMES[first]}。')
        self.set_notice('计划已启动；上一步正常结束后会自动开始下一步。')
        if first == 'collector':
            worker = self.workers.active.get('collector')
            if worker is not None and worker.ui_run_id:
                self.state.begin(worker.ui_run_id)
        self.refresh()

    def stop_plan(self):
        step = self.plan.current()
        was_running = self.plan.running
        self.plan.abort()
        if step is not None and self.workers.running(step[0]):
            self.workers.stop(step[0])
            self.set_status(step[0], '正在停止…')
            self.append(NAMES[step[0]], '已请求停止，等待当前操作安全结束。')
        if was_running:
            self.plan_status.setText('计划已停止，不再启动后续步骤。')
            self.append('计划', '已停止，后续步骤未执行。')
            self.set_notice('计划已停止，后续步骤不会自动开始。')
        self.refresh()

    def _plan_advance(self, kind, code):
        number = self.plan.position + 1
        try:
            outcome = self.plan.advance(kind, code)
        except (ValueError, OSError) as error:
            self.plan.abort()
            self.plan_status.setText('计划启动下一步失败。')
            self.error(f'计划第 {number + 1} 步启动失败：{error}')
            return
        if outcome == 'started':
            next_kind = self.plan.current()[0]
            self.set_status(next_kind, '运行中')
            self.plan_status.setText(
                f'计划运行中：第 {self.plan.position + 1}/{len(self.plan.steps)} 步'
                f'（{NAMES[next_kind]}）。')
            self.append('计划', f'第 {self.plan.position + 1} 步已启动：{NAMES[next_kind]}。')
            if next_kind == 'collector':
                worker = self.workers.active.get('collector')
                if worker is not None and worker.ui_run_id:
                    self.state.begin(worker.ui_run_id)
        elif outcome == 'done':
            self.plan_status.setText('计划已全部完成。')
            self.append('计划', '全部步骤已完成。')
            self.set_notice('计划已全部完成。')
        elif outcome == 'stopped':
            if code == 3:
                self.plan_status.setText(f'计划已停止：第 {number} 步被手动停止。')
                self.append('计划', f'第 {number} 步被手动停止，后续步骤未执行。')
            else:
                self.plan_status.setText(f'计划已停止：第 {number} 步异常结束（{code}）。')
                self.append('计划', f'第 {number} 步异常结束（{code}），后续步骤未执行。')
            self.set_notice('计划已停止，请查看运行记录。')

    def stop(self, kind):
        current = self.plan.current()
        if self.plan.running and current is not None and current[0] == kind:
            self.plan.abort()
            self.plan_status.setText('计划已停止，不再启动后续步骤。')
            self.append('计划', '当前步骤被停止，后续步骤未执行。')
        self.workers.stop(kind)
        if self.workers.running(kind):
            self.set_status(kind, '正在停止…')
            self.append(NAMES[kind], '已请求停止，等待当前操作安全结束。')

    def stop_everything(self):
        if self.plan.running:
            self.plan.abort()
            self.plan_status.setText('计划已停止，不再启动后续步骤。')
            self.append('计划', '已停止，后续步骤未执行。')
        for kind in tuple(self.workers.active):
            self.stop(kind)
        self.set_notice('已请求全部停止。')

    def check_environment(self):
        if self.plan.running:
            self.error('计划正在运行；请先停止计划，或等待计划完成。')
            return
        self.run('check_clicker')
        self.run('check_collector')

    def open_diagnostics(self):
        folder = COLLECTOR / 'diagnostics'
        folder.mkdir(exist_ok=True)
        try:
            os.startfile(str(folder))
        except OSError as error:
            self.error(str(error))

    def show_preview(self):
        pixmap = QPixmap(str(COLLECTOR / 'preview.png'))
        if pixmap.isNull():
            self.error('无法打开预览：预览图片还没有生成。')
            return
        screen = QGuiApplication.primaryScreen().availableGeometry()
        if (pixmap.width() > screen.width() - 100
                or pixmap.height() > screen.height() - 180):
            pixmap = pixmap.scaled(screen.width() - 100, screen.height() - 180,
                                   Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
        dialog = QDialog(self)
        dialog.setWindowTitle('宝箱识别预览')
        body = QVBoxLayout(dialog)
        body.setContentsMargins(12, 12, 12, 12)
        body.setSpacing(8)
        image = QLabel(dialog)
        image.setPixmap(pixmap)
        body.addWidget(image)
        note = QLabel('绿色：识别成功且属于桌宠；红色：识别成功但属于其他窗口。'
                      '预览不会点击。', dialog)
        note.setObjectName('muted')
        note.setWordWrap(True)
        body.addWidget(note)
        dialog.exec()

    # ------------------------------------------------------------------ 记录与轮询

    def set_notice(self, text):
        self.latest.setText(text)

    def set_status(self, kind, text):
        self._status[kind] = text
        label = self.status_labels.get(kind)
        if label is not None:
            label.setText(text)
        self._refresh_live()

    def error(self, message):
        self.set_notice(message)
        self.append('提示', message)

    def append(self, name, line):
        self.log.appendPlainText(f'{datetime.now():%H:%M:%S}  [{name}] {line}')

    def show_last_summary(self):
        rows = read_claim_history()
        if rows:
            text = '上次记录：' + claim_summary(rows[-1])
            self.set_notice(text)
            self.append('领取记录', text)

    def refresh_history(self, *, rows=None):
        if not hasattr(self, 'history_table'):
            return
        if rows is None:
            rows = read_claim_history()
        table = self.history_table
        table.setRowCount(len(rows))
        for index, row in enumerate(reversed(rows)):
            values = (row['started_at'][:19].replace('T', ' '),
                      row['finished_at'][:19].replace('T', ' ') or '—',
                      row['count'], CLAIM_STATUS[row['status']])
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setForeground(QColor(self.palette['text']))
                item.setTextAlignment(Qt.AlignmentFlag.AlignLeft
                                      | Qt.AlignmentFlag.AlignVCenter
                                      if column != 2 else Qt.AlignmentFlag.AlignCenter)
                table.setItem(index, column, item)

    def _refresh_live(self):
        snap = self.state.snapshot()
        count_label = getattr(self, 'count_label', None)
        if count_label is not None:
            count_label.setText(f"已领取 {snap['count']} 个")
            self.next_label.setText(snap['next_text'])
            self.remaining_label.setText(snap['remaining_text'])
        labels = []
        for kind, title in (('clicker', '连点'), ('collector', '监测')):
            text = '运行中' if self.workers.running(kind) else self._status[kind]
            labels.append(f'{title}：{text}')
        self.footer_status.setText('   ·   '.join(labels))
        if hasattr(self, 'plan_status') and self.plan.running:
            self.plan_status.setText(
                f'计划运行中：第 {self.plan.position + 1}/{len(self.plan.steps)} 步'
                f'（{NAMES[self.plan.current()[0]]}）。')

    def refresh(self):
        plan_running = self.plan.running
        for kind, buttons in self.buttons.items():
            start, stop = buttons
            active = self.workers.running(kind)
            start.setEnabled(not active and not self.closing and not plan_running)
            stop.setEnabled(active)
            for entry in self.inputs[kind]:
                entry.setEnabled(not active)
        self.stop_all.setEnabled(bool(self.workers.active))
        index = self.selected_step() if hasattr(self, 'plan_table') else None
        editable = not self.closing and not plan_running
        states = {'添加连点': editable, '添加领取': editable,
                  '编辑': editable and index is not None,
                  '删除': editable and index is not None,
                  '上移': editable and index is not None and index > 0,
                  '下移': editable and index is not None
                          and index < len(self.plan_steps) - 1,
                  '清空': editable and bool(self.plan_steps),
                  '执行计划': editable and bool(self.plan_steps),
                  '停止计划': plan_running}
        for name, button in self.plan_actions.items():
            button.setEnabled(states.get(name, editable))
        self.tools_menu.actions()[0].setEnabled(not self.closing and not plan_running)

    def tick(self):
        self._poll_update()
        while True:
            try:
                kind, line = self.workers.messages.get_nowait()
            except queue.Empty:
                break
            if kind == 'collector' and self.state.consume(line):
                continue
            self.append(NAMES[kind], line)
            if kind == 'clicker' and '已发送：' in line:
                self.set_status('clicker', line)
        for kind, code in self.workers.finished():
            if code == 3:
                self.set_status(kind, '已停止')
            else:
                self.set_status(kind, '已结束' if code == 0
                                else '运行失败，请查看记录')
            if kind == 'collector':
                self.state.unexpected_exit()
                rows = read_claim_history()
                self.state.recover(rows)
                if rows:
                    self.set_notice('上次记录：' + claim_summary(rows[-1]))
                if self.page == 'history':
                    self.refresh_history(rows=rows)
            if code == 3:
                self.append(NAMES[kind], '已按停止请求结束。')
            else:
                self.append(NAMES[kind], '已结束。' if code == 0
                            else f'异常结束（{code}），请查看上方记录。')
            if kind == 'preview' and code == 0 and not self.closing:
                self.show_preview()
            if self.plan.running:
                self._plan_advance(kind, code)
        active = bool(self.workers.active)
        if active != self.awake:
            self.awake = active
            keep_awake(active)
        if self.closing and not self.workers.active and self._update_task is None:
            self._update_executor.shutdown(wait=False)
            self._tick_timer.stop()
            if self._save_timer.isActive():
                self._save_timer.stop()
            self.save()
            self.fonts.close()
            if self._tray is not None:
                self._tray.hide()
            QApplication.exit(0)
            return
        self.refresh()
        self._refresh_live()

    # ------------------------------------------------------------------ 通知区域

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.restore_window()

    def _build_tray(self):
        icon_path = ROOT / '图标.ico'
        icon = QIcon(str(icon_path)) if icon_path.is_file() else QIcon()
        if not icon.isNull():
            self.setWindowIcon(icon)
            QApplication.setWindowIcon(icon)
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.append('助手', '通知区域图标不可用，请用任务栏按钮恢复窗口。')
            return
        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip('桌宠助手：单击打开窗口')
        menu = QMenu()
        menu.addAction('显示窗口', self.restore_window)
        menu.addSeparator()
        menu.addAction('退出桌宠助手', self.request_close)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()
        self._tray_hint = False


def run_job(kind, args):
    """Frozen child entry: run one bundled script with the arguments it expects."""
    script, extra = SCRIPTS[kind]
    sys.argv = [str(script), *extra, *args]
    sys.path.insert(0, str(script.parent))
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True, write_through=True)
        except (AttributeError, ValueError):
            pass
    try:
        runpy.run_path(str(script), run_name='__main__')
    except SystemExit:
        raise
    except Exception:
        # 子任务没有窗口：把错误交给父进程日志，绝不弹框或开主界面。
        try:
            traceback.print_exc(file=sys.stderr)
        except Exception:
            pass
        raise SystemExit(1)
    return 0


def main():
    args = sys.argv[1:]
    if args[:1] == ['--job']:
        if len(args) < 2 or args[1] not in SCRIPTS:
            print('未知的后台任务：' + ' '.join(args[1:2]), file=sys.stderr)
            return 2
        return run_job(args[1], args[2:])
    if args == ['--check']:
        print('Desktop pet assistant launcher check passed.')
        return 0
    app = QApplication(sys.argv)
    app.setApplicationName('桌宠助手')
    app.setApplicationVersion(APP_VERSION)
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow()
    window.show()
    window.start_fade()
    if token := os.environ.pop('WUWATOPPA_UPDATE_READY', ''):
        QTimer.singleShot(0, lambda: updater.signal_ready(token))
    if os.environ.pop('WUWATOPPA_UPDATE_FAILED', ''):
        window.error('更新失败，已返回旧版。详情请查看程序同级更新临时目录的 install.log。')
    return app.exec()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        trace = traceback.format_exc()
        (ROOT / '启动错误.log').write_text(trace, encoding='utf-8')
        try:
            app = QApplication.instance() or QApplication(sys.argv)
            QMessageBox.critical(None, '桌宠助手',
                                 '界面启动失败，详细原因已保存到程序数据目录的“启动错误.log”。')
        except Exception:
            pass
