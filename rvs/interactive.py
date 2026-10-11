"""Small cross-platform interactive terminal selectors."""

import os
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING

from rich.live import Live

from . import output


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from typing import TextIO


KEY_UP = "up"
KEY_DOWN = "down"
KEY_ENTER = "enter"
KEY_CTRL_C = "ctrl-c"
_ESCAPE_SEQUENCE_TIMEOUT_SECONDS = 0.25
_ESCAPE_SEQUENCE_MAX_CHARS = 8


@contextmanager
def _raw_terminal() -> Iterator[None]:
    if sys.platform == "win32":
        yield
        return

    try:
        import termios
        import tty
    except ImportError:
        yield
        return

    try:
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        tty.setcbreak(fd)
    except OSError, termios.error:
        yield
        return

    try:
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _read_selector_key() -> str:
    if sys.platform == "win32":
        import msvcrt

        key = msvcrt.getwch()
        if key in {"\x00", "\xe0"}:
            code = msvcrt.getwch()
            if code == "H":
                return KEY_UP
            if code == "P":
                return KEY_DOWN
        if key in {"\n", "\r"}:
            return KEY_ENTER
        if key == "\x03":
            return KEY_CTRL_C
        return key

    key = _read_stdin_char()
    if key == "\x03":
        return KEY_CTRL_C
    if key in {"\n", "\r"}:
        return KEY_ENTER
    if key == "\x1b":
        return _selector_key_from_escape_sequence(_read_escape_sequence(key))
    return key


def _read_escape_sequence(first_char: str) -> str:
    import select

    sequence = first_char
    while len(sequence) < _ESCAPE_SEQUENCE_MAX_CHARS:
        if not select.select([_stdin_selector()], [], [], _ESCAPE_SEQUENCE_TIMEOUT_SECONDS)[0]:
            break
        char = _read_stdin_char()
        if not char:
            break
        sequence += char
        if sequence == "\x1bO":
            continue
        if char.isalpha() or char == "~":
            break
    return sequence


def _stdin_fileno() -> int | None:
    try:
        return sys.stdin.fileno()
    except AttributeError, OSError:
        return None


def _stdin_selector() -> int | TextIO:
    fd = _stdin_fileno()
    return fd if fd is not None else sys.stdin


def _read_stdin_char() -> str:
    fd = _stdin_fileno()
    if fd is None:
        return sys.stdin.read(1)
    try:
        return os.read(fd, 1).decode("utf-8", errors="ignore")
    except OSError:
        return sys.stdin.read(1)


def _selector_key_from_escape_sequence(sequence: str) -> str:
    if sequence in {"\x1b[A", "\x1bOA"}:
        return KEY_UP
    if sequence in {"\x1b[B", "\x1bOB"}:
        return KEY_DOWN
    if sequence.startswith("\x1b[") and sequence.endswith("A"):
        return KEY_UP
    if sequence.startswith("\x1b[") and sequence.endswith("B"):
        return KEY_DOWN
    return sequence


def select_index(
    *,
    item_count: int,
    initial_index: int,
    render: Callable[[int], str],
    unavailable_message: str,
) -> int:
    """Select an item index with arrow keys and Enter."""
    if item_count < 1:
        raise ValueError("An interactive selector requires at least one item.")
    if not sys.stdin.isatty() or not output.console.is_terminal or output.is_json():
        output.fatal(unavailable_message)

    selected_index = initial_index
    with (
        _raw_terminal(),
        Live(
            render(selected_index),
            console=output.console,
            refresh_per_second=10,
            transient=True,
        ) as live,
    ):
        while True:
            key = _read_selector_key()
            if key == KEY_CTRL_C:
                raise KeyboardInterrupt
            if key == KEY_UP:
                selected_index = (selected_index - 1) % item_count
            elif key == KEY_DOWN:
                selected_index = (selected_index + 1) % item_count
            elif key == KEY_ENTER:
                return selected_index
            live.update(render(selected_index))
