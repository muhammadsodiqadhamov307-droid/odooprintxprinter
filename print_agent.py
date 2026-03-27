#!/usr/bin/env python3
"""
print_agent.py
==============
Local Windows Print Agent for Odoo 19 PoS Thermal Printing.
Polls Odoo XML-RPC for pending pos.print.job records and sends them
to an Xprinter XP-80 via network TCP or USB.

Requirements: see requirements.txt
Run: python print_agent.py
"""

# ============================================================================
# BOOTSTRAP / FALLBACK CONFIGURATION
# ============================================================================
# In normal operation, Odoo UI config is authoritative.
# Values below are only for first connection and safety fallback.
# You can override them via environment variables (no file edit needed).

ODOO_URL = os.getenv('POS_PRINT_BOOTSTRAP_ODOO_URL', 'http://localhost:8070')
ODOO_DB = os.getenv('POS_PRINT_BOOTSTRAP_ODOO_DB', 'default')
ODOO_USERNAME = os.getenv('POS_PRINT_BOOTSTRAP_ODOO_USERNAME', 'admin')
ODOO_PASSWORD = os.getenv('POS_PRINT_BOOTSTRAP_ODOO_PASSWORD', 'password')

# Agent refreshes config from Odoo at runtime (no restart needed for route edits)
REMOTE_CONFIG_REFRESH_SEC = float(os.getenv('POS_PRINT_REMOTE_CONFIG_REFRESH_SEC', '3.0'))

POLL_INTERVAL_SEC = float(os.getenv('POS_PRINT_FALLBACK_POLL_INTERVAL_SEC', '0.2'))

PRINTER_MODE = os.getenv('POS_PRINT_FALLBACK_MODE', 'network')  # default fallback
PRINTER_NETWORK_IP = os.getenv('POS_PRINT_FALLBACK_NETWORK_IP', '192.168.123.100')
PRINTER_NETWORK_PORT = int(os.getenv('POS_PRINT_FALLBACK_NETWORK_PORT', '9100'))

PRINTER_USB_VENDOR_ID = int(os.getenv('POS_PRINT_FALLBACK_USB_VENDOR_ID', '0x1FC9'), 0)
PRINTER_USB_PRODUCT_ID = int(os.getenv('POS_PRINT_FALLBACK_USB_PRODUCT_ID', '0x2016'), 0)

# Optional hard fallback routes when Odoo config is unavailable.
PRINTER_ROUTES = {
    # Set distinct IPs per printer. Example:
    'Kitchen': {'mode': 'network', 'ip': '192.168.123.101', 'port': 9100},
    'Bar': {'mode': 'network', 'ip': '192.168.123.100', 'port': 9100},
}

# Network resilience (can be overridden per route):
#   {'timeout_sec': 1.0, 'retries': 2, 'cooldown_sec': 3}
DEFAULT_NETWORK_TIMEOUT_SEC = 1.0
DEFAULT_NETWORK_RETRIES = 2
DEFAULT_ROUTE_COOLDOWN_SEC = 3.0

LOG_LEVEL = 'INFO'  # 'DEBUG' for verbose output
LOG_FILE = 'print_agent.log'  # Set to None to log to stdout only

# ============================================================================
# IMPORTS
# ============================================================================
import json
import logging
import os
import socket
import sys
import time
import xmlrpc.client
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

try:
    import libusb_package
except ImportError:
    libusb_package = None

try:
    from escpos import exceptions as EscExceptions
    from escpos.printer import Network as EscNetwork
    from escpos.printer import Usb as EscUsb
except ImportError:
    print('ERROR: python-escpos is not installed.')
    print('Run: pip install python-escpos pyusb Pillow')
    sys.exit(1)


logger = logging.getLogger('PrintAgent')
_printer_cache = None
_route_fail_until = {}
_runtime_lock = threading.RLock()

