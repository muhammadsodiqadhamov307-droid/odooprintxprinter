import logging
from collections import defaultdict

from odoo import fields, http
from odoo.http import request

_logger = logging.getLogger(__name__)


class PosPrintController(http.Controller):
    def _order_domain(self, session_id=False, config_id=False):
        domain = [('state', 'in', ['paid', 'done', 'invoiced'])]
        if session_id:
            domain.append(('session_id', '=', session_id))
        elif config_id:
            day_start_utc = fields.Datetime.to_string(
                fields.Datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            )
            domain.extend([
                ('session_id.config_id', '=', config_id),
                ('date_order', '>=', day_start_utc),
            ])
        return domain

    def _resolve_pos_category_name(self, product):
        if not product:
            return 'Uncategorized'
        if hasattr(product, 'pos_categ_id') and product.pos_categ_id:
            return product.pos_categ_id.display_name
        if hasattr(product, 'pos_categ_ids') and product.pos_categ_ids:
            return product.pos_categ_ids[:1].display_name
        if product.categ_id:
            return product.categ_id.display_name
        return 'Uncategorized'

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

            session = request.env['pos.session'].sudo().browse(session_id) if session_id else request.env['pos.session']
            config = session.config_id if session_id and session.exists() else (
                request.env['pos.config'].sudo().browse(config_id) if config_id else request.env['pos.config']
            )
            company = (session.company_id if session_id and session.exists() else config.company_id) or request.env.company
            currency = company.currency_id

            order_domain = self._order_domain(session_id=session_id, config_id=config_id)
            orders = request.env['pos.order'].sudo().search(order_domain)
            order_count = len(orders)

            category_buckets = defaultdict(lambda: {
                'name': 'Uncategorized',
                'qty': 0.0,
                'total': 0.0,
                'products': defaultdict(lambda: {'product_name': 'Product', 'qty': 0.0, 'line_total': 0.0}),
            })
            total_paid = 0.0
            payment_buckets = defaultdict(float)

            for order in orders:
                total_paid += float(order.amount_total or 0.0)
                for payment in order.payment_ids:
                    label = payment.payment_method_id.name or 'Payment'
                    payment_buckets[label] += float(payment.amount or 0.0)
                for line in order.lines:
                    product = line.product_id
                    category_name = self._resolve_pos_category_name(product)
                    bucket = category_buckets[category_name]
                    bucket['name'] = category_name
                    qty = float(line.qty or 0.0)
                    line_total = float(getattr(line, 'price_subtotal_incl', line.price_subtotal or 0.0) or 0.0)
                    product_name = product.display_name if product else (line.full_product_name or line.name or 'Product')
                    bucket['qty'] += qty
                    bucket['total'] += line_total
                    product_bucket = bucket['products'][product_name]
                    product_bucket['product_name'] = product_name
                    product_bucket['qty'] += qty
                    product_bucket['line_total'] += line_total

            categories = []
            products = []
            for category_name in sorted(category_buckets.keys(), key=lambda value: value.lower()):
                bucket = category_buckets[category_name]
                category_products = []
                for product_name in sorted(bucket['products'].keys(), key=lambda value: value.lower()):
                    product_bucket = bucket['products'][product_name]
                    product_line = {
                        'product_name': product_bucket['product_name'],
                        'qty': product_bucket['qty'],
                        'line_total': round(product_bucket['line_total'], currency.decimal_places or 2),
                    }
                    category_products.append(product_line)
                    products.append({
                        'category_name': category_name,
                        **product_line,
                    })
                categories.append({
                    'name': bucket['name'],
                    'qty': bucket['qty'],
                    'total': round(bucket['total'], currency.decimal_places or 2),
                    'products': category_products,
                })

            data = {
                'type': 'daily_sales',
                'printer_name': 'Receipt',
                'company_name': company.display_name,
                'currency_symbol': currency.symbol or '',
                'currency_precision': currency.decimal_places or 2,
                'printed_at': fields.Datetime.now().isoformat(),
                'session_name': session.name if session_id and session.exists() else '',
                'config_name': config.display_name if config and config.exists() else '',
                'order_count': order_count,
                'total_paid': round(total_paid, currency.decimal_places or 2),
                'products': products,
                'categories': categories,
                'payments': [
                    {'name': name, 'amount': round(amount, currency.decimal_places or 2)}
                    for name, amount in sorted(payment_buckets.items(), key=lambda item: item[0].lower())
                ],
                'taxes': [],
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
