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
import os

# ============================================================================
# BOOTSTRAP / FALLBACK CONFIGURATION
# ============================================================================
# Values below are bootstrap defaults for first run.
# After first run, local app config file is authoritative.
# You can still override via environment variables if needed.

ODOO_URL = os.getenv('POS_PRINT_BOOTSTRAP_ODOO_URL', 'http://localhost:8070')
ODOO_DB = os.getenv('POS_PRINT_BOOTSTRAP_ODOO_DB', 'default')
ODOO_USERNAME = os.getenv('POS_PRINT_BOOTSTRAP_ODOO_USERNAME', 'admin')
ODOO_PASSWORD = os.getenv('POS_PRINT_BOOTSTRAP_ODOO_PASSWORD', 'password')

USE_ODOO_REMOTE_CONFIG = os.getenv('POS_PRINT_USE_ODOO_REMOTE_CONFIG', '0').lower() in ('1', 'true', 'yes')
REMOTE_CONFIG_REFRESH_SEC = float(os.getenv('POS_PRINT_REMOTE_CONFIG_REFRESH_SEC', '3.0'))
LOCAL_CONFIG_FILE = os.getenv('POS_PRINT_LOCAL_CONFIG_FILE', 'agent_config.local.json')
WINDOWS_REGISTRY_PATH = os.getenv('POS_PRINT_REGISTRY_PATH', r'Software\OdooPrintAgent')

POLL_INTERVAL_SEC = float(os.getenv('POS_PRINT_POLL_INTERVAL_SEC', '0.2'))

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
import re
import socket
import sys
import time
import xmlrpc.client
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from urllib.parse import urlparse

try:
    import libusb_package
except ImportError:
    libusb_package = None

try:
    import winreg
except ImportError:
    winreg = None

try:
    from escpos import exceptions as EscExceptions
    from escpos.printer import Network as EscNetwork
    from escpos.printer import Usb as EscUsb
except ImportError:
    print('ERROR: python-escpos is not installed.')
    print('Run: pip install python-escpos pyusb Pillow')
    sys.exit(1)

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None
    ImageDraw = None
    ImageFont = None


logger = logging.getLogger('PrintAgent')
_printer_cache = None
_route_fail_until = {}
_runtime_lock = threading.RLock()
_last_odoo_unavailable_log_ts = 0.0
_font_cache = {}

_LEGACY_EDITOR_CANVAS_WIDTH = 300
_EDITOR_CANVAS_WIDTH = 420
_EDITOR_CANVAS_PADDING = 12
_VISUAL_PAPER_FALLBACK_WIDTH = 576
_VISUAL_MIN_BLOCK_WIDTH = 96
_VISUAL_MIN_BLOCK_HEIGHT = 26


def _default_templates():
    return {
        'receipt': {
            'elements': [
                {'field': 'company_name', 'align': 'center', 'style': 'double'},
                {'field': 'order_name_line', 'align': 'center'},
                {'field': 'date_line', 'align': 'center'},
                {'field': 'cashier_line', 'align': 'center'},
                {'field': 'table_guests_line', 'align': 'center'},
                {'field': 'tracking_number', 'align': 'center', 'style': 'double'},
                {'field': 'separator'},
                {'field': 'lines_block'},
                {'field': 'separator'},
                {'field': 'subtotal_line'},
                {'field': 'tax_line'},
                {'field': 'total_line', 'style': 'bold'},
                {'field': 'payments_block'},
            ]
        },
        'kitchen': {
            'elements': [
                {'field': 'table_big', 'align': 'center', 'style': 'double'},
                {'field': 'table_circle', 'align': 'center'},
                {'field': 'ticket_title', 'align': 'center'},
                {'field': 'printer_line'},
                {'field': 'table_line'},
                {'field': 'order_line'},
                {'field': 'time_line'},
                {'field': 'waiter_line'},
                {'field': 'separator'},
                {'field': 'items_block'},
                {'field': 'separator'},
            ]
        },
    }

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
    'templates': _default_templates(),
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


def _config_file_path():
    if os.path.isabs(LOCAL_CONFIG_FILE):
        return LOCAL_CONFIG_FILE
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), LOCAL_CONFIG_FILE)


def _runtime_snapshot():
    with _runtime_lock:
        return json.loads(json.dumps(_runtime_config))


def _sanitize_runtime_payload(payload):
    if not isinstance(payload, dict):
        return None

    clean = {}
    if isinstance(payload.get('poll_interval_sec'), (int, float, str)):
        clean['poll_interval_sec'] = _as_float(payload.get('poll_interval_sec'), POLL_INTERVAL_SEC)

    default_cfg = payload.get('default')
    if isinstance(default_cfg, dict):
        clean['default'] = {
            'mode': default_cfg.get('mode', PRINTER_MODE),
            'ip': str(default_cfg.get('ip', PRINTER_NETWORK_IP) or ''),
            'port': _as_int(default_cfg.get('port', PRINTER_NETWORK_PORT), PRINTER_NETWORK_PORT),
            'usb_vendor_id': _as_int(default_cfg.get('usb_vendor_id', PRINTER_USB_VENDOR_ID), PRINTER_USB_VENDOR_ID),
            'usb_product_id': _as_int(default_cfg.get('usb_product_id', PRINTER_USB_PRODUCT_ID), PRINTER_USB_PRODUCT_ID),
            'timeout_sec': _as_float(default_cfg.get('timeout_sec', DEFAULT_NETWORK_TIMEOUT_SEC), DEFAULT_NETWORK_TIMEOUT_SEC),
            'retries': _as_int(default_cfg.get('retries', DEFAULT_NETWORK_RETRIES), DEFAULT_NETWORK_RETRIES),
            'cooldown_sec': _as_float(default_cfg.get('cooldown_sec', DEFAULT_ROUTE_COOLDOWN_SEC), DEFAULT_ROUTE_COOLDOWN_SEC),
        }

    odoo_cfg = payload.get('odoo')
    if isinstance(odoo_cfg, dict):
        clean['odoo'] = {
            'url': str(odoo_cfg.get('url', ODOO_URL) or ''),
            'db': str(odoo_cfg.get('db', ODOO_DB) or ''),
            'username': str(odoo_cfg.get('username', ODOO_USERNAME) or ''),
            'password': str(odoo_cfg.get('password', ODOO_PASSWORD) or ''),
        }

    routes_cfg = payload.get('routes')
    if isinstance(routes_cfg, dict):
        clean_routes = {}
        for raw_name, raw_route in routes_cfg.items():
            name = str(raw_name or '').strip()
            if not name or not isinstance(raw_route, dict):
                continue
            clean_routes[name] = {
                'mode': raw_route.get('mode', PRINTER_MODE),
                'ip': str(raw_route.get('ip', '') or ''),
                'port': _as_int(raw_route.get('port', PRINTER_NETWORK_PORT), PRINTER_NETWORK_PORT),
                'usb_vendor_id': _as_int(raw_route.get('usb_vendor_id', PRINTER_USB_VENDOR_ID), PRINTER_USB_VENDOR_ID),
                'usb_product_id': _as_int(raw_route.get('usb_product_id', PRINTER_USB_PRODUCT_ID), PRINTER_USB_PRODUCT_ID),
                'timeout_sec': _as_float(raw_route.get('timeout_sec', DEFAULT_NETWORK_TIMEOUT_SEC), DEFAULT_NETWORK_TIMEOUT_SEC),
                'retries': _as_int(raw_route.get('retries', DEFAULT_NETWORK_RETRIES), DEFAULT_NETWORK_RETRIES),
                'cooldown_sec': _as_float(raw_route.get('cooldown_sec', DEFAULT_ROUTE_COOLDOWN_SEC), DEFAULT_ROUTE_COOLDOWN_SEC),
            }
        clean['routes'] = clean_routes

    templates_cfg = payload.get('templates')
    if isinstance(templates_cfg, dict):
        clean_templates = {}
        for tname, tdata in templates_cfg.items():
            if not isinstance(tdata, dict):
                continue
            elems = tdata.get('elements')
            if not isinstance(elems, list):
                continue
            normalized_elems = []
            for elem in elems:
                if not isinstance(elem, dict):
                    continue
                field = str(elem.get('field') or elem.get('name') or '').strip()
                if not field:
                    continue
                normalized = {
                    'field': field,
                    'align': str(elem.get('align', 'left') or 'left'),
                    'style': str(elem.get('style', 'normal') or 'normal'),
                    'col': _as_int(elem.get('col', 0), 0),
                }
                if elem.get('text') is not None:
                    normalized['text'] = str(elem.get('text') or '')
                if elem.get('label') is not None:
                    normalized['label'] = str(elem.get('label') or '')
                for key in ('order', 'x', 'y', 'width', 'height'):
                    if elem.get(key) is None:
                        continue
                    normalized[key] = _as_int(elem.get(key), 0)
                normalized_elems.append(normalized)
            if normalized_elems:
                normalized_template = {'elements': normalized_elems}
                if tdata.get('canvas_width') is not None:
                    normalized_template['canvas_width'] = _as_int(tdata.get('canvas_width'), _EDITOR_CANVAS_WIDTH)
                clean_templates[str(tname)] = normalized_template
        if clean_templates:
            clean['templates'] = clean_templates

    return clean