_runtime_config = {
    'poll_interval_sec': POLL_INTERVAL_SEC,
    'default': {
        'mode': PRINTER_MODE,
        'ip': PRINTER_NETWORK_IP,
        'port': PRINTER_NETWORK_PORT,
        'usb_vendor_id': PRINTER_USB_VENDOR_ID,
        'usb_product_id': PRINTER_USB_PRODUCT_ID,
        'timeout_sec': DEFAULT_NETWORK_TIMEOUT_SEC,
        'retries': DEFAULT_NETWORK_RETRIES,
        'cooldown_sec': DEFAULT_ROUTE_COOLDOWN_SEC,
    },
    'routes': dict(PRINTER_ROUTES),
    'odoo': {
        'url': ODOO_URL,
        'db': ODOO_DB,
        'username': ODOO_USERNAME,
        'password': ODOO_PASSWORD,
    },
    'last_update': None,
}
_runtime_last_fetch_ts = 0.0


# ============================================================================
# LOGGING SETUP
# ============================================================================
def setup_logging():
    level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    handlers = [logging.StreamHandler(sys.stdout)]
    if LOG_FILE:
        handlers.append(logging.FileHandler(LOG_FILE, encoding='utf-8'))
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=handlers,
        force=True,
    )
    logger.setLevel(level)


def _runtime_poll_interval():
    with _runtime_lock:
        return _as_float(_runtime_config.get('poll_interval_sec', POLL_INTERVAL_SEC), POLL_INTERVAL_SEC)


def _runtime_defaults():
    with _runtime_lock:
        defaults = _runtime_config.get('default') or {}
        return dict(defaults)


def _runtime_routes():
    with _runtime_lock:
        routes = _runtime_config.get('routes') or {}
        return dict(routes)


def _runtime_odoo_settings():
    with _runtime_lock:
        odoo_settings = _runtime_config.get('odoo') or {}
        return dict(odoo_settings)


def _runtime_default_value(key, fallback=None):
    defaults = _runtime_defaults()
    if key in defaults and defaults[key] not in (None, ''):
        return defaults[key]
    return fallback


def _apply_remote_config_payload(payload):
    if not isinstance(payload, dict):
        return False

    routes = payload.get('routes') if isinstance(payload.get('routes'), dict) else None
    defaults = payload.get('default') if isinstance(payload.get('default'), dict) else None
    odoo_settings = payload.get('odoo') if isinstance(payload.get('odoo'), dict) else None
    poll_interval = payload.get('poll_interval_sec', POLL_INTERVAL_SEC)

    with _runtime_lock:
        changed = False
        if routes is not None:
            normalized_routes = {str(k): dict(v or {}) for k, v in routes.items()}
            if normalized_routes != (_runtime_config.get('routes') or {}):
                _runtime_config['routes'] = normalized_routes
                changed = True

        if defaults is not None:
            normalized_defaults = dict(_runtime_config.get('default') or {})
            normalized_defaults.update(defaults)
            if normalized_defaults != (_runtime_config.get('default') or {}):
                _runtime_config['default'] = normalized_defaults
                changed = True

        if odoo_settings is not None:
            normalized_odoo = dict(_runtime_config.get('odoo') or {})
            normalized_odoo.update({
                'url': odoo_settings.get('url') or normalized_odoo.get('url') or ODOO_URL,
                'db': odoo_settings.get('db') or normalized_odoo.get('db') or ODOO_DB,
                'username': odoo_settings.get('username') or normalized_odoo.get('username') or ODOO_USERNAME,
                'password': odoo_settings.get('password') or normalized_odoo.get('password') or ODOO_PASSWORD,
            })
            if normalized_odoo != (_runtime_config.get('odoo') or {}):
                _runtime_config['odoo'] = normalized_odoo
                changed = True

        poll_interval_num = _as_float(poll_interval, POLL_INTERVAL_SEC)
        if poll_interval_num <= 0:
            poll_interval_num = POLL_INTERVAL_SEC
        if poll_interval_num != _runtime_config.get('poll_interval_sec'):
            _runtime_config['poll_interval_sec'] = poll_interval_num
            changed = True

        if changed:
            _runtime_config['last_update'] = payload.get('write_date') or datetime.now().isoformat()
        return changed


