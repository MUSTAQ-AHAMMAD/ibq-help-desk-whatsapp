# -*- coding: utf-8 -*-
from odoo import _, api, fields, models

from .whatsapp_account import normalize_number


class ResPartner(models.Model):
    _inherit = "res.partner"

    whatsapp_conversation_ids = fields.One2many(
        "whatsapp.conversation", "partner_id", string="WhatsApp Chats"
    )
    whatsapp_conversation_count = fields.Integer(
        compute="_compute_whatsapp_conversation_count"
    )

    def _compute_whatsapp_conversation_count(self):
        groups = self.env["whatsapp.conversation"]._read_group(
            [("partner_id", "in", self.ids)], ["partner_id"], ["__count"]
        )
        mapped = {partner.id: count for partner, count in groups}
        for partner in self:
            partner.whatsapp_conversation_count = mapped.get(partner.id, 0)

    @api.model
    def _find_or_create_from_whatsapp(self, number, profile_name=None, create=True):
        """Match an inbound WhatsApp number to a contact.

        Matching is done on the normalised E.164 form against both mobile and
        phone, so a number stored as '+971 50 123 4567' still resolves.
        """
        number = normalize_number(number)
        if not number:
            return self.browse()
        digits = number.lstrip("+")
        # Fall back to a suffix match: local formats often drop the country code.
        tail = digits[-9:] if len(digits) > 9 else digits
        partner = self.sudo().search([
            "|", ("mobile", "like", tail), ("phone", "like", tail),
        ], limit=1)
        if partner:
            return partner
        if not create:
            return self.browse()
        return self.sudo().create({
            "name": profile_name or number,
            "mobile": number,
            "type": "contact",
            "comment": _("Created automatically from an inbound WhatsApp message."),
        })

    def action_view_whatsapp_conversations(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id(
            "ibq_whatsapp_helpdesk.action_whatsapp_conversation"
        )
        action["domain"] = [("partner_id", "=", self.id)]
        action["context"] = {"default_partner_id": self.id}
        return action