def save_local_config(payload):
    clean = _sanitize_runtime_payload(payload)
    if clean is None:
        raise ValueError('Invalid configuration payload')
    json_value = json.dumps(clean, ensure_ascii=True)

    if winreg is not None:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, WINDOWS_REGISTRY_PATH)
        winreg.SetValueEx(key, 'ConfigJson', 0, winreg.REG_SZ, json_value)
        winreg.CloseKey(key)
        return f'HKCU\\{WINDOWS_REGISTRY_PATH}'

    path = _config_file_path()
    with open(path, 'w', encoding='utf-8') as f:
        f.write(json_value)
    return path


def load_local_config():
    if winreg is not None:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_REGISTRY_PATH)
            raw, _typ = winreg.QueryValueEx(key, 'ConfigJson')
            winreg.CloseKey(key)
            payload = json.loads(raw)
            clean = _sanitize_runtime_payload(payload)
            if clean:
                changed = _apply_remote_config_payload(clean)
                if changed:
                    logger.info('Loaded local config from registry HKCU\\%s', WINDOWS_REGISTRY_PATH)
                return changed
            return False
        except FileNotFoundError:
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error('Failed to load local config from registry: %s', exc)
            return False

    path = _config_file_path()
    if not os.path.exists(path):
        return False
    try:
        with open(path, 'r', encoding='utf-8') as f:
            payload = json.load(f)
        clean = _sanitize_runtime_payload(payload)
        if clean:
            changed = _apply_remote_config_payload(clean)
            if changed:
                logger.info('Loaded local config from %s', path)
            return changed
    except Exception as exc:  # noqa: BLE001
        logger.error('Failed to load local config %s: %s', path, exc)
    return False


def _trigger_self_restart(delay_sec=0.8):
    def _do_restart():
        time.sleep(delay_sec)
        logger.warning('Restart requested from control API. Restarting process...')
        python = sys.executable
        os.execl(python, python, *sys.argv)
    threading.Thread(target=_do_restart, daemon=True).start()


def _read_log_tail(max_lines=200):
    path = LOG_FILE
    if not path:
        return ''
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if not os.path.exists(path):
        return ''
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
    return ''.join(lines[-max(1, int(max_lines)):])


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


def _runtime_templates():
    with _runtime_lock:
        templates = _runtime_config.get('templates') or {}
        return json.loads(json.dumps(templates))


def _apply_remote_config_payload(payload):
    if not isinstance(payload, dict):
        return False

    routes = payload.get('routes') if isinstance(payload.get('routes'), dict) else None
    defaults = payload.get('default') if isinstance(payload.get('default'), dict) else None
    odoo_settings = payload.get('odoo') if isinstance(payload.get('odoo'), dict) else None
    templates = payload.get('templates') if isinstance(payload.get('templates'), dict) else None
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

        if templates is not None:
            normalized_templates = _default_templates()
            for tname, tdata in templates.items():
                if not isinstance(tdata, dict):
                    continue
                elems = tdata.get('elements')
                if isinstance(elems, list):
                    normalized_template = {'elements': [dict(e) for e in elems if isinstance(e, dict)]}
                    if tdata.get('canvas_width') is not None:
                        normalized_template['canvas_width'] = _as_int(tdata.get('canvas_width'), _EDITOR_CANVAS_WIDTH)
                    normalized_templates[str(tname)] = normalized_template
            if normalized_templates != (_runtime_config.get('templates') or {}):
                _runtime_config['templates'] = normalized_templates
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
        # Do a non-blocking startup attempt so local HTTP API can come up even
        # if Odoo is temporarily unavailable or credentials are wrong.
        self._connect(retry_forever=False)

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

    def ensure_connected(self, retry_forever=False):
        if self.models and self.uid and self.active_db and self.active_password:
            return True
        return self._connect(retry_forever=retry_forever)

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
        if not self.ensure_connected(retry_forever=False):
            raise ConnectionError(
                f'Odoo not connected ({self.desired_url}, db={self.desired_db}). '
                'Check Odoo URL/database/user/password.'
            )
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
            self.models = None
            self.uid = None
            if not self._connect(retry_forever=False):
                raise
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
    # Business rule: default fallback is only for customer receipts.
    # Kitchen/order tickets must have an explicit route configured.
    if printer_type != 'receipt' and not route:
        target = route_name or printer_name or 'unmapped'
        raise RuntimeError(
            f'No explicit printer route configured for "{target}". '
            'Default fallback is reserved for receipt printing only.'
        )

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
    qty_segment = f' {qty}' if qty else ''
    reserved = len(qty_segment) + len(price) + 1
    name_width = max(8, width - reserved)
    display_name = _truncate(name, name_width)
    spaces = ' ' * max(1, width - len(display_name) - len(qty_segment) - len(price))
    return f'{display_name}{qty_segment}{spaces}{price}'


def _fit_name_qty(name, qty, width=40):
    name = str(name or '')
    qty = str(qty or '')
    lines = _wrap_left_with_right(name, qty, width=width)
    return '\n'.join(lines)


def _wrap_words(text, width):
    text = str(text or '').strip()
    width = max(1, int(width))
    if not text:
        return ['']

    words = text.split()
    if not words:
        return [_truncate(text, width)]

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

    normalized = []
    for line in lines:
        if len(line) <= width:
            normalized.append(line)
            continue
        remainder = line
        while len(remainder) > width:
            normalized.append(remainder[:width])
            remainder = remainder[width:]
        if remainder:
            normalized.append(remainder)
    return normalized or ['']


def _wrap_left_with_right(left, right, width=42):
    left_text = str(left or '').strip()
    right_text = str(right or '').strip()
    width = max(8, int(width))

    if not right_text:
        return _wrap_words(left_text, width)

    reserved = len(right_text) + 1
    first_line_width = max(6, width - reserved)
    wrapped_left = _wrap_words(left_text, first_line_width)
    if not wrapped_left:
        wrapped_left = ['']

    first = wrapped_left[0]
    spaces = ' ' * max(1, width - len(first) - len(right_text))
    lines = [f'{first}{spaces}{right_text}']
    lines.extend(_wrap_words(line, width) for line in wrapped_left[1:])

    flattened = []
    for line in lines:
        if isinstance(line, list):
            flattened.extend(line)
        else:
            flattened.append(line)
    return flattened