def refresh_runtime_config(odoo: "OdooConnection", force=False):
    """
    Pull runtime agent configuration from Odoo and apply it in-memory.
    Returns True if config changed.
    """
    global _runtime_last_fetch_ts

    now = time.monotonic()
    if not force and (now - _runtime_last_fetch_ts) < REMOTE_CONFIG_REFRESH_SEC:
        return False
    _runtime_last_fetch_ts = now

    try:
        payload = odoo.execute('pos.print.agent.config', 'rpc_get_agent_config')
    except Exception as exc:  # noqa: BLE001
        logger.debug('Could not refresh remote runtime config: %s', exc)
        return False

    changed = _apply_remote_config_payload(payload)
    odoo_settings = _runtime_odoo_settings()
    odoo.apply_remote_odoo_settings(odoo_settings)

    if changed:
        route_count = len((_runtime_routes() or {}).keys())
        logger.info(
            'Remote config updated: poll=%.3fs routes=%s',
            _runtime_poll_interval(),
            route_count,
        )
    return changed


# ============================================================================
# ODOO XML-RPC CONNECTION
# ============================================================================
class OdooConnection:
    """Manages an authenticated XML-RPC session to Odoo."""

    def __init__(self):
        self.desired_url = ODOO_URL
        self.desired_db = ODOO_DB
        self.desired_username = ODOO_USERNAME
        self.desired_password = ODOO_PASSWORD

        runtime = _runtime_odoo_settings()
        self.desired_url = runtime.get('url') or self.desired_url
        self.desired_db = runtime.get('db') or self.desired_db
        self.desired_username = runtime.get('username') or self.desired_username
        self.desired_password = runtime.get('password') or self.desired_password

        self.active_url = None
        self.active_db = None
        self.active_username = None
        self.active_password = None
        self.uid = None
        self.models = None
        self._connect()

    def _connect(self, retry_forever=True):
        """Authenticate and store uid + models proxy."""
        while True:
            try:
                url = self.desired_url
                db = self.desired_db
                username = self.desired_username
                password = self.desired_password
                logger.info('Connecting to Odoo at %s (db=%s)...', url, db)
                common = xmlrpc.client.ServerProxy(
                    f'{url}/xmlrpc/2/common',
                    allow_none=True,
                )
                uid = common.authenticate(
                    db,
                    username,
                    password,
                    {},
                )
                if not uid:
                    raise ValueError('Authentication failed - check credentials')
                models = xmlrpc.client.ServerProxy(
                    f'{url}/xmlrpc/2/object',
                    allow_none=True,
                )

                self.uid = uid
                self.models = models
                self.active_url = url
                self.active_db = db
                self.active_username = username
                self.active_password = password
                logger.info('Authenticated as uid=%s', self.uid)
                return True
            except Exception as e:
                if not retry_forever:
                    logger.warning('Odoo reconnect attempt failed: %s', e)
                    return False
                logger.error('Odoo connection error: %s. Retrying in 10s...', e)
                time.sleep(10)

    def apply_remote_odoo_settings(self, odoo_settings):
        """
        Apply Odoo connection settings received from Odoo config.
        For safety, switches only if a test reconnect succeeds.
        """
        if not isinstance(odoo_settings, dict):
            return

        new_url = (odoo_settings.get('url') or '').strip() or self.desired_url
        new_db = (odoo_settings.get('db') or '').strip() or self.desired_db
        new_username = (odoo_settings.get('username') or '').strip() or self.desired_username
        new_password = (odoo_settings.get('password') or '').strip() or self.desired_password

        current_tuple = (self.desired_url, self.desired_db, self.desired_username, self.desired_password)
        new_tuple = (new_url, new_db, new_username, new_password)
        if new_tuple == current_tuple:
            return

        old_tuple = current_tuple
        self.desired_url, self.desired_db, self.desired_username, self.desired_password = new_tuple
        if self._connect(retry_forever=False):
            logger.info(
                'Switched Odoo connection from %s/%s to %s/%s using remote settings',
                old_tuple[0],
                old_tuple[1],
                new_url,
                new_db,
            )
            return

        # Revert if new connection fails
        self.desired_url, self.desired_db, self.desired_username, self.desired_password = old_tuple
        logger.warning(
            'Remote Odoo settings could not be applied. Keeping current connection %s/%s',
            self.active_url or self.desired_url,
            self.active_db or self.desired_db,
        )

    def execute(self, model, method, *args, **kwargs):
        """
        Execute an XML-RPC call. On failure, reconnects once and retries.
        Returns the result or raises the exception.
        """
        try:
            return self.models.execute_kw(
                self.active_db,
                self.uid,
                self.active_password,
                model,
                method,
                list(args),
                kwargs,
            )
        except xmlrpc.client.Fault as e:
            logger.error('XML-RPC Fault: %s', e)
            raise
        except (socket.timeout, ConnectionRefusedError, OSError) as e:
            logger.warning('Network error during XML-RPC call: %s. Reconnecting...', e)
            self._connect()
            return self.models.execute_kw(
                self.active_db,
                self.uid,
                self.active_password,
                model,
                method,
                list(args),
                kwargs,
            )


