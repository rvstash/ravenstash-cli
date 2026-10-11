"""Rich-based terminal output helpers for rvs.

Messages, table cells, and titles are rendered as plain text: server-supplied
strings can never inject Rich markup, and terminal control characters are
replaced before anything reaches the console.
"""

import json
import re
from typing import NoReturn

from rich.console import Console
from rich.table import Table
from rich.text import Text

from .account.handles import typed_handle


console = Console()
err_console = Console(stderr=True)
_json_enabled = False
# C0 and C1 controls other than tab and newline, including ESC and DEL.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def plain(value: object) -> str:
    """Return *value* as text with terminal control characters made visible-safe."""
    return _CONTROL_CHARACTERS.sub("\ufffd", str(value))


def _text(value: object, style: str | None = None) -> Text:
    return Text(plain(value), style=style or "")


def _message(label: str, style: str, msg: str) -> Text:
    return Text.assemble((label, style), " ", plain(msg))


def set_json(enabled: bool) -> None:
    global _json_enabled
    _json_enabled = enabled


def is_json() -> bool:
    return _json_enabled


def _emit_json(payload: dict, *, err: bool = False) -> None:
    target = err_console if err else console
    target.print(
        json.dumps(payload, default=str),
        markup=False,
        highlight=False,
        soft_wrap=True,
    )


def value(value: str, *, key: str = "value") -> None:
    if _json_enabled:
        _emit_json({key: value})
    else:
        console.print(value, markup=False, highlight=False, soft_wrap=True)


def success(msg: str) -> None:
    if _json_enabled:
        _emit_json({"level": "success", "message": msg})
        return
    console.print(_message("OK", "bold green", msg))


def info(msg: str) -> None:
    if _json_enabled:
        _emit_json({"level": "info", "message": msg})
        return
    console.print(_message("->", "cyan", msg))


def warn(msg: str) -> None:
    if _json_enabled:
        _emit_json({"level": "warning", "message": msg}, err=True)
        return
    err_console.print(_message("!", "bold yellow", msg))


def error(msg: str) -> None:
    if _json_enabled:
        _emit_json({"level": "error", "message": msg}, err=True)
        return
    err_console.print(_message("Error:", "bold red", msg))


def fatal(msg: str) -> NoReturn:
    if _json_enabled:
        _emit_json({"level": "error", "message": msg}, err=True)
        raise SystemExit(1)
    err_console.print(_message("Error:", "bold red", msg))
    raise SystemExit(1)


def table(
    columns: list[str],
    rows: list[list[str]],
    title: str | None = None,
    *,
    json_keys: list[str] | None = None,
) -> None:
    if _json_enabled:
        _emit_json(
            {
                "title": title,
                "items": [dict(zip(json_keys or columns, row, strict=True)) for row in rows],
            }
        )
        return
    t = Table(
        title=_text(title) if title is not None else None,
        show_header=True,
        header_style="bold dim",
    )
    for col in columns:
        t.add_column(_text(col))
    for row in rows:
        t.add_row(*(_text(cell) for cell in row))
    console.print(t)


def section(title: str) -> None:
    if _json_enabled:
        _emit_json({"section": title})
        return
    console.print()
    console.print(_text(title, "bold"))


def kv(
    pairs: dict[str, str | None],
    title: str | None = None,
    *,
    json_keys: list[str] | None = None,
    json_values: dict[str, object] | None = None,
) -> None:
    """Print key/value rows; ``json_values`` replaces the rows in JSON output."""
    if _json_enabled:
        if json_values is None:
            json_values = (
                dict(zip(json_keys, pairs.values(), strict=True)) if json_keys else dict(pairs)
            )
        _emit_json({"title": title, "values": json_values})
        return
    t = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    t.add_column("key", style="bold dim", no_wrap=True)
    t.add_column("value")
    for k, v in pairs.items():
        t.add_row(_text(k), _text(v) if v else Text("-", style="dim"))
    if title:
        console.print(_text(title, "bold"))
    console.print(t)


def resource_account_hint(selector: str, selected_account_ref: str, owner: dict) -> None:
    """Report explicit cross-account access without changing CLI context."""
    owner_id = str(owner["ref"])
    account_type = str(owner.get("type") or "account")
    try:
        label = typed_handle(account_type, owner.get("handle") or owner_id)
    except ValueError:
        label = f"account:{owner_id}"
    message = (
        f"Resource {selector} belongs to another account: {label} ({account_type}, {owner_id}). "
        "The selected account is unchanged; resource operations use the owning account."
    )
    if _json_enabled:
        _emit_json(
            {
                "level": "info",
                "event": "cross_account_resource",
                "message": message,
                "selector": selector,
                "selected_account_ref": selected_account_ref,
                "owner_account_ref": owner_id,
                "owner_account": label,
            },
            err=True,
        )
    else:
        info(message)