def _wrap_left_with_column(left, right, column=24, width=42, gap=2):
    left_text = str(left or '').strip()
    right_text = str(right or '').strip()
    width = max(8, int(width))
    column = max(6, min(int(column), width - 1))
    gap = max(1, int(gap))

    if not right_text:
        return _wrap_words(left_text, width)

    left_width = max(6, column - gap)
    wrapped_left = _wrap_words(left_text, left_width)
    if not wrapped_left:
        wrapped_left = ['']

    lines = []
    for index, left_line in enumerate(wrapped_left):
        if index == 0:
            spaces = ' ' * max(gap, column - len(left_line))
            lines.append(f'{left_line}{spaces}{right_text}')
        else:
            lines.append(left_line)
    return lines


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


def _is_placeholder_label(value):
    text = str(value or '').strip().lower()
    return text in {'n/a', 'na', '-', '--', 'none', 'null'}


def _looks_like_synthetic_takeout_label(value):
    text = str(value or '').strip()
    return bool(re.fullmatch(r'\d+\s*x', text, flags=re.IGNORECASE))


def _resolve_table_label(payload):
    explicit_table = _first_non_empty(
        payload.get('table_id', {}).get('table_number') if isinstance(payload.get('table_id'), dict) else None,
        payload.get('table_id', {}).get('name') if isinstance(payload.get('table_id'), dict) else None,
        payload.get('table_number'),
        payload.get('table'),
    )
    generic_table = _first_non_empty(
        payload.get('table_name'),
        payload.get('table'),
    )
    raw_table = explicit_table or generic_table
    takeout_name = _first_non_empty(payload.get('takeout_name'))
    if takeout_name and (_is_placeholder_label(raw_table) or _looks_like_synthetic_takeout_label(raw_table)):
        return takeout_name or raw_table
    return raw_table or takeout_name


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


_KITCHEN_PRIORITY_QTY_KEYS = (
    'delta',
    'qty_delta',
    'qtyDelta',
    'change',
    'change_qty',
    'changeQty',
    'difference',
    'diff',
    'removed_qty',
    'removedQty',
    'cancelled_qty',
    'cancelledQty',
    'canceled_qty',
    'canceledQty',
    'decrease_qty',
    'decreaseQty',
    'decreased_qty',
    'decreasedQty',
)
_KITCHEN_FALLBACK_QTY_KEYS = (
    'qty',
    'quantity',
    'qty_done',
    'count',
    'new_qty',
    'newQty',
    'amount',
)
_KITCHEN_PRIORITY_QTY_KEY_SET = set(_KITCHEN_PRIORITY_QTY_KEYS)
_KITCHEN_NEGATIVE_MARKERS = {
    'cancelled',
    'canceled',
    'removed',
    'remove',
    'delete',
    'deleted',
    'cxl',
    'minus',
    'negative',
    'decrease',
    'decreased',
    'reduce',
    'reduced',
    'less',
    'decrement',
    'decremented',
    'subtract',
    'subtracted',
}
_KITCHEN_NEGATIVE_TOKENS = (
    'cancel',
    'cxl',
    'remove',
    'delete',
    'minus',
    'negative',
    'decrease',
    'reduce',
    'decrement',
    'subtract',
    'less',
)
_KITCHEN_SEMANTIC_QTY_TOKENS = (
    'qty',
    'quantity',
    'count',
    'delta',
    'change',
    'diff',
    'difference',
    'decrease',
    'decrement',
    'reduce',
    'remove',
    'cancel',
)
_KITCHEN_OLD_QTY_TOKENS = ('old', 'prev', 'previous', 'before', 'from')
_KITCHEN_NEW_QTY_TOKENS = ('new', 'current', 'after', 'to')
_KITCHEN_EXCLUDED_QTY_TOKENS = ('price', 'cost', 'tax', 'total', 'subtotal', 'discount')


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


def _canonical_change_section(changes):
    if not isinstance(changes, dict):
        return ''

    section = _first_non_empty(
        changes.get('section'),
        changes.get('change_section'),
    ).replace(' ', '').strip().lower()
    if section in ('cancelled', 'canceled'):
        return 'cancelled'
    if section == 'new':
        return 'new'
    if section in ('noteupdate', 'note_update'):
        return 'noteUpdate'

    title = _first_non_empty(changes.get('title')).replace(' ', '').strip().lower()
    if title in ('cancelled', 'canceled'):
        return 'cancelled'
    if title == 'new':
        return 'new'
    if title == 'noteupdate':
        return 'noteUpdate'
    return ''


def _normalize_signed_change_line(line, section_name=''):
    if not isinstance(line, dict):
        return None

    raw_qty = line.get('qty')
    if raw_qty is None:
        raw_qty = line.get('quantity')
    if raw_qty is None:
        raw_qty = line.get('qty_done')
    if raw_qty is None:
        raw_qty = line.get('count')
    if raw_qty is None:
        raw_qty = line.get('amount')

    qty = _parse_qty(raw_qty, default=1.0)
    section = str(section_name or line.get('change_section') or '').strip().lower()
    if section in ('cancelled', 'canceled') and qty > 0:
        qty = -qty

    normalized = dict(line)
    normalized['qty'] = qty
    normalized['quantity'] = qty
    if section:
        normalized['change_section'] = section
    return normalized


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


def _collect_signed_change_lines(changes):
    if isinstance(changes, dict):
        signed_lines = changes.get('signed_lines')
        if isinstance(signed_lines, list):
            return [
                normalized
                for normalized in (_normalize_signed_change_line(line) for line in signed_lines)
                if normalized is not None
            ]

        section = _canonical_change_section(changes)
        data_lines = _as_line_list(changes.get('data') or [])
        if data_lines:
            return [
                normalized
                for normalized in (
                    _normalize_signed_change_line(line, section_name=section) for line in data_lines
                )
                if normalized is not None
            ]

        combined = []
        for section_name in ('new', 'cancelled', 'noteUpdate'):
            combined.extend(
                normalized
                for normalized in (
                    _normalize_signed_change_line(line, section_name=section_name)
                    for line in _as_line_list(changes.get(section_name) or [])
                )
                if normalized is not None
            )
        return combined

    if isinstance(changes, list):
        return [
            normalized
            for normalized in (_normalize_signed_change_line(line) for line in changes)
            if normalized is not None
        ]

    return []


def _build_kitchen_lines(payload):
    return [
        {'text': row.get('text', ''), 'style': row.get('style', 'normal')}
        for row in _build_kitchen_rows(payload)
    ]


def _build_kitchen_rows(payload):
    changes = payload.get('changes', {})
    raw_lines = _collect_signed_change_lines(changes)
    if not raw_lines:
        raw_lines = [
            normalized
            for normalized in (
                _normalize_signed_change_line(line)
                for line in _as_line_list(payload.get('orderlines') or [])
            )
            if normalized is not None
        ]

    rendered = []
    for line in raw_lines:
        product_name = _extract_product_name(line)
        if not product_name:
            continue

        note = _extract_line_note(line)
        qty = _parse_qty(line.get('qty'), default=_parse_qty(line.get('quantity'), default=1.0))
        if abs(qty) < 1e-9:
            continue

        logger.info('[KITCHEN-DELTA] %s | qty=%s', product_name, _display_qty(qty))

        qty_str = _display_qty(qty)
        rendered.append({
            'left': product_name,
            'right': qty_str,
            'text': _fit_name_qty(product_name, qty_str, width=40),
            'style': 'normal',
        })
        if note:
            rendered.append({'left': f" >> {note}", 'right': '', 'text': f" >> {note}", 'style': 'normal'})

    return rendered


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


