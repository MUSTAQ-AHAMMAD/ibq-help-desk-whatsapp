# -*- coding: utf-8 -*-
"""The slice of Odoo Enterprise's WhatsApp app that matters for the collision."""
from odoo import fields, models


class WhatsappMessageEE(models.Model):
    _name = "whatsapp.message"
    _description = "WhatsApp Message (Enterprise contract)"

    mail_message_id = fields.Many2one("mail.message", ondelete="cascade")
    body = fields.Text()


class MailMessage(models.Model):
    _inherit = "mail.message"

    # This is the One2many that fails with KeyError: 'mail_message_id'
    # when another module redefines whatsapp.message without that field.
    whatsapp_message_ids = fields.One2many("whatsapp.message", "mail_message_id")