# ============================================================================
# PRINTER FACTORY
# ============================================================================
def _as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _resolve_route(printer_name=None):
    """
    Resolve route config by printer name (case-insensitive).
    Returns (route_name, route_dict).
    """
    if not printer_name:
        return None, {}

    requested = str(printer_name).strip()
    if not requested:
        return None, {}

    requested_norm = requested.lower()
    routes = _runtime_routes()
    if not routes:
        routes = dict(PRINTER_ROUTES)

    for route_name, route in routes.items():
        if str(route_name).strip().lower() == requested_norm:
            return route_name, dict(route or {})

    # Unknown name: keep it for logs, but use global defaults.
    return requested, {}


def _route_key(route_name, route):
    if route_name:
        return f'name:{str(route_name).strip().lower()}'
    mode = route.get('mode', _runtime_default_value('mode', PRINTER_MODE))
    if mode == 'network':
        return f'net:{route.get("ip", _runtime_default_value("ip", PRINTER_NETWORK_IP))}:{route.get("port", _runtime_default_value("port", PRINTER_NETWORK_PORT))}'
    if mode == 'usb':
        return f'usb:{route.get("usb_vendor_id", _runtime_default_value("usb_vendor_id", PRINTER_USB_VENDOR_ID))}:{route.get("usb_product_id", _runtime_default_value("usb_product_id", PRINTER_USB_PRODUCT_ID))}'
    return f'mode:{mode}'


def _cooldown_remaining(route_key):
    until = _route_fail_until.get(route_key, 0.0)
    remaining = until - time.monotonic()
    return remaining if remaining > 0 else 0.0


def _is_network_printer(printer):
    return isinstance(printer, EscNetwork)


def _close_printer(printer):
    if printer and _is_network_printer(printer):
        try:
            printer.close()
        except Exception:
            pass


def get_printer(printer_name=None, route=None, network_timeout=None):
    """
    Returns an escpos printer instance based on PRINTER_MODE or per-printer route.
    Raises an exception if the printer cannot be reached.
    """
    route_name, resolved_route = _resolve_route(printer_name)
    route = dict(route or resolved_route)
    mode = route.get('mode', _runtime_default_value('mode', PRINTER_MODE))

    if mode == 'network':
        host = route.get('ip', _runtime_default_value('ip', PRINTER_NETWORK_IP))
        port = _as_int(route.get('port', _runtime_default_value('port', PRINTER_NETWORK_PORT)), PRINTER_NETWORK_PORT)
        timeout = _as_float(
            network_timeout if network_timeout is not None else route.get('timeout_sec', _runtime_default_value('timeout_sec', DEFAULT_NETWORK_TIMEOUT_SEC)),
            DEFAULT_NETWORK_TIMEOUT_SEC,
        )
        logger.info('Routing printer "%s" to network %s:%s', route_name or printer_name, host, port)
        printer = EscNetwork(host=host, port=port, timeout=timeout)
        return printer

    if mode == 'usb':
        global _printer_cache
        if _printer_cache is not None and not route:
            return _printer_cache
        vid = _as_int(route.get('usb_vendor_id', _runtime_default_value('usb_vendor_id', PRINTER_USB_VENDOR_ID)), PRINTER_USB_VENDOR_ID)
        pid = _as_int(route.get('usb_product_id', _runtime_default_value('usb_product_id', PRINTER_USB_PRODUCT_ID)), PRINTER_USB_PRODUCT_ID)
        logger.info('Routing printer "%s" to USB VID=0x%04X PID=0x%04X', route_name or printer_name, vid, pid)
        backend = libusb_package.get_libusb1_backend() if libusb_package else None
        if backend is None:
            raise RuntimeError(
                'No libusb backend available. Install libusb-package and ensure the printer uses WinUSB.'
            )
        printer = EscUsb(idVendor=vid, idProduct=pid, backend=backend)
        if not route:
            _printer_cache = printer
        return printer

    raise ValueError(f'Unknown PRINTER_MODE: {mode!r}')