def _template_definition(ticket_type):
    templates = _runtime_templates()
    defaults = _default_templates()
    template = templates.get(ticket_type) if isinstance(templates, dict) else None
    if not isinstance(template, dict):
        template = defaults.get(ticket_type, {})
    return dict(template)


def _template_elements(ticket_type):
    template = _template_definition(ticket_type)
    defaults = _default_templates()
    elements = template.get('elements')
    if not isinstance(elements, list) or not elements:
        elements = defaults.get(ticket_type, {}).get('elements', [])
    return [dict(elem) for elem in elements if isinstance(elem, dict)]


def _set_style(printer, elem):
    align = str(elem.get('align', 'left') or 'left').lower()
    if align not in ('left', 'center', 'right'):
        align = 'left'
    style = str(elem.get('style', 'normal') or 'normal').lower()
    if style == 'huge':
        printer.set(align=align, font='a', bold=True, height=4, width=4)
    elif style == 'double':
        printer.set(align=align, font='a', bold=True, height=2, width=2)
    elif style == 'bold':
        printer.set(align=align, font='a', bold=True, height=1, width=1)
    else:
        printer.set(align=align, font='a', bold=False, height=1, width=1)
    return align


def _emit_template_line(printer, text, elem, width=42):
    align = _set_style(printer, elem)
    col = max(0, _as_int(elem.get('col', 0), 0))
    line = str(text or '')
    if align == 'left' and col > 0:
        line = (' ' * col) + line
    if len(line) > width:
        line = line[:width]
    printer.text(line + '\n')


def _emit_template_rows(printer, rows, elem, width=42):
    line_elem = dict(elem or {})
    for row in rows or []:
        row_style = row.get('style')
        if row_style:
            line_elem['style'] = row_style
        if row.get('right_align') == 'column_left':
            wrapped_lines = _wrap_left_with_column(
                row.get('left', ''),
                row.get('right', ''),
                column=row.get('column', 24),
                width=width,
                gap=row.get('gap', 2),
            )
        else:
            wrapped_lines = _wrap_left_with_right(row.get('left', ''), row.get('right', ''), width=width)
        for line in wrapped_lines:
            _emit_template_line(printer, line, line_elem, width=width)


def _template_uses_visual_layout(ticket_type):
    for elem in _template_elements(ticket_type):
        if not isinstance(elem, dict):
            continue
        if str(elem.get('field') or '').strip().lower() == 'static_text':
            return True
        if any(elem.get(key) is not None for key in ('x', 'y', 'width', 'height')):
            return True
    return False


def _visual_source_canvas_width(ticket_type, elements):
    template = _template_definition(ticket_type)
    configured_width = _as_int(template.get('canvas_width'), 0)
    if configured_width > 0:
        return configured_width

    max_right = 0
    has_geometry = False
    for elem in elements:
        if not isinstance(elem, dict):
            continue
        x = elem.get('x')
        width = elem.get('width')
        if x is None or width is None:
            continue
        has_geometry = True
        max_right = max(max_right, _as_int(x, 0) + _as_int(width, 0))

    if has_geometry and max_right <= (_LEGACY_EDITOR_CANVAS_WIDTH + _EDITOR_CANVAS_PADDING):
        return _LEGACY_EDITOR_CANVAS_WIDTH
    return _EDITOR_CANVAS_WIDTH


def _scaled_visual_elem(elem, source_canvas_width, target_canvas_width):
    if source_canvas_width <= 0 or source_canvas_width == target_canvas_width:
        return dict(elem)

    scale_x = float(target_canvas_width) / float(source_canvas_width)
    normalized = dict(elem)
    for key in ('x', 'width'):
        if elem.get(key) is None:
            continue
        normalized[key] = max(0, int(round(_as_int(elem.get(key), 0) * scale_x)))
    return normalized


def _elem_label(elem, default_label=''):
    if isinstance(elem, dict):
        custom = str(elem.get('label') or '').strip()
        if custom:
            return custom
    return str(default_label or '')


def _label_value_text(label, value):
    label_text = str(label or '').strip()
    value_text = str(value or '').strip()
    if not label_text:
        return value_text
    if not value_text:
        return label_text
    return f'{label_text} : {value_text}'


def _field_font_cap(field, style='normal'):
    style_key = str(style or 'normal').lower()
    default_cap = {'normal': 24, 'bold': 26, 'double': 34, 'huge': 58}.get(style_key, 24)
    field_key = str(field or '').strip().lower()
    compact_fields = {
        'items_block',
        'lines_block',
        'payments_block',
        'printer_line',
        'table_line',
        'order_line',
        'time_line',
        'waiter_line',
        'date_line',
        'cashier_line',
        'subtotal_line',
        'tax_line',
        'total_line',
        'order_name_line',
    }
    emphasis_fields = {'company_name', 'tracking_number', 'table_big', 'ticket_title'}
    if field_key in compact_fields:
        return {'normal': 20, 'bold': 22, 'double': 26, 'huge': 36}.get(style_key, 20)
    if field_key == 'static_text':
        return {'normal': 22, 'bold': 24, 'double': 30, 'huge': 54}.get(style_key, 22)
    if field_key in emphasis_fields:
        return {'normal': 28, 'bold': 30, 'double': 38, 'huge': 112}.get(style_key, 28)
    return default_cap


def _printer_media_width_px(printer, default=_VISUAL_PAPER_FALLBACK_WIDTH):
    try:
        width_value = printer.profile.profile_data['media']['width']['pixels']
        width_num = int(width_value)
        if width_num >= 512:
            return width_num
        if width_num > 0:
            logger.warning('Printer profile width %spx looks narrow for 80mm paper; using %spx visual render width instead', width_num, default)
            return default
    except Exception:
        pass
    return default


def _visual_block_rect(elem, cursor_y, canvas_width=_EDITOR_CANVAS_WIDTH):
    width = _as_int(elem.get('width', canvas_width - (_EDITOR_CANVAS_PADDING * 2)), canvas_width - (_EDITOR_CANVAS_PADDING * 2))
    width = max(_VISUAL_MIN_BLOCK_WIDTH, min(width, canvas_width - (_EDITOR_CANVAS_PADDING * 2)))
    height = _as_int(elem.get('height', 42), 42)
    height = max(_VISUAL_MIN_BLOCK_HEIGHT, min(height, 720))

    align = str(elem.get('align', 'left') or 'left').lower()
    col = max(0, _as_int(elem.get('col', 0), 0))

    if align == 'center':
        default_x = max(_EDITOR_CANVAS_PADDING, int((canvas_width - width) / 2))
    elif align == 'right':
        default_x = canvas_width - width - _EDITOR_CANVAS_PADDING
    else:
        default_x = _EDITOR_CANVAS_PADDING + (col * 8)

    default_x = max(_EDITOR_CANVAS_PADDING, min(default_x, canvas_width - width - _EDITOR_CANVAS_PADDING))
    x = _as_int(elem.get('x', default_x), default_x)
    x = max(_EDITOR_CANVAS_PADDING, min(x, canvas_width - width - _EDITOR_CANVAS_PADDING))

    y = _as_int(elem.get('y', cursor_y), cursor_y)
    y = max(_EDITOR_CANVAS_PADDING, y)

    return x, y, width, height, max(cursor_y, y + height + 6)


