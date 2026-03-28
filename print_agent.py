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


logger = logging.getLogger('PrintAgent')
_printer_cache = None
_route_fail_until = {}
_runtime_lock = threading.RLock()
_last_odoo_unavailable_log_ts = 0.0


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
                field = str(elem.get('field', '')).strip()
                if not field:
                    continue
                normalized_elems.append({
                    'field': field,
                    'align': str(elem.get('align', 'left') or 'left'),
                    'style': str(elem.get('style', 'normal') or 'normal'),
                    'col': _as_int(elem.get('col', 0), 0),
                })
            if normalized_elems:
                clean_templates[str(tname)] = {'elements': normalized_elems}
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
                    normalized_templates[str(tname)] = {'elements': [dict(e) for e in elems if isinstance(e, dict)]}
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


def _signed_kitchen_qty(line, section_name):
    qty = _extract_qty(line) if isinstance(line, dict) else _parse_qty(line, default=1.0)
    marker = ''
    if isinstance(line, dict):
        marker = _first_non_empty(
            line.get('section'),
            line.get('state'),
            line.get('status'),
            line.get('change_type'),
            line.get('type'),
        ).lower()

    is_cancelled = section_name == 'cancelled' or marker in {
        'cancelled',
        'canceled',
        'removed',
        'delete',
        'deleted',
        'cxl',
        'minus',
        'negative',
    }

    # Odoo preparation "cancelled" lines are often sent as positive deltas.
    # On paper we want explicit subtraction for kitchen/bar operators.
    if is_cancelled and qty > 0:
        return -qty
    return qty


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


def _template_elements(ticket_type):
    templates = _runtime_templates()
    defaults = _default_templates()
    template = templates.get(ticket_type) if isinstance(templates, dict) else None
    if not isinstance(template, dict):
        template = defaults.get(ticket_type, {})
    elements = template.get('elements')
    if not isinstance(elements, list) or not elements:
        elements = defaults.get(ticket_type, {}).get('elements', [])
    return [dict(elem) for elem in elements if isinstance(elem, dict)]


def _set_style(printer, elem):
    align = str(elem.get('align', 'left') or 'left').lower()
    if align not in ('left', 'center', 'right'):
        align = 'left'
    style = str(elem.get('style', 'normal') or 'normal').lower()
    if style == 'double':
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


def _build_receipt_lines(payload, currency_symbol):
    lines = []
    for line in payload.get('lines', []):
        qty_text = _display_qty(line.get('qty', 0))
        price_text = line.get('price_display') or _money(line.get('price', 0), currency_symbol)
        lines.append({'text': _fit_columns(qty_text, line.get('name', ''), price_text), 'style': 'normal'})
        unit_price_display = line.get('unit_price_display')
        if unit_price_display:
            lines.append({'text': f'  {unit_price_display}', 'style': 'normal'})
    return lines


def _build_kitchen_lines(payload):
    lines = []
    changes = payload.get('changes', {})
    for section_name in ('new', 'cancelled', 'noteUpdate'):
        for line in _as_line_list(changes.get(section_name, [])):
            lines.append({
                'qty': _signed_kitchen_qty(line, section_name),
                'product': _extract_product_name(line),
                'note': _extract_line_note(line),
                'section': section_name,
            })
    for line in _as_line_list(changes.get('data', [])):
        lines.append({
            'qty': _signed_kitchen_qty(line, 'new'),
            'product': _extract_product_name(line),
            'note': _extract_line_note(line),
            'section': 'new',
        })
    if not lines:
        for line in _as_line_list(payload.get('orderlines', [])):
            lines.append({
                'qty': _signed_kitchen_qty(line, 'new'),
                'product': _extract_product_name(line),
                'note': _extract_line_note(line),
                'section': 'new',
            })
    rendered = []
    for line in lines:
        section_name = line.get('section')
        prefix = 'NOTE ' if section_name == 'noteUpdate' else ''
        name = f"{prefix}{line['product']}"[:30]
        rendered.append({'text': f"{_display_qty(line['qty']):<6}{name}", 'style': 'normal'})
        if line['note']:
            rendered.append({'text': f" >> {line['note']}", 'style': 'normal'})
    return rendered