def print_with_route(data: str, printer_type: str, printer_name=None) -> None:
    """
    Print data with route-aware timeout/retry/cooldown so one bad route
    does not block all other routes.
    """
    route_name, route = _resolve_route(printer_name)
    mode = route.get('mode', _runtime_default_value('mode', PRINTER_MODE))

    timeout_sec = _as_float(
        route.get('timeout_sec', _runtime_default_value('timeout_sec', DEFAULT_NETWORK_TIMEOUT_SEC)),
        DEFAULT_NETWORK_TIMEOUT_SEC,
    )
    retries_default = _as_int(_runtime_default_value('retries', DEFAULT_NETWORK_RETRIES), DEFAULT_NETWORK_RETRIES) if mode == 'network' else 1
    retries = max(1, _as_int(route.get('retries', retries_default), retries_default))
    cooldown_sec = max(
        0.0,
        _as_float(
            route.get('cooldown_sec', _runtime_default_value('cooldown_sec', DEFAULT_ROUTE_COOLDOWN_SEC)),
            DEFAULT_ROUTE_COOLDOWN_SEC,
        ),
    )
    route_key = _route_key(route_name, route)

    remaining = _cooldown_remaining(route_key)
    if remaining > 0:
        raise RuntimeError(
            f'Route "{route_name or "default"}" temporarily skipped ({remaining:.1f}s cooldown remaining)'
        )

    last_exc = None
    for attempt in range(1, retries + 1):
        printer = None
        try:
            printer = get_printer(route_name or printer_name, route=route, network_timeout=timeout_sec)
            format_receipt(data, printer_type, printer)
            _route_fail_until.pop(route_key, None)
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            is_last = attempt >= retries
            if not is_last:
                logger.warning(
                    'Print attempt %s/%s failed for route "%s": %s. Retrying...',
                    attempt,
                    retries,
                    route_name or printer_name or 'default',
                    exc,
                )
                time.sleep(0.15)
            else:
                if cooldown_sec > 0:
                    _route_fail_until[route_key] = time.monotonic() + cooldown_sec
                raise
        finally:
            _close_printer(printer)

    if last_exc:
        raise last_exc


# ============================================================================
# RECEIPT FORMATTER
# ============================================================================
def _money(amount, symbol=''):
    try:
        value = float(amount or 0)
    except (TypeError, ValueError):
        value = 0.0
    if symbol:
        return f'{value:.2f} {symbol}'
    return f'{value:.2f}'


def _left_right(left, right, width=42):
    left = str(left or '')
    right = str(right or '')
    if len(left) + len(right) >= width:
        left = left[: max(0, width - len(right) - 1)]
    spaces = ' ' * max(1, width - len(left) - len(right))
    return f'{left}{spaces}{right}'


def _truncate(text, width):
    text = str(text or '')
    return text if len(text) <= width else text[: max(0, width - 1)] + '.'


def _fit_columns(qty, name, price, width=42):
    qty = str(qty or '')
    name = str(name or '')
    price = str(price or '')
    reserved = len(qty) + len(price) + 2
    name_width = max(8, width - reserved)
    display_name = _truncate(name, name_width)
    spaces = ' ' * max(1, width - len(qty) - len(display_name) - len(price))
    return f'{qty} {display_name}{spaces}{price}'


def _resolve_printer_name(payload, default=None):
    try:
        if isinstance(payload, dict):
            if payload.get('printer_name'):
                return payload['printer_name']
            if payload.get('printer_type') == 'kitchen':
                return 'Kitchen'
            if payload.get('type') == 'kitchen':
                return 'Kitchen'
            if payload.get('type') == 'receipt':
                return 'Receipt'
    except Exception:
        pass
    return default


def _first_non_empty(*values):
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ''