def _load_visual_font(style, size):
    if ImageFont is None:
        raise RuntimeError('Pillow is not installed')

    style_key = str(style or 'normal').lower()
    size_key = max(8, int(size))
    cache_key = (style_key, size_key)
    cached = _font_cache.get(cache_key)
    if cached is not None:
        return cached

    candidates = []
    if os.name == 'nt':
        if style_key in ('bold', 'double', 'huge'):
            candidates.extend([
                r'C:\Windows\Fonts\consolab.ttf',
                r'C:\Windows\Fonts\courbd.ttf',
                r'C:\Windows\Fonts\lucon.ttf',
            ])
        candidates.extend([
            r'C:\Windows\Fonts\consola.ttf',
            r'C:\Windows\Fonts\cour.ttf',
            r'C:\Windows\Fonts\lucon.ttf',
        ])

    for font_path in candidates:
        if not os.path.exists(font_path):
            continue
        try:
            font = ImageFont.truetype(font_path, size_key)
            _font_cache[cache_key] = font
            return font
        except Exception:
            continue

    font = ImageFont.load_default()
    _font_cache[cache_key] = font
    return font


def _measure_text_width(draw, text, font):
    if not text:
        return 0
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return max(0, right - left)


def _measure_line_height(draw, font):
    left, top, right, bottom = draw.textbbox((0, 0), 'Ag', font=font)
    return max(1, (bottom - top))


def _field_allows_dynamic_height(field):
    return str(field or '').strip().lower() in {'items_block', 'lines_block', 'payments_block'}


def _style_font_metrics(style, max_height, field=''):
    style_key = str(style or 'normal').lower()
    scale = {
        'normal': 0.56,
        'bold': 0.60,
        'double': 0.82,
        'huge': 2.05,
    }.get(style_key, 0.56)
    size_ceiling = 140 if style_key == 'huge' else 96
    max_font_size = max(10, min(size_ceiling, int(max_height * scale)))
    max_font_size = min(max_font_size, _field_font_cap(field, style_key))
    min_font_size = 8 if style_key == 'normal' else 10
    return style_key, max_font_size, min_font_size


def _text_block_padding(width, height, style):
    style_key = str(style or 'normal').lower()
    if style_key == 'huge':
        return max(2, int(width * 0.015)), max(1, int(height * 0.02))
    return max(4, int(width * 0.04)), max(3, int(height * 0.10))


def _wrap_text_for_block(draw, text, font, max_width):
    raw_lines = str(text or '').replace('\r\n', '\n').split('\n')
    if not raw_lines:
        return ['']

    wrapped = []
    for raw_line in raw_lines:
        if raw_line == '':
            wrapped.append('')
            continue
        remaining = raw_line
        while remaining:
            if _measure_text_width(draw, remaining, font) <= max_width:
                wrapped.append(remaining)
                break
            cut = len(remaining)
            while cut > 1 and _measure_text_width(draw, remaining[:cut], font) > max_width:
                cut -= 1
            if cut <= 0:
                cut = 1
            wrapped.append(remaining[:cut])
            remaining = remaining[cut:]
    return wrapped or ['']


def _fit_text_to_block(draw, text, style, max_width, max_height, field=''):
    max_width = max(12, int(max_width))
    max_height = max(12, int(max_height))

    style_key, max_font_size, min_font_size = _style_font_metrics(style, max_height, field=field)

    fallback = None
    for size in range(max_font_size, min_font_size - 1, -1):
        font = _load_visual_font(style_key, size)
        lines = _wrap_text_for_block(draw, text, font, max_width)
        line_height = _measure_line_height(draw, font) + max(2, int(size * 0.2))
        total_height = line_height * max(1, len(lines))
        if total_height <= max_height:
            return font, lines, line_height
        fallback = (font, lines, line_height)

    if fallback is not None:
        return fallback
    font = _load_visual_font(style_key, min_font_size)
    return font, _wrap_text_for_block(draw, text, font, max_width), _measure_line_height(draw, font) + 2


def _fit_text_with_growth(draw, text, style, max_width, base_height, field=''):
    max_width = max(12, int(max_width))
    base_height = max(12, int(base_height))
    style_key, max_font_size, min_font_size = _style_font_metrics(style, base_height, field=field)

    for size in range(max_font_size, min_font_size - 1, -1):
        font = _load_visual_font(style_key, size)
        lines = _wrap_text_for_block(draw, text, font, max_width)
        line_height = _measure_line_height(draw, font) + max(2, int(size * 0.2))
        if lines:
            return font, lines, line_height

    font = _load_visual_font(style_key, min_font_size)
    return font, _wrap_text_for_block(draw, text, font, max_width), _measure_line_height(draw, font) + 2


def _draw_separator_block(draw, rect):
    left, top, width, height = rect
    y = top + max(1, int(height / 2))
    dash = 9
    gap = 5
    x = left + 4
    right = left + width - 4
    while x < right:
        x2 = min(right, x + dash)
        draw.line((x, y, x2, y), fill=0, width=1)
        x = x2 + gap


def _prepare_column_rows(draw, rows, style, width, height, field=''):
    width = max(12, int(width))
    height = max(12, int(height))
    style_key, max_font_size, min_font_size = _style_font_metrics(style, height, field=field)
    gap = 12

    for size in range(max_font_size, min_font_size - 1, -1):
        font = _load_visual_font(style_key, size)
        line_height = _measure_line_height(draw, font) + max(2, int(size * 0.2))
        right_width = 0
        for row in rows:
            right_width = max(right_width, _measure_text_width(draw, row.get('right', ''), font))

        prepared = []
        for row in rows:
            right_text = str(row.get('right', '') or '')
            align_mode = str(row.get('right_align', 'edge_right') or 'edge_right').lower()
            if right_text:
                if align_mode == 'column_left':
                    ratio = _as_float(row.get('column_ratio', 0.58), 0.58)
                    ratio = max(0.28, min(ratio, 0.80))
                    right_x = max(24, min(int(width * ratio), max(24, width - _measure_text_width(draw, right_text, font))))
                else:
                    right_x = max(24, width - right_width)
            else:
                right_x = width
            available_left = max(8, right_x - gap)
            wrapped_left = _wrap_text_for_block(draw, row.get('left', ''), font, available_left)
            if not wrapped_left:
                wrapped_left = ['']
            for index, left_line in enumerate(wrapped_left):
                prepared.append({
                    'left': left_line,
                    'right': right_text if index == 0 else '',
                    'right_x': right_x,
                })
        if prepared:
            return font, prepared, line_height, right_width

    font = _load_visual_font(style_key, min_font_size)
    line_height = _measure_line_height(draw, font) + 2
    prepared = []
    for row in rows:
        right_text = str(row.get('right', '') or '')
        align_mode = str(row.get('right_align', 'edge_right') or 'edge_right').lower()
        if right_text and align_mode == 'column_left':
            ratio = _as_float(row.get('column_ratio', 0.58), 0.58)
            ratio = max(0.28, min(ratio, 0.80))
            right_x = max(24, min(int(width * ratio), max(24, width - _measure_text_width(draw, right_text, font))))
        elif right_text:
            right_x = max(24, width - _measure_text_width(draw, right_text, font))
        else:
            right_x = width
        prepared.append({
            'left': str(row.get('left', '')),
            'right': right_text,
            'right_x': right_x,
        })
    right_width = max((_measure_text_width(draw, row.get('right', ''), font) for row in rows), default=0)
    return font, prepared, line_height, right_width


def _draw_column_rows_block(draw, rect, rows, style='normal', field=''):
    if not rows:
        return

    left, top, width, height = rect
    pad_x, pad_y = _text_block_padding(width, height, style)
    content_width = max(12, width - (pad_x * 2))
    content_height = max(12, height - (pad_y * 2))
    font, prepared, line_height, right_width = _prepare_column_rows(
        draw,
        rows,
        style,
        content_width,
        content_height,
        field=field,
    )
    total_height = line_height * max(1, len(prepared))
    current_y = top + pad_y + max(0, int((content_height - total_height) / 2))
    for row in prepared:
        left_text = row.get('left', '')
        right_text = row.get('right', '')
        draw.text((left + pad_x, current_y), left_text, fill=0, font=font)
        if right_text:
            draw.text((left + pad_x + int(row.get('right_x', content_width - right_width)), current_y), right_text, fill=0, font=font)
        current_y += line_height


