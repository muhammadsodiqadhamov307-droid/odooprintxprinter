from odoo import api, fields, models


class PosPrintAgentConfig(models.Model):
    _name = "pos.print.agent.config"
    _description = "PoS Print Agent Configuration"
    _order = "id desc"

    name = fields.Char(required=True, default="Default")
    active = fields.Boolean(default=True)
    poll_interval_sec = fields.Float(
        string="Agent Poll Interval (seconds)",
        default=0.2,
        help="Fallback queue polling interval used by the local print agent.",
    )

    agent_odoo_url = fields.Char(
        string="Agent Odoo URL",
        default="http://localhost:8070",
        help="Optional remote override for print_agent.py Odoo URL.",
    )
    agent_odoo_db = fields.Char(
        string="Agent Odoo Database",
        default="default",
        help="Optional remote override for print_agent.py database name.",
    )
    agent_odoo_username = fields.Char(
        string="Agent Odoo Username",
        default="admin",
        help="Optional remote override for print_agent.py username.",
    )
    agent_odoo_password = fields.Char(
        string="Agent Odoo Password",
        default="password",
        help="Optional remote override for print_agent.py password.",
    )

    default_mode = fields.Selection(
        selection=[("network", "Network"), ("usb", "USB")],
        required=True,
        default="network",
    )
    default_network_ip = fields.Char(
        string="Default Network IP",
        default="192.168.123.100",
    )
    default_network_port = fields.Integer(
        string="Default Network Port",
        default=9100,
    )
    default_usb_vendor_id = fields.Integer(
        string="Default USB Vendor ID",
        default=0x1FC9,
    )
    default_usb_product_id = fields.Integer(
        string="Default USB Product ID",
        default=0x2016,
    )
    default_timeout_sec = fields.Float(
        string="Default Connect Timeout (seconds)",
        default=1.0,
    )
    default_retries = fields.Integer(
        string="Default Retries",
        default=2,
    )
    default_cooldown_sec = fields.Float(
        string="Default Cooldown (seconds)",
        default=3.0,
        help="If a route fails, skip it for this many seconds before retrying.",
    )

    route_ids = fields.One2many(
        "pos.print.agent.route",
        "config_id",
        string="Printer Routes",
    )

    @api.model
    def _get_or_create_active(self):
        config = self.sudo().search([("active", "=", True)], limit=1, order="id desc")
        if config:
            return config

        return self.sudo().create({
            "name": "Default",
            "active": True,
        })

    @api.model
    def rpc_get_agent_config(self):
        """
        Called by print_agent.py via XML-RPC.
        Returns a serializable dictionary of active config + routes.
        """
        config = self._get_or_create_active()
        routes = {}
        for route in config.route_ids.filtered("active"):
            key = (route.name or "").strip()
            if not key:
                continue
            routes[key] = route.to_agent_dict()

        return {
            "config_id": config.id,
            "config_name": config.name,
            "write_date": config.write_date.isoformat() if config.write_date else False,
            "poll_interval_sec": config.poll_interval_sec,
            "default": {
                "mode": config.default_mode,
                "ip": config.default_network_ip or "",
                "port": config.default_network_port or 9100,
                "usb_vendor_id": config.default_usb_vendor_id or 0,
                "usb_product_id": config.default_usb_product_id or 0,
                "timeout_sec": config.default_timeout_sec,
                "retries": config.default_retries,
                "cooldown_sec": config.default_cooldown_sec,
            },
            "odoo": {
                "url": config.agent_odoo_url or "",
                "db": config.agent_odoo_db or "",
                "username": config.agent_odoo_username or "",
                "password": config.agent_odoo_password or "",
            },
            "routes": routes,
        }


class PosPrintAgentRoute(models.Model):
    _name = "pos.print.agent.route"
    _description = "PoS Print Agent Route"
    _order = "sequence, id"

    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    name = fields.Char(
        required=True,
        help="Logical printer name from PoS payload, for example: Receipt, Kitchen, Bar.",
    )
    config_id = fields.Many2one(
        "pos.print.agent.config",
        required=True,
        ondelete="cascade",
    )
    mode = fields.Selection(
        selection=[("network", "Network"), ("usb", "USB")],
        required=True,
        default="network",
    )
    network_ip = fields.Char(string="Network IP")
    network_port = fields.Integer(string="Network Port", default=9100)
    usb_vendor_id = fields.Integer(string="USB Vendor ID")
    usb_product_id = fields.Integer(string="USB Product ID")
    timeout_sec = fields.Float(string="Connect Timeout (seconds)", default=1.0)
    retries = fields.Integer(string="Retries", default=2)
    cooldown_sec = fields.Float(string="Cooldown (seconds)", default=3.0)
    notes = fields.Char(string="Notes")

    _sql_constraints = [
        (
            "pos_print_agent_route_name_unique_per_config",
            "unique(config_id, name)",
            "Printer route name must be unique per configuration.",
        ),
    ]

    def to_agent_dict(self):
        self.ensure_one()
        return {
            "mode": self.mode,
            "ip": self.network_ip or "",
            "port": self.network_port or 9100,
            "usb_vendor_id": self.usb_vendor_id or 0,
            "usb_product_id": self.usb_product_id or 0,
            "timeout_sec": self.timeout_sec,
            "retries": self.retries,
            "cooldown_sec": self.cooldown_sec,
        }