def _render_receipt_template(printer, payload):
    currency_symbol = payload.get('currency_symbol', '')
    elements = _template_elements('receipt')

    table_label = payload.get('table') or '-'
    guest_label = payload.get('customer_count') or '-'
    context = {
        'company_name': payload.get('company_name', 'Odoo POS'),
        'order_name_line': f"Ticket {payload.get('order_name', '')}" if payload.get('order_name') else '',
        'date_line': f"{payload.get('date', '')[:19].replace('T', ' ')}" if payload.get('date') else '',
        'cashier_line': f"Served by: {payload.get('cashier', '')}" if payload.get('cashier') else '',
        'table_guests_line': f"Table: {table_label}  Guests: {guest_label}" if payload.get('table') or payload.get('customer_count') else '',
        'tracking_number': payload.get('tracking_number') or '',
        'subtotal_line': _left_right('Subtotal', _money(payload.get('subtotal', 0), currency_symbol)),
        'tax_line': _left_right('Tax', _money(payload.get('tax', 0), currency_symbol)),
        'total_line': _left_right('Total', _money(payload.get('total', 0), currency_symbol)),
    }

    payments_lines = []
    for payment in payload.get('payments', []):
        payments_lines.append({
            'text': _left_right(
                payment.get('name', 'Payment'),
                payment.get('amount_display') or _money(payment.get('amount', 0), currency_symbol),
            ),
            'style': 'normal',
        })

    for elem in elements:
        field = elem.get('field')
        if field == 'separator':
            _emit_template_line(printer, '-' * 42, elem)
            continue
        if field == 'blank':
            _emit_template_line(printer, '', elem)
            continue
        if field == 'lines_block':
            for l in _build_receipt_lines(payload, currency_symbol):
                line_elem = dict(elem)
                line_elem['style'] = l.get('style', line_elem.get('style', 'normal'))
                _emit_template_line(printer, l.get('text', ''), line_elem)
            continue
        if field == 'payments_block':
            for l in payments_lines:
                line_elem = dict(elem)
                line_elem['style'] = l.get('style', line_elem.get('style', 'normal'))
                _emit_template_line(printer, l.get('text', ''), line_elem)
            continue
        value = context.get(field, '')
        if value:
            _emit_template_line(printer, value, elem)


def _render_kitchen_template(printer, payload):
    elements = _template_elements('kitchen')
    printer_label = _first_non_empty(
        payload.get('printer_name'),
        payload.get('route_printer'),
        payload.get('printer'),
        'Kitchen',
    )
    table_label = _first_non_empty(
        payload.get('table'),
        payload.get('table_name'),
        payload.get('table_number'),
        payload.get('table_id', {}).get('table_number') if isinstance(payload.get('table_id'), dict) else None,
        payload.get('table_id', {}).get('name') if isinstance(payload.get('table_id'), dict) else None,
    ) or 'N/A'
    order_label = _first_non_empty(
        payload.get('order'),
        payload.get('order_name'),
        payload.get('name'),
        payload.get('tracking_number'),
        payload.get('trackingNumber'),
    )

    context = {
        'ticket_title': f"** {printer_label.upper()} ORDER **",
        'table_big': f"TABLE {table_label}",
        'table_circle': f"({table_label})",
        'printer_line': f"Printer : {printer_label}",
        'table_line': f"Table   : {table_label}",
        'order_line': f"Order   : {order_label}",
        'time_line': f"Time    : {datetime.now().strftime('%H:%M:%S')}",
    }

    kitchen_lines = _build_kitchen_lines(payload)

    for elem in elements:
        field = elem.get('field')
        if field == 'separator':
            _emit_template_line(printer, '-' * 40, elem)
            continue
        if field == 'blank':
            _emit_template_line(printer, '', elem)
            continue
        if field == 'items_block':
            for l in kitchen_lines:
                line_elem = dict(elem)
                line_elem['style'] = l.get('style', line_elem.get('style', 'normal'))
                _emit_template_line(printer, l.get('text', ''), line_elem)
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