def _draw_text_block(draw, rect, text, align='left', style='normal', field=''):
    if text is None:
        return

    left, top, width, height = rect
    pad_x, pad_y = _text_block_padding(width, height, style)
    content_width = max(12, width - (pad_x * 2))
    content_height = max(12, height - (pad_y * 2))

    if _field_allows_dynamic_height(field):
        font, lines, line_height = _fit_text_with_growth(draw, str(text), style, content_width, content_height, field=field)
    else:
        font, lines, line_height = _fit_text_to_block(draw, str(text), style, content_width, content_height, field=field)
    total_height = line_height * max(1, len(lines))
    current_y = top + pad_y + max(0, int((content_height - total_height) / 2))

    align_key = str(align or 'left').lower()
    for line in lines:
        line_width = _measure_text_width(draw, line, font)
        if align_key == 'center':
            x = left + int((width - line_width) / 2)
        elif align_key == 'right':
            x = left + width - pad_x - line_width
        else:
            x = left + pad_x
        draw.text((x, current_y), line, fill=0, font=font)
        current_y += line_height


def _layout_visual_blocks(draw, blocks, scale):
    ordered_blocks = sorted(blocks, key=lambda block: (block.get('y', 0), block.get('x', 0), block.get('order', 0)))
    laid_out_blocks = []
    cumulative_shift = 0

    for block in ordered_blocks:
        rect = (
            int(block['x'] * scale),
            int(block['y'] * scale) + cumulative_shift,
            max(12, int(block['width'] * scale)),
            max(12, int(block['height'] * scale)),
        )

        if _field_allows_dynamic_height(block.get('field')):
            pad_x, pad_y = _text_block_padding(rect[2], rect[3], block.get('style', 'normal'))
            content_width = max(12, rect[2] - (pad_x * 2))
            content_height = max(12, rect[3] - (pad_y * 2))
            if block.get('rows'):
                _font, prepared, line_height, _right_width = _prepare_column_rows(
                    draw,
                    block.get('rows', []),
                    block.get('style', 'normal'),
                    content_width,
                    content_height,
                    field=block.get('field', ''),
                )
                needed_height = max(rect[3], (pad_y * 2) + (line_height * max(1, len(prepared))) + 2)
            else:
                _font, lines, line_height = _fit_text_with_growth(
                    draw,
                    str(block.get('text', '')),
                    block.get('style', 'normal'),
                    content_width,
                    content_height,
                    field=block.get('field', ''),
                )
                needed_height = max(rect[3], (pad_y * 2) + (line_height * max(1, len(lines))) + 2)
            if needed_height > rect[3]:
                extra_height = needed_height - rect[3]
                rect = (rect[0], rect[1], rect[2], needed_height)
                cumulative_shift += extra_height

        laid_out_blocks.append({**block, 'rect': rect})

    max_bottom = max((entry['rect'][1] + entry['rect'][3]) for entry in laid_out_blocks) if laid_out_blocks else 120
    return laid_out_blocks, max_bottom


def _render_visual_blocks(printer, blocks):
    if Image is None or ImageDraw is None:
        raise RuntimeError('Pillow is not installed')

    paper_width = _printer_media_width_px(printer)
    scale = float(paper_width) / float(_EDITOR_CANVAS_WIDTH)
    metrics_image = Image.new('L', (paper_width, 64), 255)
    metrics_draw = ImageDraw.Draw(metrics_image)
    laid_out_blocks, max_bottom = _layout_visual_blocks(metrics_draw, blocks, scale)
    paper_height = max(180, max_bottom + 32)

    image = Image.new('L', (paper_width, paper_height), 255)
    draw = ImageDraw.Draw(image)
    laid_out_blocks, max_bottom = _layout_visual_blocks(draw, blocks, scale)

    for entry in laid_out_blocks:
        block = entry
        rect = entry['rect']
        if block.get('field') == 'blank':
            continue
        if block.get('field') == 'separator':
            _draw_separator_block(draw, rect)
            continue
        if block.get('rows'):
            _draw_column_rows_block(
                draw,
                rect,
                block.get('rows', []),
                style=block.get('style', 'normal'),
                field=block.get('field', ''),
            )
            continue
        _draw_text_block(
            draw,
            rect,
            block.get('text', ''),
            align=block.get('align', 'left'),
            style=block.get('style', 'normal'),
            field=block.get('field', ''),
        )

    crop_height = min(image.height, max_bottom + 4)
    image = image.crop((0, 0, image.width, max(48, crop_height)))
    final_image = image.point(lambda value: 0 if value < 192 else 255, mode='1')

    printer.set(align='left', font='a', bold=False, height=1, width=1)
    try:
        printer.image(final_image, impl='bitImageRaster', center=False)
    except Exception:
        printer.image(final_image, impl='bitImageColumn', center=False)


def _build_receipt_lines(payload, currency_symbol):
    return [
        {'text': row.get('text', ''), 'style': row.get('style', 'normal')}
        for row in _build_receipt_rows(payload, currency_symbol)
    ]


def _build_receipt_rows(payload, currency_symbol):
    lines = []
    for line in payload.get('lines', []):
        qty_text = _display_qty(line.get('qty', 0))
        price_text = line.get('price_display') or _money(line.get('price', 0), currency_symbol)
        name_qty = line.get('name', '')
        if qty_text:
            name_qty = f'{name_qty} x {qty_text}'
        lines.append({
            'left': name_qty,
            'right': price_text,
            'right_align': 'edge_right',
            'text': _wrap_left_with_right(name_qty, price_text, width=48)[0] if price_text else name_qty,
            'style': 'normal',
        })
        unit_price_display = line.get('unit_price_display')
        if unit_price_display:
            lines.append({'left': f'  {unit_price_display}', 'right': '', 'text': f'  {unit_price_display}', 'style': 'normal'})
    return lines