def _parse_qty(value, default=1.0):
    if value is None:
        return float(default)
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return float(default)
    try:
        return float(text)
    except ValueError:
        try:
            return float(text.replace(',', '.'))
        except ValueError:
            return float(default)


def _display_qty(value):
    qty = _parse_qty(value, default=1.0)
    if abs(qty - round(qty)) < 1e-9:
        return str(int(round(qty)))
    return f'{qty:g}'


def _as_line_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []


def _extract_qty(line):
    if not isinstance(line, dict):
        return 1.0
    for key in ('qty', 'quantity', 'qty_done', 'count', 'new_qty', 'newQty', 'delta', 'amount'):
        if key in line and line.get(key) is not None:
            return _parse_qty(line.get(key), default=1.0)
    return 1.0


def _extract_product_name(line):
    if not isinstance(line, dict):
        return str(line or '')
    product = line.get('product')
    if isinstance(product, dict):
        return _first_non_empty(product.get('name'), product.get('display_name'))
    if isinstance(product, (list, tuple)) and len(product) > 1:
        return str(product[1])
    return _first_non_empty(
        product if isinstance(product, str) else None,
        line.get('name'),
        line.get('product_name'),
        line.get('display_name'),
        line.get('full_product_name'),
    )


def _extract_line_note(line):
    if not isinstance(line, dict):
        return ''
    return _first_non_empty(
        line.get('note'),
        line.get('internal_note'),
        line.get('customer_note'),
    )


