import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class PosPrintController(http.Controller):
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
