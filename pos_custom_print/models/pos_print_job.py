from datetime import datetime

from odoo import api, fields, models


class PosPrintJob(models.Model):
    _name = 'pos.print.job'
    _description = 'PoS Thermal Print Job'
    _order = 'create_date desc'

    name = fields.Char(
        string='Job Reference',
        required=True,
        default=lambda self: self._default_name(),
    )
    data = fields.Text(
        string='Print Data',
        required=True,
        help='ESC/POS command string or plain text. '
        'Stored as UTF-8. The print agent decodes this.',
    )
    state = fields.Selection(
        selection=[
            ('pending', 'Pending'),
            ('printed', 'Printed'),
            ('failed', 'Failed'),
        ],
        string='Status',
        required=True,
        default='pending',
        index=True,
        help='pending: waiting for agent | printed: success | failed: agent error',
    )
    printer_type = fields.Selection(
        selection=[
            ('receipt', 'Receipt Printer'),
            ('kitchen', 'Kitchen Printer'),
        ],
        string='Printer Type',
        required=True,
        default='receipt',
    )
    printer_name = fields.Char(
        string='Route Printer',
        index=True,
        help='Optional logical printer name (for example: Kitchen, Bar, Receipt). '
        'Used by the local print agent to route jobs to the right device.',
    )
    error_msg = fields.Text(
        string='Error Message',
        help='Populated by the print agent if printing fails.',
    )

    @api.model
    def _default_name(self):
        now = datetime.now()
        return 'PRINT-%s' % now.strftime('%Y%m%d-%H%M%S-%f')
