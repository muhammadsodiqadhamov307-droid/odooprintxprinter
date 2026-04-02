import inspect
import logging
from collections import defaultdict

from odoo import fields, http
from odoo.http import request

_logger = logging.getLogger(__name__)


class PosPrintController(http.Controller):
    def _call_sale_details(self, report_model, session_id=False, config_id=False):
        method = report_model.get_sale_details
        params = inspect.signature(method).parameters
        kwargs = {}
        if 'date_start' in params:
            kwargs['date_start'] = False
        if 'date_stop' in params:
            kwargs['date_stop'] = False
        if 'ticket_type' in params:
            kwargs['ticket_type'] = False
        if 'user_id' in params:
            kwargs['user_id'] = False
        if 'config_ids' in params:
            kwargs['config_ids'] = [config_id] if config_id else False
        elif 'configs' in params:
            kwargs['configs'] = [config_id] if config_id else False
        if 'session_ids' in params:
            kwargs['session_ids'] = [session_id] if session_id else False
        return method(**kwargs)

    def _normalize_product_id(self, raw_value):
        if isinstance(raw_value, int):
            return raw_value
        if isinstance(raw_value, (list, tuple)) and raw_value:
            return raw_value[0]
        if isinstance(raw_value, str) and raw_value.isdigit():
            return int(raw_value)
        return False

    @http.route(
        '/pos/daily_sales_report',
        type='json',
        auth='user',
        methods=['POST'],
        csrf=False,
    )
    def daily_sales_report(self, **kwargs):
        try:
            session_id = kwargs.get('session_id')
            config_id = kwargs.get('config_id')
            session_id = int(session_id) if session_id else False
            config_id = int(config_id) if config_id else False

            report_model = request.env['report.point_of_sale.report_saledetails'].sudo()
            details = self._call_sale_details(report_model, session_id=session_id, config_id=config_id) or {}

            session = request.env['pos.session'].sudo().browse(session_id) if session_id else request.env['pos.session']
            config = session.config_id if session_id and session.exists() else (
                request.env['pos.config'].sudo().browse(config_id) if config_id else request.env['pos.config']
            )
            company = (session.company_id if session_id and session.exists() else config.company_id) or request.env.company
            currency = company.currency_id

            products = []
            product_ids = []
            for line in details.get('products', []) or []:
                product_id = self._normalize_product_id(line.get('product_id'))
                if product_id:
                    product_ids.append(product_id)
            products_by_id = {
                product.id: product
                for product in request.env['product.product'].sudo().browse(product_ids).exists()
            }

            category_totals = defaultdict(lambda: {'name': 'Uncategorized', 'qty': 0.0, 'total': 0.0})
            for line in details.get('products', []) or []:
                qty = float(line.get('quantity') or line.get('qty') or 0.0)
                price_unit = float(line.get('price_unit') or line.get('price') or 0.0)
                discount = float(line.get('discount') or 0.0)
                line_total = qty * price_unit * (1 - (discount / 100.0))
                product_id = self._normalize_product_id(line.get('product_id'))
                product = products_by_id.get(product_id)
                category_name = (
                    line.get('category_name')
                    or line.get('categ_name')
                    or (product.categ_id.display_name if product and product.categ_id else '')
                    or 'Uncategorized'
                )
                product_name = (
                    line.get('product_name')
                    or line.get('name')
                    or (product.display_name if product else '')
                    or 'Product'
                )
                products.append({
                    'category_name': category_name,
                    'product_name': product_name,
                    'qty': qty,
                    'line_total': round(line_total, currency.decimal_places or 2),
                })
                category_totals[category_name]['name'] = category_name
                category_totals[category_name]['qty'] += qty
                category_totals[category_name]['total'] += line_total

            categories = sorted(
                (
                    {
                        'name': value['name'],
                        'qty': value['qty'],
                        'total': round(value['total'], currency.decimal_places or 2),
                    }
                    for value in category_totals.values()
                ),
                key=lambda item: item['name'].lower(),
            )
            products.sort(key=lambda item: (item['category_name'].lower(), item['product_name'].lower()))

            order_domain = [('state', 'in', ['paid', 'done', 'invoiced'])]
            if session_id:
                order_domain.append(('session_id', '=', session_id))
            elif config_id:
                today_start = fields.Datetime.to_string(fields.Datetime.now().replace(hour=0, minute=0, second=0, microsecond=0))
                order_domain.extend([
                    ('session_id.config_id', '=', config_id),
                    ('date_order', '>=', today_start),
                ])
            order_count = request.env['pos.order'].sudo().search_count(order_domain)

            data = {
                'type': 'daily_sales',
                'printer_name': 'Receipt',
                'company_name': details.get('company_name') or company.display_name,
                'currency_symbol': currency.symbol or '',
                'currency_precision': currency.decimal_places or 2,
                'printed_at': fields.Datetime.now().isoformat(),
                'session_name': session.name if session_id and session.exists() else '',
                'config_name': config.display_name if config and config.exists() else '',
                'order_count': order_count,
                'total_paid': float(details.get('total_paid') or 0.0),
                'products': products,
                'categories': categories,
                'payments': details.get('payments') or [],
                'taxes': details.get('taxes') or [],
            }
            return {'success': True, 'data': data}
        except Exception as e:
            _logger.exception('PosPrintController: failed to build daily sales report')
            return {'success': False, 'error': str(e)}

    @http.route(
        '/pos/add_print_job',
        type='json',
        auth='user',
        methods=['POST'],
        csrf=False,
    )
    def add_print_job(self, **kwargs):
        """
        Expected JSON body (sent by patch_printer.js):
        {
            'jsonrpc': '2.0',
            'method': 'call',
            'params': {
                'data': '<receipt text or ESC/POS string>',
                'printer_type': 'receipt' | 'kitchen',
                'printer_name': 'Receipt' | 'Kitchen' | 'Bar' | ...
            }
        }

        Returns:
            {'success': True, 'job_id': <int>} on success
            {'success': False, 'error': '<message>'} on failure
        """
        try:
            data = kwargs.get('data', '').strip()
            printer_type = kwargs.get('printer_type', 'receipt')
            printer_name = (kwargs.get('printer_name') or '').strip() or False

            if not data:
                return {'success': False, 'error': 'No print data received'}

            if printer_type not in ('receipt', 'kitchen'):
                printer_type = 'receipt'

            job = request.env['pos.print.job'].sudo().create({
                'data': data,
                'printer_type': printer_type,
                'printer_name': printer_name,
                'state': 'pending',
            })
            _logger.info(
                'PosPrintJob created: id=%s type=%s printer_name=%s',
                job.id,
                printer_type,
                printer_name,
            )
            return {'success': True, 'job_id': job.id}
        except Exception as e:
            _logger.exception('PosPrintController: failed to create job')
            return {'success': False, 'error': str(e)}