def _wrap_text(text, width=42):
    text = str(text or '')
    if not text:
        return ['']
    words = text.split()
    if not words:
        return [text[:width]]
    lines = []
    current = words[0]
    for word in words[1:]:
        candidate = f'{current} {word}'
        if len(candidate) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def format_receipt(data: str, printer_type: str, printer) -> None:
    """
    Formats and prints the receipt or kitchen ticket.

    Strategy:
    - If data is valid JSON (sent from kitchen override), render as structured KOT.
    - Otherwise, print as plain text receipt.

    @param data : The raw string from pos.print.job.data
    @param printer_type : 'receipt' or 'kitchen'
    @param printer : An escpos printer instance (network or USB)
    """
    printer.set(align='center', font='a', bold=True, height=2, width=2)

    try:
        payload = json.loads(data)
        is_json = True
    except (json.JSONDecodeError, TypeError):
        payload = None
        is_json = False

    if printer_type == 'receipt' and is_json and payload.get('type') == 'receipt':
        currency_symbol = payload.get('currency_symbol', '')
        printer.set(align='center', font='a', bold=True, height=2, width=1)
        printer.text(f"{payload.get('company_name', 'Odoo POS')}\n")
        printer.set(align='center', font='a', bold=False, height=1, width=1)
        if payload.get('order_name'):
            printer.text(f"Ticket {payload.get('order_name')}\n")
        if payload.get('date'):
            printer.text(f"{payload.get('date')[:19].replace('T', ' ')}\n")
        if payload.get('cashier'):
            printer.text(f"Served by: {payload.get('cashier')}\n")
        if payload.get('table') or payload.get('customer_count'):
            table_label = payload.get('table') or '-'
            guest_label = payload.get('customer_count') or '-'
            printer.text(f"Table: {table_label}  Guests: {guest_label}\n")
        if payload.get('tracking_number'):
            printer.set(align='center', font='a', bold=True, height=2, width=2)
            printer.text(f"{payload.get('tracking_number')}\n")
        printer.text('-' * 42 + '\n')
        printer.set(align='left', font='a', bold=False, height=1, width=1)
        for line in payload.get('lines', []):
            qty_text = _display_qty(line.get('qty', 0))
            price_text = line.get('price_display') or _money(line.get('price', 0), currency_symbol)
            printer.set(align='left', font='b', bold=False, height=1, width=1)
            printer.text(_fit_columns(qty_text, line.get('name', ''), price_text) + '\n')
            printer.set(align='left', font='a', bold=False, height=1, width=1)
            unit_price_display = line.get('unit_price_display')
            if unit_price_display:
                printer.set(align='left', font='b', bold=False, height=1, width=1)
                printer.text(f"  {unit_price_display}\n")
                printer.set(align='left', font='a', bold=False, height=1, width=1)
        printer.text('-' * 42 + '\n')
        printer.text(_left_right('Subtotal', _money(payload.get('subtotal', 0), currency_symbol)) + '\n')
        printer.text(_left_right('Tax', _money(payload.get('tax', 0), currency_symbol)) + '\n')
        printer.set(bold=True)
        printer.text(_left_right('Total', _money(payload.get('total', 0), currency_symbol)) + '\n')
        printer.set(bold=False)
        for payment in payload.get('payments', []):
            printer.text(
                _left_right(
                    payment.get('name', 'Payment'),
                    payment.get('amount_display') or _money(payment.get('amount', 0), currency_symbol),
                ) + '\n'
            )
    elif printer_type == 'kitchen' and is_json:
        lines = []
        changes = payload.get('changes', {})
        table_label = _first_non_empty(
            payload.get('table'),
            payload.get('table_name'),
            payload.get('table_number'),
            payload.get('table_id', {}).get('table_number') if isinstance(payload.get('table_id'), dict) else None,
            payload.get('table_id', {}).get('name') if isinstance(payload.get('table_id'), dict) else None,
        )
        order_label = _first_non_empty(
            payload.get('order'),
            payload.get('order_name'),
            payload.get('name'),
            payload.get('tracking_number'),
            payload.get('trackingNumber'),
        )
        for section_name in ('new', 'cancelled', 'noteUpdate'):
            for line in _as_line_list(changes.get(section_name, [])):
                lines.append({
                    'qty': _extract_qty(line),
                    'product': _extract_product_name(line),
                    'note': _extract_line_note(line),
                    'section': section_name,
                })
        for line in _as_line_list(changes.get('data', [])):
            lines.append({
                'qty': _extract_qty(line),
                'product': _extract_product_name(line),
                'note': _extract_line_note(line),
                'section': 'new',
            })
        for line in _as_line_list(payload.get('orderlines', [])):
            lines.append({
                'qty': _extract_qty(line),
                'product': _extract_product_name(line),
                'note': _extract_line_note(line),
                'section': 'new',
            })

        printer.text('** KITCHEN ORDER **\n')
        printer.set(align='left', font='a', bold=False, height=1, width=1)
        printer.text(f"Printer : {payload.get('printer_name', 'Kitchen')}\n")
        printer.text(f"Table   : {table_label or 'N/A'}\n")
        printer.text(f"Order   : {order_label}\n")
        printer.text(f"Time    : {datetime.now().strftime('%H:%M:%S')}\n")
        printer.text('-' * 40 + '\n')
        for line in lines:
            prefix = ''
            if line['section'] == 'cancelled':
                prefix = 'CXL '
            elif line['section'] == 'noteUpdate':
                prefix = 'NOTE '
            name = f"{prefix}{line['product']}"[:30]
            printer.text(f"{_display_qty(line['qty']):<6}{name}\n")
            if line['note']:
                printer.text(f" >> {line['note']}\n")
        printer.text('-' * 40 + '\n')
    else:
        printer.text('** RECEIPT **\n')
        printer.set(align='left', font='a', bold=False, height=1, width=1)
        printer.text(data + '\n')

    printer.text('\n\n\n')
    try:
        printer.cut(mode='FULL')
    except Exception:
        # fallback raw cut
        try:
            printer._raw(b'\x1dV\x00')
        except Exception:
            pass


