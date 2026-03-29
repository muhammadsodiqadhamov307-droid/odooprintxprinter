#!/usr/bin/env python3
"""
Native host for the visual template editor.

Opens the single-file HTML editor in a native webview window and exchanges
template data through a temporary JSON session file.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path


_SESSION_STORE = None
_WEBVIEW_WINDOW = None


class SessionStore:
    def __init__(self, path: str):
        self._path = Path(path)

    def read(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def write(self, payload: dict) -> None:
        self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class TemplateEditorApi:
    def save(self, payload: dict | None = None):
        global _SESSION_STORE, _WEBVIEW_WINDOW
        session = _SESSION_STORE.read()
        payload = payload or {}
        templates = payload.get("templates")
        if isinstance(templates, dict):
            session["templates"] = templates
        ticket_type = payload.get("ticket_type")
        if ticket_type:
            session["ticket_type"] = str(ticket_type)
        session["saved"] = True
        session.pop("error", None)
        _SESSION_STORE.write(session)
        if _WEBVIEW_WINDOW is not None:
            _WEBVIEW_WINDOW.destroy()
        return {"ok": True}

    def cancel(self):
        global _WEBVIEW_WINDOW
        if _WEBVIEW_WINDOW is not None:
            _WEBVIEW_WINDOW.destroy()
        return {"ok": True}


def build_bootstrap_html(editor_path: Path, bootstrap: dict) -> str:
    html = editor_path.read_text(encoding="utf-8")
    bootstrap_script = (
        "<script>"
        f"window.__TEMPLATE_EDITOR_BOOTSTRAP__ = {json.dumps(bootstrap, ensure_ascii=False)};"
        "</script>"
    )
    if "</head>" in html:
        return html.replace("</head>", f"{bootstrap_script}\n</head>", 1)
    return f"{bootstrap_script}\n{html}"


def resolve_editor_html() -> Path:
    base_dir = Path(__file__).resolve().parent
    return base_dir / "deploy" / "windows" / "assets" / "receipt_template_editor.html"


def main() -> int:
    global _SESSION_STORE, _WEBVIEW_WINDOW
    parser = argparse.ArgumentParser()
    parser.add_argument("session_path")
    args = parser.parse_args()

    _SESSION_STORE = SessionStore(args.session_path)
    session = _SESSION_STORE.read()
    session.setdefault("saved", False)
    session.setdefault("templates", {})
    session.setdefault("ticket_type", "receipt")
    _SESSION_STORE.write(session)

    editor_html = resolve_editor_html()
    if not editor_html.exists():
        session["error"] = f"Editor HTML not found: {editor_html}"
        _SESSION_STORE.write(session)
        print(session["error"], file=sys.stderr)
        return 2

    try:
        import webview
    except Exception as exc:  # noqa: BLE001
        session["error"] = (
            "pywebview is not installed or could not be loaded.\n"
            "Install requirements and try again.\n"
            f"Details: {exc}"
        )
        _SESSION_STORE.write(session)
        print(session["error"], file=sys.stderr)
        return 3

    try:
        html = build_bootstrap_html(editor_html, session)
        api = TemplateEditorApi()
        _WEBVIEW_WINDOW = webview.create_window(
            "Visual Template Editor",
            html=html,
            js_api=api,
            width=1480,
            height=960,
            min_size=(1180, 760),
            text_select=True,
            zoomable=True,
        )
        webview.start(debug=False)
        return 0
    except Exception:  # noqa: BLE001
        session = _SESSION_STORE.read()
        session["error"] = traceback.format_exc()
        _SESSION_STORE.write(session)
        print(session["error"], file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