def _build_receipt_template_context(payload):
    currency_symbol = payload.get('currency_symbol', '')
    table_label = _resolve_table_label(payload) or '-'
    guest_label = payload.get('customer_count') or '-'
    is_takeout_name = bool(payload.get('takeout_name')) and table_label == str(payload.get('takeout_name') or '').strip()
    elements = _template_elements('receipt')
    label_overrides = {
        str(elem.get('field') or '').strip(): str(elem.get('label') or '').strip()
        for elem in elements
        if isinstance(elem, dict) and str(elem.get('label') or '').strip()
    }

    receipt_rows = _build_receipt_rows(payload, currency_symbol)
    receipt_lines = [{'text': row.get('text', ''), 'style': row.get('style', 'normal')} for row in receipt_rows]
    payments_rows = []
    for payment in payload.get('payments', []):
        payments_rows.append({
            'left': payment.get('name', 'Payment'),
            'right': payment.get('amount_display') or _money(payment.get('amount', 0), currency_symbol),
            'right_align': 'column_left',
            'column_ratio': 0.32,
            'column': 16,
            'gap': 2,
            'text': _wrap_left_with_column(
                payment.get('name', 'Payment'),
                payment.get('amount_display') or _money(payment.get('amount', 0), currency_symbol),
                column=16,
                width=48,
                gap=2,
            )[0],
            'style': 'normal',
        })
    summary_rows = {
        'subtotal_line': [{
            'left': label_overrides.get('subtotal_line', 'Subtotal'),
            'right': _money(payload.get('subtotal', 0), currency_symbol),
            'right_align': 'column_left',
            'column_ratio': 0.32,
            'column': 16,
            'gap': 2,
            'text': _wrap_left_with_column(
                label_overrides.get('subtotal_line', 'Subtotal'),
                _money(payload.get('subtotal', 0), currency_symbol),
                column=16,
                width=48,
                gap=2,
            )[0],
            'style': 'normal',
        }],
        'tax_line': [{
            'left': label_overrides.get('tax_line', 'Tax'),
            'right': _money(payload.get('tax', 0), currency_symbol),
            'right_align': 'column_left',
            'column_ratio': 0.32,
            'column': 16,
            'gap': 2,
            'text': _wrap_left_with_column(
                label_overrides.get('tax_line', 'Tax'),
                _money(payload.get('tax', 0), currency_symbol),
                column=16,
                width=48,
                gap=2,
            )[0],
            'style': 'normal',
        }],
        'total_line': [{
            'left': label_overrides.get('total_line', 'Total'),
            'right': _money(payload.get('total', 0), currency_symbol),
            'right_align': 'column_left',
            'column_ratio': 0.32,
            'column': 16,
            'gap': 2,
            'text': _wrap_left_with_column(
                label_overrides.get('total_line', 'Total'),
                _money(payload.get('total', 0), currency_symbol),
                column=16,
                width=48,
                gap=2,
            )[0],
            'style': 'normal',
        }],
    }

    context = {
        'company_name': payload.get('company_name', 'Odoo POS'),
        'order_name_line': f"{label_overrides.get('order_name_line', 'Ticket')} {payload.get('order_name', '')}".strip() if payload.get('order_name') else '',
        'date_line': f"{payload.get('date', '')[:19].replace('T', ' ')}" if payload.get('date') else '',
        'cashier_line': _label_value_text(label_overrides.get('cashier_line', 'Served by'), payload.get('cashier', '')) if payload.get('cashier') else '',
        'table_guests_line': f"Table: {table_label}" if table_label else '',
        'tracking_number': payload.get('tracking_number') or '',
        'subtotal_line': summary_rows['subtotal_line'][0]['text'],
        'tax_line': summary_rows['tax_line'][0]['text'],
        'total_line': summary_rows['total_line'][0]['text'],
        'lines_block': '\n'.join(line.get('text', '') for line in receipt_lines),
        'payments_block': '\n'.join(line.get('text', '') for line in payments_rows),
    }
    return context, receipt_lines, receipt_rows, payments_rows, summary_rows


def _build_receipt_visual_blocks(payload):
    elements = _template_elements('receipt')
    source_canvas_width = _visual_source_canvas_width('receipt', elements)
    context, _receipt_lines, receipt_rows, payments_rows, summary_rows = _build_receipt_template_context(payload)
    blocks = []
    cursor_y = 52

    for index, elem in enumerate(elements):
        render_elem = _scaled_visual_elem(elem, source_canvas_width, _EDITOR_CANVAS_WIDTH)
        x, y, width, height, cursor_y = _visual_block_rect(render_elem, cursor_y)
        field = str(render_elem.get('field') or '').strip()
        blocks.append({
            'field': field,
            'align': str(render_elem.get('align', 'left') or 'left'),
            'style': str(render_elem.get('style', 'normal') or 'normal'),
            'text': str(render_elem.get('text') or '') if field == 'static_text' else context.get(field, ''),
            'rows': receipt_rows if field == 'lines_block' else (payments_rows if field == 'payments_block' else summary_rows.get(field)),
            'x': x,
            'y': y,
            'width': width,
            'height': height,
            'order': _as_int(render_elem.get('order', index + 1), index + 1),
        })

    return blocks


def _build_kitchen_template_context(payload):
    elements = _template_elements('kitchen')
    label_overrides = {
        str(elem.get('field') or '').strip(): str(elem.get('label') or '').strip()
        for elem in elements
        if isinstance(elem, dict) and str(elem.get('label') or '').strip()
    }
    printer_label = _first_non_empty(
        payload.get('printer_name'),
        payload.get('route_printer'),
        payload.get('printer'),
        'Kitchen',
    )
    table_label = _resolve_table_label(payload) or 'N/A'
    is_takeout_name = bool(payload.get('takeout_name')) and table_label == str(payload.get('takeout_name')).strip()
    order_label = _first_non_empty(
        payload.get('order'),
        payload.get('order_name'),
        payload.get('name'),
        payload.get('tracking_number'),
        payload.get('trackingNumber'),
    )
    waiter_label = _first_non_empty(
        payload.get('waiter'),
        payload.get('cashier'),
        payload.get('server'),
        payload.get('employee'),
        payload.get('user'),
    )

    kitchen_rows = _build_kitchen_rows(payload)
    kitchen_lines = [{'text': row.get('text', ''), 'style': row.get('style', 'normal')} for row in kitchen_rows]
    context = {
        'ticket_title': f"** {printer_label.upper()} ORDER **",
        'table_big': f"{label_overrides.get('table_big', 'TABLE')} {table_label}".strip(),
        'table_circle': '' if is_takeout_name else f"({table_label})",
        'printer_line': _label_value_text(label_overrides.get('printer_line', 'Printer'), printer_label),
        'table_line': _label_value_text(label_overrides.get('table_line', 'Table'), table_label),
        'order_line': _label_value_text(label_overrides.get('order_line', 'Order'), order_label),
        'time_line': _label_value_text(label_overrides.get('time_line', 'Time'), datetime.now().strftime('%H:%M:%S')),
        'waiter_line': _label_value_text(label_overrides.get('waiter_line', 'Waiter'), waiter_label) if waiter_label else '',
        'items_block': '\n'.join(line.get('text', '') for line in kitchen_lines),
    }
    return context, kitchen_lines, kitchen_rows


def _build_kitchen_visual_blocks(payload):
    elements = _template_elements('kitchen')
    source_canvas_width = _visual_source_canvas_width('kitchen', elements)
    context, _kitchen_lines, kitchen_rows = _build_kitchen_template_context(payload)
    blocks = []
    cursor_y = 52

    for index, elem in enumerate(elements):
        render_elem = _scaled_visual_elem(elem, source_canvas_width, _EDITOR_CANVAS_WIDTH)
        x, y, width, height, cursor_y = _visual_block_rect(render_elem, cursor_y)
        field = str(render_elem.get('field') or '').strip()
        blocks.append({
            'field': field,
            'align': str(render_elem.get('align', 'left') or 'left'),
            'style': str(render_elem.get('style', 'normal') or 'normal'),
            'text': str(render_elem.get('text') or '') if field == 'static_text' else context.get(field, ''),
            'rows': kitchen_rows if field == 'items_block' else None,
            'x': x,
            'y': y,
            'width': width,
            'height': height,
            'order': _as_int(render_elem.get('order', index + 1), index + 1),
        })

    return blocks



def _render_receipt_template(printer, payload):
    elements = _template_elements('receipt')
    if _template_uses_visual_layout('receipt'):
        try:
            visual_blocks = _build_receipt_visual_blocks(payload)
            _render_visual_blocks(printer, visual_blocks)
            return
        except Exception:
            logger.exception('Visual receipt render failed; falling back to line renderer')

    currency_symbol = payload.get('currency_symbol', '')
    context, _receipt_lines, receipt_rows, payments_rows, summary_rows = _build_receipt_template_context(payload)

    for elem in elements:
        field = elem.get('field')
        if field == 'separator':
            _emit_template_line(printer, '-' * 42, elem)
            continue
        if field == 'blank':
            _emit_template_line(printer, '', elem)
            continue
        if field == 'lines_block':
            _emit_template_rows(printer, receipt_rows, elem)
            continue
        if field == 'payments_block':
            _emit_template_rows(printer, payments_rows, elem)
            continue
        if field in summary_rows:
            _emit_template_rows(printer, summary_rows[field], elem)
            continue
        if field == 'static_text':
            value = str(elem.get('text') or '')
            if value:
                _emit_template_line(printer, value, elem)
            continue
        value = context.get(field, '')
        if value:
            _emit_template_line(printer, value, elem)