# ============================================================================
# CORE POLL LOOP
# ============================================================================
def process_pending_jobs(odoo: OdooConnection) -> None:
    """
    Fetch all pending jobs from Odoo, print each one, then update the state to
    'printed' or 'failed'.
    """
    pending_ids = odoo.execute(
        'pos.print.job',
        'search',
        [('state', '=', 'pending')],
        order='create_date asc',
    )
    if not pending_ids:
        return

    logger.info('Found %d pending job(s)', len(pending_ids))
    jobs = odoo.execute(
        'pos.print.job',
        'read',
        pending_ids,
        fields=['id', 'name', 'data', 'printer_type', 'printer_name'],
    )

    for job in jobs:
        job_id = job['id']
        job_name = job['name']
        data = job['data']
        printer_type = job['printer_type']
        route_printer = job.get('printer_name')
        try:
            if not route_printer:
                parsed = json.loads(data)
                route_printer = _resolve_printer_name(parsed)
        except Exception:
            route_printer = route_printer or None
        logger.info(
            'Processing job id=%s name=%s type=%s',
            job_id,
            job_name,
            printer_type,
        )

        try:
            print_with_route(data, printer_type, route_printer)
            odoo.execute(
                'pos.print.job',
                'write',
                [job_id],
                {'state': 'printed', 'error_msg': False},
            )
            logger.info('Job id=%s printed successfully', job_id)
        except Exception as exc:
            global _printer_cache
            error_text = str(exc)
            logger.error('Job id=%s FAILED: %s', job_id, error_text)
            if isinstance(exc, EscExceptions.Error):
                logger.debug('ESC/POS exception details', exc_info=True)
                if PRINTER_MODE == 'usb':
                    _printer_cache = None
            try:
                odoo.execute(
                    'pos.print.job',
                    'write',
                    [job_id],
                    {'state': 'failed', 'error_msg': error_text},
                )
            except Exception as write_exc:
                logger.error(
                    'Could not update failed state for job %s: %s',
                    job_id,
                    write_exc,
                )


# ============================================================================
# ENTRY POINT
# ============================================================================
def main():
    setup_logging()
    runtime_odoo = _runtime_odoo_settings()
    startup_url = runtime_odoo.get('url') or ODOO_URL
    startup_db = runtime_odoo.get('db') or ODOO_DB
    logger.info('=' * 60)
    logger.info('Odoo PoS Print Agent starting...')
    logger.info('Mode: %s | Poll interval: %ss', _runtime_default_value('mode', PRINTER_MODE), _runtime_poll_interval())
    logger.info('Odoo: %s (db=%s)', startup_url, startup_db)
    logger.info('=' * 60)

    odoo = OdooConnection()
    refresh_runtime_config(odoo, force=True)

    # Start local HTTP push server for immediate print
    def start_http_server():
        host = '127.0.0.1'
        port = 8899

        class PrintHandler(BaseHTTPRequestHandler):
            def _set_headers(self, status=200):
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
                self.send_header('Access-Control-Allow-Headers', 'Content-Type')
                self.end_headers()

            def do_OPTIONS(self):
                self._set_headers()

            def do_POST(self):
                try:
                    length = int(self.headers.get('Content-Length', 0))
                    payload = self.rfile.read(length) if length else b'{}'
                    body = json.loads(payload.decode('utf-8'))
                    data = body.get('data', '')
                    printer_type = body.get('printer_type', 'receipt')
                    route_printer = _resolve_printer_name(body)
                    logger.info('Push received: type=%s route_printer=%s keys=%s', printer_type, route_printer, list(body.keys()))
                    if not data:
                        self._set_headers(400)
                        self.wfile.write(b'{"success":false,"error":"no data"}')
                        return
                    try:
                        print_with_route(data, printer_type, route_printer)
                        self._set_headers(200)
                        self.wfile.write(b'{"success":true}')
                    except Exception as exc:  # noqa: BLE001
                        logger.error('Push print failed: %s', exc, exc_info=True)
                        self._set_headers(500)
                        self.wfile.write(json.dumps({"success": False, "error": str(exc)}).encode('utf-8'))
                except Exception as exc:  # noqa: BLE001
                    logger.error('HTTP handler error: %s', exc, exc_info=True)
                    self._set_headers(500)
                    self.wfile.write(json.dumps({"success": False, "error": str(exc)}).encode('utf-8'))

        httpd = ThreadingHTTPServer((host, port), PrintHandler)
        logger.info('HTTP push server listening on http://%s:%s/print', host, port)
        httpd.serve_forever()

    threading.Thread(target=start_http_server, daemon=True).start()

    while True:
        try:
            refresh_runtime_config(odoo)
            process_pending_jobs(odoo)
        except KeyboardInterrupt:
            logger.info('Shutting down (KeyboardInterrupt)')
            sys.exit(0)
        except Exception as e:
            logger.error('Unexpected error in poll loop: %s', e, exc_info=True)

        time.sleep(_runtime_poll_interval())


if __name__ == '__main__':
    main()
