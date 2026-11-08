"""Global Windows keyboard macro. Prompts for settings; Ctrl+Alt stops it."""

import argparse
import ctypes
from ctypes import wintypes
from pathlib import Path
import math
import sys
import time


DEFAULT_COUNT = 2000
DEFAULT_INTERVAL = 0.1
DEFAULT_HOURS = 0.0
MIN_COUNT, MAX_COUNT = 0, 100000
MIN_INTERVAL, MAX_INTERVAL = 0.0001, 60.0
MIN_HOURS, MAX_HOURS = 0.0, 1000.0


def parse_count(value):
    message = f"次数必须为 {MIN_COUNT}～{MAX_COUNT} 的整数（0 表示按运行时长）。"
    try:
        count = int(value)
    except (ValueError, TypeError):
        raise ValueError(message) from None
    if not MIN_COUNT <= count <= MAX_COUNT:
        raise ValueError(message)
    return count


def parse_interval(value):
    message = f"间隔必须为 {MIN_INTERVAL:g}～{MAX_INTERVAL:g} 秒的有限数值。"
    try:
        interval = float(value)
    except (ValueError, TypeError):
        raise ValueError(message) from None
    if not math.isfinite(interval) or not MIN_INTERVAL <= interval <= MAX_INTERVAL:
        raise ValueError(message)
    return interval


def parse_hours(value):
    message = f"运行时长必须为 {MIN_HOURS:g}～{MAX_HOURS:g} 小时的有限数值。"
    try:
        hours = float(value)
    except (ValueError, TypeError):
        raise ValueError(message) from None
    if not math.isfinite(hours) or not MIN_HOURS <= hours <= MAX_HOURS:
        raise ValueError(message)
    return hours


def prompt_parameters(count=None, interval=None, hours=None):
    fields = (
        (count, DEFAULT_COUNT, parse_count,
         f"请输入次数（{MIN_COUNT}～{MAX_COUNT} 整数，0 表示只按运行时长；回车默认 {DEFAULT_COUNT}；q 取消）："),
        (interval, DEFAULT_INTERVAL, parse_interval,
         f"请输入间隔秒数（{MIN_INTERVAL:g}～{MAX_INTERVAL:g}；回车默认 {DEFAULT_INTERVAL:g}；q 取消）："),
        (hours, DEFAULT_HOURS, parse_hours,
         f"请输入运行时长小时数（0～{MAX_HOURS:g}，0 表示只按次数；回车默认 {DEFAULT_HOURS:g}；q 取消）："),
    )
    values = []
    for supplied, default, parse, question in fields:
        if supplied is not None:
            values.append(parse(str(supplied)))
            continue
        while True:
            try:
                text = input(question).strip()
            except EOFError:
                print("\n输入已关闭，已取消启动。")
                return None
            if text.lower() == 'q':
                print("已取消启动。")
                return None
            try:
                values.append(parse(text if text else str(default)))
                break
            except ValueError as error:
                print(error)
    return tuple(values)


def run_macro(count, interval, send_press, stopped, sleep, progress=None,
              hours=0, clock=None, reason=None):
    """Send presses until the count or the duration is reached; 0 means unlimited."""
    clock = clock if clock is not None else time.monotonic
    deadline = clock() + hours * 3600 if hours else None
    completed = 0

    def record(value):
        if reason is not None:
            reason['value'] = value

    while count == 0 or completed < count:
        if stopped():
            record('stopped')
            break
        if deadline is not None and clock() >= deadline:
            record('hours')
            break
        send_press()
        completed += 1
        if progress is not None:
            progress(completed)
        if count and completed == count:
            record('count')
            break
        remaining = interval
        while remaining > 0:
            if stopped():
                record('stopped')
                return completed
            if deadline is not None and clock() >= deadline:
                record('hours')
                return completed
            step = min(0.02, remaining)
            sleep(step)
            remaining -= step
    return completed


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD),
    ]


class INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", wintypes.DWORD), ("data", INPUTUNION)]


