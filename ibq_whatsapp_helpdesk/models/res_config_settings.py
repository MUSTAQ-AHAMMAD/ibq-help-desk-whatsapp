# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    whatsapp_default_account_id = fields.Many2one(
        "whatsapp.account", string="Default WhatsApp Account",
        config_parameter="ibq_whatsapp.default_account_id",
    )
    whatsapp_activity_on_reply = fields.Boolean(
        "Schedule an activity on customer reply",
        config_parameter="ibq_whatsapp.activity_on_reply",
        help="Raise a to-do for the assigned agent when a customer answers a "
             "ticket over WhatsApp.",
    )
    whatsapp_idle_close_hours = fields.Integer(
        "Auto-close idle chats after (hours)",
        config_parameter="ibq_whatsapp.idle_close_hours",
        default=48,
    )
    whatsapp_log_webhooks = fields.Boolean(
        "Log raw webhook payloads",
        config_parameter="ibq_whatsapp.log_webhooks",
        help="Write every inbound Twilio payload to the Odoo log. Useful while "
             "wiring things up, noisy in production.",
    )

    def action_open_whatsapp_accounts(self):
        return self.env["ir.actions.actions"]._for_xml_id(
            "ibq_whatsapp_helpdesk.action_whatsapp_account"
        )