def _render_kitchen_template(printer, payload):
    elements = _template_elements('kitchen')
    if _template_uses_visual_layout('kitchen'):
        try:
            visual_blocks = _build_kitchen_visual_blocks(payload)
            _render_visual_blocks(printer, visual_blocks)
            return
        except Exception:
            logger.exception('Visual kitchen render failed; falling back to line renderer')

    context, _kitchen_lines, kitchen_rows = _build_kitchen_template_context(payload)

    import json as _json
    logger.info('[KITCHEN-DEBUG] raw changes payload: %s', _json.dumps(payload.get('changes', {}), ensure_ascii=False, default=str))

    for elem in elements:
        field = elem.get('field')
        if field == 'separator':
            _emit_template_line(printer, '-' * 40, elem)
            continue
        if field == 'blank':
            _emit_template_line(printer, '', elem)
            continue
        if field == 'items_block':
            _emit_template_rows(printer, kitchen_rows, elem, width=40)
            continue
        if field == 'static_text':
            value = str(elem.get('text') or '')
            if value:
                _emit_template_line(printer, value, elem)
            continue
        value = context.get(field, '')
        if value:
            _emit_template_line(printer, value, elem)


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
        _render_receipt_template(printer, payload)
    elif printer_type == 'kitchen' and is_json:
        _render_kitchen_template(printer, payload)
    else:
        printer.text('** RECEIPT **\n')
        printer.set(align='left', font='a', bold=False, height=1, width=1)
        printer.text(data + '\n')

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
    global _last_odoo_unavailable_log_ts
    if not odoo.ensure_connected(retry_forever=False):
        now = time.monotonic()
        if (now - _last_odoo_unavailable_log_ts) >= 10.0:
            logger.warning(
                'Odoo is not connected. Agent API is online; update connection from Manager and restart agent.'
            )
            _last_odoo_unavailable_log_ts = now
        return

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
        logger.info('RAW ODOO PAYLOAD: %s', data)

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
    load_local_config()
    runtime_odoo = _runtime_odoo_settings()
    startup_url = runtime_odoo.get('url') or ODOO_URL
    startup_db = runtime_odoo.get('db') or ODOO_DB
    logger.info('=' * 60)
    logger.info('Odoo PoS Print Agent starting...')
    logger.info('Mode: %s | Poll interval: %ss', _runtime_default_value('mode', PRINTER_MODE), _runtime_poll_interval())
    logger.info('Odoo: %s (db=%s)', startup_url, startup_db)
    logger.info('=' * 60)

    odoo = OdooConnection()
    if USE_ODOO_REMOTE_CONFIG:
        refresh_runtime_config(odoo, force=True)

    # Start local HTTP push server for immediate print
    def start_http_server():
        host = '127.0.0.1'
        port = 8899

        class PrintHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                logger.debug('HTTP %s', format % args)

            def _set_headers(self, status=200):
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
                self.send_header('Access-Control-Allow-Headers', 'Content-Type')
                self.end_headers()

            def _write_json(self, status, payload):
                self._set_headers(status)
                self.wfile.write(json.dumps(payload).encode('utf-8'))

            def _read_json_body(self):
                length = int(self.headers.get('Content-Length', 0))
                payload = self.rfile.read(length) if length else b'{}'
                return json.loads(payload.decode('utf-8'))

            def do_OPTIONS(self):
                self._set_headers()

            def do_GET(self):
                try:
                    path = urlparse(self.path).path
                    if path == '/health':
                        self._write_json(200, {'success': True, 'status': 'ok'})
                        return
                    if path == '/api/config':
                        self._write_json(200, {'success': True, 'config': _runtime_snapshot()})
                        return
                    if path == '/api/logs':
                        self._write_json(200, {'success': True, 'log_tail': _read_log_tail(300)})
                        return
                    self._write_json(404, {'success': False, 'error': 'not found'})
                except Exception as exc:  # noqa: BLE001
                    logger.error('HTTP GET error: %s', exc, exc_info=True)
                    self._write_json(500, {'success': False, 'error': str(exc)})

            def do_POST(self):
                try:
                    path = urlparse(self.path).path
                    body = self._read_json_body()

                    if path == '/api/config':
                        target = body.get('config', body)
                        saved_to = save_local_config(target)
                        _apply_remote_config_payload(_sanitize_runtime_payload(target) or {})
                        odoo.apply_remote_odoo_settings(_runtime_odoo_settings())
                        self._write_json(200, {'success': True, 'saved_to': saved_to, 'config': _runtime_snapshot()})
                        return

                    if path == '/api/test-print':
                        route_printer = body.get('printer_name') or body.get('route_printer') or 'Receipt'
                        printer_type = body.get('printer_type', 'receipt')
                        test_payload = {
                            'type': 'receipt' if printer_type == 'receipt' else 'kitchen',
                            'company_name': 'Odoo Print Agent',
                            'order_name': 'TEST',
                            'tracking_number': 'TEST-001',
                            'cashier': 'Agent',
                            'date': datetime.now().isoformat(),
                            'table': body.get('table') or '',
                            'customer_count': body.get('customer_count') or '',
                            'printer_name': route_printer,
                            'currency_symbol': '',
                            'subtotal': 0,
                            'tax': 0,
                            'total': 0,
                            'payments': [],
                            'lines': [{'name': body.get('text') or 'Test line', 'qty': 1, 'price': 0, 'price_display': '', 'unit_price_display': ''}],
                            'changes': {'new': [{'qty': 1, 'product': body.get('text') or 'Test kitchen line', 'note': ''}]},
                            'order': 'TEST',
                        }
                        print_with_route(json.dumps(test_payload), printer_type, route_printer)
                        self._write_json(200, {'success': True})
                        return

                    if path == '/api/restart':
                        _trigger_self_restart()
                        self._write_json(200, {'success': True, 'message': 'restart scheduled'})
                        return

                    if path == '/print':
                        data = body.get('data', '')
                        printer_type = body.get('printer_type', 'receipt')
                        route_printer = _resolve_printer_name(body)
                        logger.info('Push received: type=%s route_printer=%s keys=%s', printer_type, route_printer, list(body.keys()))
                        logger.info('RAW HTTP PAYLOAD: %s', data)
                        if not data:
                            self._write_json(400, {'success': False, 'error': 'no data'})
                            return
                        try:
                            print_with_route(data, printer_type, route_printer)
                            self._write_json(200, {'success': True})
                        except Exception as exc:  # noqa: BLE001
                            logger.error('Push print failed: %s', exc, exc_info=True)
                            self._write_json(500, {'success': False, 'error': str(exc)})
                        return

                    self._write_json(404, {'success': False, 'error': 'not found'})
                except Exception as exc:  # noqa: BLE001
                    logger.error('HTTP handler error: %s', exc, exc_info=True)
                    self._write_json(500, {'success': False, 'error': str(exc)})

        httpd = ThreadingHTTPServer((host, port), PrintHandler)
        logger.info('HTTP server listening on http://%s:%s (push=/print, config=/api/config, test=/api/test-print)', host, port)
        httpd.serve_forever()

    threading.Thread(target=start_http_server, daemon=True).start()

    while True:
        try:
            if USE_ODOO_REMOTE_CONFIG:
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