class WindowsKeyboard:
    def __init__(self):
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user32.GetAsyncKeyState.restype = wintypes.SHORT
        self.user32.SendInput.argtypes = [
            wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int,
        ]
        self.user32.SendInput.restype = wintypes.UINT
        expected_size = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        if ctypes.sizeof(INPUT) != expected_size:
            raise RuntimeError("Windows INPUT 结构大小不正确。")

    def pressed(self, vk):
        return bool(self.user32.GetAsyncKeyState(vk) & 0x8000)

    def stop_pressed(self):
        return self.pressed(0x11) and self.pressed(0x12)

    def send_f15(self):
        events = (INPUT * 2)()
        for event in events:
            event.type = 1  # INPUT_KEYBOARD
            event.ki.wVk = 0x7E  # F15
        events[1].ki.dwFlags = 0x0002  # KEYEVENTF_KEYUP
        sent = self.user32.SendInput(2, events, ctypes.sizeof(INPUT))
        if sent != 2:
            error = ctypes.get_last_error()
            # A partially accepted batch may contain the key-down only.
            if sent == 1:
                self.user32.SendInput(1, ctypes.byref(events[1]), ctypes.sizeof(INPUT))
            raise OSError(error, "Windows 未能完整发送按键，宏已停止。")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="填写次数、间隔和运行时长后自动运行；Ctrl+Alt 停止；全局发送 F15 按键。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--count", type=parse_count,
                        help=f"按键次数（{MIN_COUNT}～{MAX_COUNT}；未指定时询问）")
    parser.add_argument("--interval", type=parse_interval,
                        help=f"间隔秒数（{MIN_INTERVAL:g}～{MAX_INTERVAL:g}；未指定时询问）")
    parser.add_argument("--hours", type=parse_hours,
                        help=f"运行时长小时数（{MIN_HOURS:g}～{MAX_HOURS:g}，0 表示只按次数；未指定时询问）")
    parser.add_argument("--check", action="store_true", help="仅检查环境，不发送任何按键")
    parser.add_argument("--stop-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--plan", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("此工具适用于 Windows。")
    if args.check:
        WindowsKeyboard()
        print("环境检查通过：Windows 接口及 INPUT 结构正常。未发送任何按键。")
        return 0

    settings = prompt_parameters(args.count, args.interval, args.hours)
    if settings is None:
        return 0
    args.count, args.interval, args.hours = settings
    if args.count == 0 and args.hours == 0:
        parser.error("次数与运行时长至少填写一项，避免无限运行。")
    keyboard = WindowsKeyboard()
    count_text = f"发送 {args.count} 次" if args.count else "不限次数"
    hours_text = f"{args.hours:g} 小时" if args.hours else "不限时长"
    print(f"目标：{count_text}，{hours_text}；间隔 {args.interval} 秒。")
    print("保持桌宠运行，无需切到桌宠窗口。")
    print("已自动开始，按 Ctrl+Alt 停止。全局按键模式，切换窗口不会停止。", flush=True)

    def progress(completed):
        if completed % 50 == 0 or completed == args.count:
            print(f"\r已发送：{completed}/{args.count}", end="", flush=True)

    reason = {}
    completed = run_macro(
        args.count, args.interval, keyboard.send_f15,
        lambda: keyboard.stop_pressed() or
                (args.stop_file is not None and args.stop_file.exists()), time.sleep, progress,
        args.hours, reason=reason,
    )
    finished = reason.get('value') in ('count', 'hours')
    print(f"\n{'已完成' if finished else '已停止'}：发送 {completed} 次模拟按键。")
    print("请查看桌宠内的实际操作计数；软件是否记录模拟输入由它自身决定。")
    if args.plan and reason.get('value') == 'stopped':
        print("计划模式：已被停止，后续步骤不会自动开始。")
        return 3
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已停止。")
        sys.exit(0)
    except (OSError, RuntimeError) as error:
        print(f"\n错误：{error}")
        sys.exit(1)
