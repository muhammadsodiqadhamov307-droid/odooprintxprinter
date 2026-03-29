#!/usr/bin/env python3
"""
Local desktop manager for Odoo Print Agent.
Controls print_agent.py through local HTTP API (127.0.0.1:8899).
"""

import json
import os
import subprocess
import sys
import tempfile
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from urllib.error import URLError
from urllib.request import Request, urlopen


API_BASE = "http://127.0.0.1:8899"
SERVICE_NAME = "OdooPrintAgent"


def default_templates():
    return {
        "receipt": {
            "elements": [
                {"field": "company_name", "align": "center", "style": "double", "col": 0},
                {"field": "order_name_line", "align": "center", "style": "normal", "col": 0},
                {"field": "date_line", "align": "center", "style": "normal", "col": 0},
                {"field": "cashier_line", "align": "center", "style": "normal", "col": 0},
                {"field": "table_guests_line", "align": "center", "style": "normal", "col": 0},
                {"field": "tracking_number", "align": "center", "style": "double", "col": 0},
                {"field": "separator", "align": "left", "style": "normal", "col": 0},
                {"field": "lines_block", "align": "left", "style": "normal", "col": 0},
                {"field": "separator", "align": "left", "style": "normal", "col": 0},
                {"field": "subtotal_line", "align": "left", "style": "normal", "col": 0},
                {"field": "tax_line", "align": "left", "style": "normal", "col": 0},
                {"field": "total_line", "align": "left", "style": "bold", "col": 0},
                {"field": "payments_block", "align": "left", "style": "normal", "col": 0},
            ]
        },
        "kitchen": {
            "elements": [
                {"field": "table_big", "align": "center", "style": "double", "col": 0},
                {"field": "table_circle", "align": "center", "style": "normal", "col": 0},
                {"field": "ticket_title", "align": "center", "style": "normal", "col": 0},
                {"field": "printer_line", "align": "left", "style": "normal", "col": 0},
                {"field": "table_line", "align": "left", "style": "normal", "col": 0},
                {"field": "order_line", "align": "left", "style": "normal", "col": 0},
                {"field": "time_line", "align": "left", "style": "normal", "col": 0},
                {"field": "waiter_line", "align": "left", "style": "normal", "col": 0},
                {"field": "separator", "align": "left", "style": "normal", "col": 0},
                {"field": "items_block", "align": "left", "style": "normal", "col": 0},
                {"field": "separator", "align": "left", "style": "normal", "col": 0},
            ]
        },
    }


TEMPLATE_FIELDS = {
    "receipt": [
        "company_name",
        "order_name_line",
        "date_line",
        "cashier_line",
        "table_guests_line",
        "tracking_number",
        "separator",
        "lines_block",
        "subtotal_line",
        "tax_line",
        "total_line",
        "payments_block",
        "static_text",
        "blank",
    ],
    "kitchen": [
        "table_big",
        "table_circle",
        "ticket_title",
        "printer_line",
        "table_line",
        "order_line",
        "time_line",
        "waiter_line",
        "separator",
        "items_block",
        "static_text",
        "blank",
    ],
}


