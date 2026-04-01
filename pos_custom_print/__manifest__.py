{
    'name': 'PoS Custom Thermal Print',
    'version': '19.0.2.10.0',
    'category': 'Point of Sale',
    'summary': 'Routes PoS print jobs to a local Xprinter XP-80 via a polling agent',
    'author': 'Your Company',
    'license': 'LGPL-3',
    'depends': ['point_of_sale', 'web'],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron_cleanup.xml',
    ],
    'assets': {
        'point_of_sale._assets_pos': [
            'pos_custom_print/static/src/js/patch_printer.js',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
}