def http_get(path):
    req = Request(f"{API_BASE}{path}", method="GET")
    with urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post(path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = Request(
        f"{API_BASE}{path}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


class TemplateElementDialog(tk.Toplevel):
    def __init__(self, parent, ticket_type, initial=None):
        super().__init__(parent)
        self.title("Template Element")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result = None
        initial = initial or {}

        fields = TEMPLATE_FIELDS.get(ticket_type, [])
        self.var_field = tk.StringVar(value=initial.get("field", fields[0] if fields else "blank"))
        self.var_align = tk.StringVar(value=initial.get("align", "left"))
        self.var_style = tk.StringVar(value=initial.get("style", "normal"))
        self.var_col = tk.StringVar(value=str(initial.get("col", 0)))
        self.var_text = tk.StringVar(value=initial.get("text", ""))

        pad = {"padx": 8, "pady": 5}
        ttk.Label(self, text="Field").grid(row=0, column=0, sticky="w", **pad)
        field_combo = ttk.Combobox(self, textvariable=self.var_field, values=fields, width=26, state="readonly")
        field_combo.grid(
            row=0, column=1, sticky="w", **pad
        )
        field_combo.bind("<<ComboboxSelected>>", lambda _e: self._update_text_state())

        ttk.Label(self, text="Align").grid(row=1, column=0, sticky="w", **pad)
        ttk.Combobox(self, textvariable=self.var_align, values=["left", "center", "right"], width=26, state="readonly").grid(
            row=1, column=1, sticky="w", **pad
        )

        ttk.Label(self, text="Style").grid(row=2, column=0, sticky="w", **pad)
        ttk.Combobox(self, textvariable=self.var_style, values=["normal", "bold", "double", "huge"], width=26, state="readonly").grid(
            row=2, column=1, sticky="w", **pad
        )

        ttk.Label(self, text="Left Offset (col)").grid(row=3, column=0, sticky="w", **pad)
        ttk.Entry(self, textvariable=self.var_col, width=28).grid(row=3, column=1, sticky="w", **pad)

        ttk.Label(self, text="Custom Text").grid(row=4, column=0, sticky="nw", **pad)
        self.entry_text = tk.Text(self, width=28, height=4, wrap="word")
        self.entry_text.grid(row=4, column=1, sticky="w", **pad)
        if self.var_text.get():
            self.entry_text.insert("1.0", self.var_text.get())

        btns = ttk.Frame(self)
        btns.grid(row=5, column=0, columnspan=2, sticky="e", padx=8, pady=10)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Save", command=self._save).pack(side=tk.RIGHT, padx=4)

        self._update_text_state()
        self.wait_window(self)

    def _update_text_state(self):
        is_static_text = self.var_field.get().strip() == "static_text"
        self.entry_text.configure(state=(tk.NORMAL if is_static_text else tk.DISABLED))

    def _save(self):
        try:
            col = int(self.var_col.get().strip() or "0")
        except ValueError:
            messagebox.showwarning("Validation", "Left offset must be a number.", parent=self)
            return
        text_value = self.entry_text.get("1.0", tk.END).strip() if self.var_field.get().strip() == "static_text" else ""
        self.result = {
            "field": self.var_field.get().strip(),
            "align": self.var_align.get().strip() or "left",
            "style": self.var_style.get().strip() or "normal",
            "col": max(0, col),
        }
        if self.result["field"] == "static_text":
            self.result["text"] = text_value
        self.destroy()


class TemplateEditorWindow(tk.Toplevel):
    def __init__(self, parent, templates):
        super().__init__(parent)
        self.title("Template Editor (Text Grid)")
        self.geometry("900x520")
        self.transient(parent)
        self.grab_set()
        self.templates = json.loads(json.dumps(templates or default_templates()))
        self.ticket_type = tk.StringVar(value="receipt")
        self.preview = tk.StringVar(value="")
        self.drag_index = None
        self.saved = False

        self._build_ui()
        self._load_list()
        self.wait_window(self)

    def _build_ui(self):
        root = ttk.Frame(self, padding=10)
        root.pack(fill=tk.BOTH, expand=True)

        top = ttk.Frame(root)
        top.pack(fill=tk.X)
        ttk.Label(top, text="Template Type").pack(side=tk.LEFT)
        cmb = ttk.Combobox(top, textvariable=self.ticket_type, values=["receipt", "kitchen"], width=14, state="readonly")
        cmb.pack(side=tk.LEFT, padx=8)
        cmb.bind("<<ComboboxSelected>>", lambda _e: self._load_list())

        ttk.Label(
            root,
            text="Drag lines up/down to reorder. Use Add/Edit to choose field, align, style and left offset.",
        ).pack(fill=tk.X, pady=(8, 4))

        middle = ttk.Frame(root)
        middle.pack(fill=tk.BOTH, expand=True)

        self.listbox = tk.Listbox(middle, height=18)
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.listbox.bind("<ButtonPress-1>", self._on_drag_start)
        self.listbox.bind("<B1-Motion>", self._on_drag_motion)

        tools = ttk.Frame(middle)
        tools.pack(side=tk.LEFT, fill=tk.Y, padx=8)
        ttk.Button(tools, text="Add", width=16, command=self._add).pack(pady=2)
        ttk.Button(tools, text="Edit", width=16, command=self._edit).pack(pady=2)
        ttk.Button(tools, text="Delete", width=16, command=self._delete).pack(pady=2)
        ttk.Button(tools, text="Move Up", width=16, command=lambda: self._move(-1)).pack(pady=2)
        ttk.Button(tools, text="Move Down", width=16, command=lambda: self._move(1)).pack(pady=2)
        ttk.Button(tools, text="Reset Default", width=16, command=self._reset_default).pack(pady=12)

        preview_frame = ttk.LabelFrame(root, text="Preview (Approximate)")
        preview_frame.pack(fill=tk.BOTH, expand=False, pady=(8, 0))
        preview_text = tk.Text(preview_frame, height=10, wrap="none")
        preview_text.pack(fill=tk.BOTH, expand=True)
        preview_text.configure(state=tk.DISABLED)
        self.preview_text = preview_text

        bottom = ttk.Frame(root)
        bottom.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=4)
        ttk.Button(bottom, text="Save", command=self._save).pack(side=tk.RIGHT, padx=4)

    def _elements(self):
        t = self.ticket_type.get()
        self.templates.setdefault(t, {"elements": []})
        self.templates[t].setdefault("elements", [])
        return self.templates[t]["elements"]

    def _format_elem(self, idx, elem):
        field = elem.get("field", "")
        align = elem.get("align", "left")
        style = elem.get("style", "normal")
        col = elem.get("col", 0)
        text_suffix = ""
        if field == "static_text":
            text_preview = str(elem.get("text", "") or "").strip().replace("\n", " ")
            if len(text_preview) > 18:
                text_preview = text_preview[:18] + "..."
            text_suffix = f" | text={text_preview}"
        return f"{idx+1:02d}. {field:18} | align={align:6} | style={style:6} | col={col}{text_suffix}"

    def _preview_label(self, field):
        sample = {
            "company_name": "My Company",
            "order_name_line": "Ticket 000123",
            "date_line": "2026-03-28 19:12:00",
            "cashier_line": "Served by: Administrator",
            "table_guests_line": "Table: 2  Guests: 4",
            "tracking_number": "1003",
            "separator": "-" * 42,
            "lines_block": "1 Club Sandwich           6.80 so'm",
            "subtotal_line": "Subtotal                  13.40 so'm",
            "tax_line": "Tax                        2.01 so'm",
            "total_line": "Total                     15.41 so'm",
            "payments_block": "Card                      15.41 so'm",
            "table_big": "TABLE 2",
            "table_circle": "(2)",
            "ticket_title": "** KITCHEN ORDER **",
            "printer_line": "Printer : Kitchen",
            "table_line": "Table   : 2",
            "order_line": "Order   : 3006",
            "time_line": "Time    : 19:12:51",
            "waiter_line": "Waiter  : Administrator",
            "items_block": " -2  Coca-Cola",
            "static_text": "Custom note here",
            "blank": "",
        }
        return sample.get(field, field)

    def _refresh_preview(self):
        lines = []
        for elem in self._elements():
            label = str(elem.get("text", "") or "") if elem.get("field") == "static_text" else self._preview_label(elem.get("field"))
            try:
                col = max(0, int(elem.get("col", 0)))
            except (TypeError, ValueError):
                col = 0
            align = elem.get("align", "left")
            if align == "center":
                text = label.center(42)
            elif align == "right":
                text = label.rjust(42)
            else:
                text = (" " * col) + label
            lines.append(text[:42])
        self.preview_text.configure(state=tk.NORMAL)
        self.preview_text.delete("1.0", tk.END)
        self.preview_text.insert("1.0", "\n".join(lines))
        self.preview_text.configure(state=tk.DISABLED)

    def _load_list(self):
        self.listbox.delete(0, tk.END)
        for idx, elem in enumerate(self._elements()):
            self.listbox.insert(tk.END, self._format_elem(idx, elem))
        self._refresh_preview()

    def _selected_index(self):
        sel = self.listbox.curselection()
        return sel[0] if sel else None

    def _add(self):
        dlg = TemplateElementDialog(self, self.ticket_type.get())
        if dlg.result:
            self._elements().append(dlg.result)
            self._load_list()

    def _edit(self):
        idx = self._selected_index()
        if idx is None:
            return
        elems = self._elements()
        dlg = TemplateElementDialog(self, self.ticket_type.get(), initial=elems[idx])
        if dlg.result:
            elems[idx] = dlg.result
            self._load_list()

    def _delete(self):
        idx = self._selected_index()
        if idx is None:
            return
        del self._elements()[idx]
        self._load_list()

    def _move(self, delta):
        idx = self._selected_index()
        if idx is None:
            return
        elems = self._elements()
        new_idx = idx + delta
        if new_idx < 0 or new_idx >= len(elems):
            return
        elems[idx], elems[new_idx] = elems[new_idx], elems[idx]
        self._load_list()
        self.listbox.selection_set(new_idx)

    def _on_drag_start(self, event):
        self.drag_index = self.listbox.nearest(event.y)

    def _on_drag_motion(self, event):
        if self.drag_index is None:
            return
        new_index = self.listbox.nearest(event.y)
        if new_index == self.drag_index:
            return
        elems = self._elements()
        if 0 <= self.drag_index < len(elems) and 0 <= new_index < len(elems):
            elems[self.drag_index], elems[new_index] = elems[new_index], elems[self.drag_index]
            self.drag_index = new_index
            self._load_list()
            self.listbox.selection_set(new_index)

    def _reset_default(self):
        t = self.ticket_type.get()
        self.templates[t] = default_templates().get(t, {"elements": []})
        self._load_list()

    def _save(self):
        self.saved = True
        self.destroy()

class AgentManagerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.restart_script = os.path.join(self.base_dir, "deploy", "windows", "restart_service.ps1")
        self.template_editor_host = os.path.join(self.base_dir, "template_editor_host.py")
        self.local_log_files = [
            os.path.join(self.base_dir, "print_agent.log"),
            os.path.join(self.base_dir, "print_agent_service_stdout.log"),
            os.path.join(self.base_dir, "print_agent_service_stderr.log"),
        ]
        self.title("Odoo Print Agent Manager")
        self.geometry("980x640")
        try:
            icon_path = os.path.join(self.base_dir, "deploy", "windows", "assets", "app_logo.ico")
            if os.path.exists(icon_path):
                self.iconbitmap(icon_path)
        except Exception:
            pass
        self.routes = {}
        self.templates = default_templates()
        self._build_ui()
        self.load_from_agent()

    def _build_ui(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill=tk.X)

        self.var_url = tk.StringVar()
        self.var_db = tk.StringVar()
        self.var_user = tk.StringVar()
        self.var_pass = tk.StringVar()
        self.var_poll = tk.StringVar(value="0.2")
        self.var_mode = tk.StringVar(value="network")
        self.var_ip = tk.StringVar(value="192.168.123.100")
        self.var_port = tk.StringVar(value="9100")
        self.var_usb_vid = tk.StringVar(value=str(0x1FC9))
        self.var_usb_pid = tk.StringVar(value=str(0x2016))

        row = 0
        for label, var, width, show in [
            ("Odoo URL", self.var_url, 38, None),
            ("Database", self.var_db, 16, None),
            ("Username", self.var_user, 16, None),
            ("Password", self.var_pass, 16, "*"),
            ("Poll Interval (sec)", self.var_poll, 10, None),
        ]:
            ttk.Label(top, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            entry = ttk.Entry(top, textvariable=var, width=width, show=show)
            entry.grid(row=row, column=1, sticky="w", pady=3)
            row += 1

        defaults = ttk.LabelFrame(self, text="Default Receipt Printer Fallback", padding=10)
        defaults.pack(fill=tk.X, padx=10, pady=(0, 8))
        ttk.Label(defaults, text="Mode").grid(row=0, column=0, sticky="w")
        ttk.Combobox(defaults, textvariable=self.var_mode, values=["network", "usb"], width=12, state="readonly").grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(defaults, text="IP").grid(row=0, column=2, sticky="w")
        ttk.Entry(defaults, textvariable=self.var_ip, width=16).grid(row=0, column=3, sticky="w", padx=6)
        ttk.Label(defaults, text="Port").grid(row=0, column=4, sticky="w")
        ttk.Entry(defaults, textvariable=self.var_port, width=8).grid(row=0, column=5, sticky="w", padx=6)
        ttk.Label(defaults, text="USB VID").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(defaults, textvariable=self.var_usb_vid, width=12).grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(defaults, text="USB PID").grid(row=1, column=2, sticky="w", pady=(6, 0))
        ttk.Entry(defaults, textvariable=self.var_usb_pid, width=12).grid(row=1, column=3, sticky="w", padx=6, pady=(6, 0))

        routes_frame = ttk.LabelFrame(self, text="Printer Routes", padding=10)
        routes_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))

        cols = ("name", "mode", "ip", "port", "vid", "pid")
        self.tree = ttk.Treeview(routes_frame, columns=cols, show="headings", height=12)
        headings = {
            "name": "Name",
            "mode": "Mode",
            "ip": "IP",
            "port": "Port",
            "vid": "USB VID",
            "pid": "USB PID",
        }
        widths = {"name": 130, "mode": 90, "ip": 160, "port": 80, "vid": 120, "pid": 120}
        for c in cols:
            self.tree.heading(c, text=headings[c])
            self.tree.column(c, width=widths[c], anchor="w")
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(routes_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        btns = ttk.Frame(self, padding=10)
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="Reload from Agent", command=self.load_from_agent).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Add Printer", command=self.add_route).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Edit Printer", command=self.edit_route).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Delete Printer", command=self.delete_route).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Test Selected Printer", command=self.test_selected).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Visual Template Editor", command=self.open_template_editor).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="View Logs", command=self.view_logs).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Save Config", command=self.save_config).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Restart Agent", command=self.restart_agent).pack(side=tk.RIGHT, padx=4)

        self.status = tk.StringVar(value="Ready")
        ttk.Label(self, textvariable=self.status, relief=tk.SUNKEN, anchor="w").pack(fill=tk.X, side=tk.BOTTOM)

    def _set_status(self, msg):
        self.status.set(msg)

    def _refresh_tree(self):
        for row_id in self.tree.get_children():
            self.tree.delete(row_id)
        for name, route in sorted(self.routes.items()):
            self.tree.insert(
                "",
                tk.END,
                values=(
                    name,
                    route.get("mode", ""),
                    route.get("ip", ""),
                    route.get("port", ""),
                    route.get("usb_vendor_id", ""),
                    route.get("usb_product_id", ""),
                ),
            )

    def load_from_agent(self):
        try:
            data = http_get("/api/config")
            cfg = data.get("config", {})
            odoo = cfg.get("odoo", {})
            defaults = cfg.get("default", {})
            self.routes = cfg.get("routes", {}) or {}
            self.templates = cfg.get("templates", {}) or default_templates()

            self.var_url.set(odoo.get("url", ""))
            self.var_db.set(odoo.get("db", ""))
            self.var_user.set(odoo.get("username", ""))
            self.var_pass.set(odoo.get("password", ""))
            self.var_poll.set(str(cfg.get("poll_interval_sec", "0.2")))
            self.var_mode.set(defaults.get("mode", "network"))
            self.var_ip.set(str(defaults.get("ip", "")))
            self.var_port.set(str(defaults.get("port", 9100)))
            self.var_usb_vid.set(str(defaults.get("usb_vendor_id", "")))
            self.var_usb_pid.set(str(defaults.get("usb_product_id", "")))

            self._refresh_tree()
            self._set_status("Loaded config from agent")
        except URLError:
            messagebox.showerror(
                "Agent Offline",
                "Cannot connect to local print agent on 127.0.0.1:8899.\n"
                "Use Restart Agent, then View Logs.",
            )
            self._set_status("Agent offline")
        except Exception as exc:
            messagebox.showerror("Error", f"Failed to load config: {exc}")
            self._set_status("Load failed")

    def _route_dialog(self, name="", route=None):
        route = route or {}
        d = tk.Toplevel(self)
        d.title("Printer Route")
        d.geometry("380x250")
        d.transient(self)
        d.grab_set()

        v_name = tk.StringVar(value=name)
        v_mode = tk.StringVar(value=route.get("mode", "network"))
        v_ip = tk.StringVar(value=str(route.get("ip", "")))
        v_port = tk.StringVar(value=str(route.get("port", 9100)))
        v_vid = tk.StringVar(value=str(route.get("usb_vendor_id", "")))
        v_pid = tk.StringVar(value=str(route.get("usb_product_id", "")))

        ttk.Label(d, text="Name").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(d, textvariable=v_name, width=24).grid(row=0, column=1, sticky="w")
        ttk.Label(d, text="Mode").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        ttk.Combobox(d, textvariable=v_mode, values=["network", "usb"], width=20, state="readonly").grid(row=1, column=1, sticky="w")
        ttk.Label(d, text="IP").grid(row=2, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(d, textvariable=v_ip, width=24).grid(row=2, column=1, sticky="w")
        ttk.Label(d, text="Port").grid(row=3, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(d, textvariable=v_port, width=24).grid(row=3, column=1, sticky="w")
        ttk.Label(d, text="USB VID").grid(row=4, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(d, textvariable=v_vid, width=24).grid(row=4, column=1, sticky="w")
        ttk.Label(d, text="USB PID").grid(row=5, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(d, textvariable=v_pid, width=24).grid(row=5, column=1, sticky="w")

        result = {"ok": False}

        def save():
            key = v_name.get().strip()
            if not key:
                messagebox.showwarning("Validation", "Printer name is required.", parent=d)
                return
            result["ok"] = True
            result["name"] = key
            result["route"] = {
                "mode": v_mode.get().strip() or "network",
                "ip": v_ip.get().strip(),
                "port": int(v_port.get().strip() or "9100"),
                "usb_vendor_id": int(v_vid.get().strip() or "0"),
                "usb_product_id": int(v_pid.get().strip() or "0"),
                "timeout_sec": route.get("timeout_sec", 1.0),
                "retries": route.get("retries", 2),
                "cooldown_sec": route.get("cooldown_sec", 3.0),
            }
            d.destroy()

        ttk.Button(d, text="Save", command=save).grid(row=6, column=1, sticky="e", pady=12, padx=8)
        d.wait_window()
        return result

    def add_route(self):
        result = self._route_dialog()
        if not result.get("ok"):
            return
        self.routes[result["name"]] = result["route"]
        self._refresh_tree()

    def edit_route(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("Edit Printer", "Select a printer row first.")
            return
        name = self.tree.item(selected[0], "values")[0]
        route = self.routes.get(name, {})
        result = self._route_dialog(name=name, route=route)
        if not result.get("ok"):
            return
        if result["name"] != name:
            self.routes.pop(name, None)
        self.routes[result["name"]] = result["route"]
        self._refresh_tree()

    def delete_route(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("Delete Printer", "Select a printer row first.")
            return
        name = self.tree.item(selected[0], "values")[0]
        if messagebox.askyesno("Delete Printer", f"Delete route '{name}'?"):
            self.routes.pop(name, None)
            self._refresh_tree()

    def build_config_payload(self):
        return {
            "poll_interval_sec": float(self.var_poll.get().strip() or "0.2"),
            "odoo": {
                "url": self.var_url.get().strip(),
                "db": self.var_db.get().strip(),
                "username": self.var_user.get().strip(),
                "password": self.var_pass.get().strip(),
            },
            "default": {
                "mode": self.var_mode.get().strip() or "network",
                "ip": self.var_ip.get().strip(),
                "port": int(self.var_port.get().strip() or "9100"),
                "usb_vendor_id": int(self.var_usb_vid.get().strip() or "0"),
                "usb_product_id": int(self.var_usb_pid.get().strip() or "0"),
                "timeout_sec": 1.0,
                "retries": 2,
                "cooldown_sec": 3.0,
            },
            "routes": self.routes,
            "templates": self.templates,
        }

    def save_config(self):
        try:
            payload = self.build_config_payload()
            http_post("/api/config", payload)
            self._set_status("Config saved")
            messagebox.showinfo("Success", "Configuration saved to agent.")
        except Exception as exc:
            messagebox.showerror("Save Failed", str(exc))
            self._set_status("Save failed")

    def open_template_editor(self):
        session_payload = {
            "templates": self.templates,
            "ticket_type": "receipt",
            "saved": False,
        }
        session_path = None
        try:
            with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as handle:
                json.dump(session_payload, handle, ensure_ascii=False, indent=2)
                session_path = handle.name

            if not os.path.exists(self.template_editor_host):
                raise RuntimeError(f"template_editor_host.py not found at {self.template_editor_host}")

            proc = subprocess.run(
                [sys.executable, self.template_editor_host, session_path],
                capture_output=True,
                text=True,
                timeout=3600,
                check=False,
            )

            result = {}
            if session_path and os.path.exists(session_path):
                with open(session_path, "r", encoding="utf-8") as handle:
                    result = json.load(handle)

            if proc.returncode != 0:
                error_text = (result.get("error") or proc.stderr or proc.stdout or "").strip()
                raise RuntimeError(error_text or f"Visual editor exited with code {proc.returncode}")

            if result.get("saved"):
                self.templates = result.get("templates", {}) or default_templates()
                try:
                    payload = self.build_config_payload()
                    http_post("/api/config", payload)
                    self._set_status("Templates saved to agent")
                except Exception as save_exc:
                    self._set_status("Templates updated locally (click Save Config to apply)")
                    messagebox.showwarning(
                        "Template Saved Locally",
                        "The template was saved in the manager, but it could not be pushed to the agent yet.\n\n"
                        f"{save_exc}\n\n"
                        "Click Save Config after the agent is available.",
                    )
            else:
                self._set_status("Template edit cancelled")
        except Exception as exc:
            messagebox.showwarning(
                "Visual Editor Unavailable",
                "Could not open the visual template editor.\n\n"
                f"{exc}\n\n"
                "Opening the legacy text editor instead.",
            )
            editor = TemplateEditorWindow(self, self.templates)
            if editor.saved:
                self.templates = editor.templates
                self._set_status("Templates updated (click Save Config to apply)")
        finally:
            if session_path and os.path.exists(session_path):
                try:
                    os.remove(session_path)
                except OSError:
                    pass

    def test_selected(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("Test Printer", "Select a printer row first.")
            return
        name = self.tree.item(selected[0], "values")[0]
        text = simpledialog.askstring("Test Text", "Text to print:", initialvalue="TEST PAGE")
        if text is None:
            return
        try:
            http_post("/api/test-print", {"printer_name": name, "printer_type": "receipt", "text": text})
            messagebox.showinfo("Success", f"Test sent to {name}")
            self._set_status(f"Test sent to {name}")
        except Exception as exc:
            messagebox.showerror("Test Failed", str(exc))
            self._set_status("Test failed")

    def restart_agent(self):
        if not messagebox.askyesno("Restart Agent", "Restart print agent now?"):
            return
        try:
            http_post("/api/restart", {})
            messagebox.showinfo("Restart", "Restart requested.")
            self._set_status("Restart requested")
        except Exception as exc:
            try:
                if not os.path.exists(self.restart_script):
                    raise RuntimeError(f"restart_service.ps1 not found at {self.restart_script}")
                proc = subprocess.run(
                    [
                        "powershell.exe",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        self.restart_script,
                        "-ServiceName",
                        SERVICE_NAME,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                if proc.returncode == 0:
                    self._set_status("Service restarted")
                    messagebox.showinfo("Restart", "Service restarted via Windows service command.")
                    return
                raise RuntimeError((proc.stderr or proc.stdout or "").strip() or f"Exit code: {proc.returncode}")
            except Exception as svc_exc:
                messagebox.showerror(
                    "Restart Failed",
                    f"{exc}\n\nService fallback also failed:\n{svc_exc}",
                )
                self._set_status("Restart failed")

    def view_logs(self):
        try:
            result = http_get("/api/logs")
            log_text = result.get("log_tail", "")
        except Exception as exc:
            log_text = self._read_local_logs()
            if not log_text:
                messagebox.showerror("Logs", f"Failed to fetch logs: {exc}")
                return

        win = tk.Toplevel(self)
        win.title("Print Agent Logs")
        win.geometry("980x520")
        txt = tk.Text(win, wrap="none")
        ysb = ttk.Scrollbar(win, orient="vertical", command=txt.yview)
        xsb = ttk.Scrollbar(win, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        txt.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        ysb.pack(side=tk.RIGHT, fill=tk.Y)
        xsb.pack(side=tk.BOTTOM, fill=tk.X)
        txt.insert("1.0", log_text or "(no logs yet)")
        txt.see(tk.END)
        txt.configure(state=tk.DISABLED)

    def _tail_file(self, path, max_lines=250):
        if not os.path.exists(path):
            return ""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            return "".join(lines[-max_lines:])
        except Exception:
            return ""

    def _read_local_logs(self):
        chunks = []
        for path in self.local_log_files:
            tail = self._tail_file(path, max_lines=220)
            if tail:
                chunks.append(f"===== {os.path.basename(path)} =====\n{tail}")
        return "\n\n".join(chunks)


if __name__ == "__main__":
    app = AgentManagerApp()
    app.mainloop()
