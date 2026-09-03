# -*- coding: utf-8 -*-
from odoo import _, api, fields, models

from .whatsapp_account import normalize_number


class WhatsappBlocklist(models.Model):
    """Numbers whose inbound messages are dropped on arrival.

    Checked before a conversation is created, so a blocked number cannot open
    tickets, trigger the bot, or reach the agent queue at all. Nothing is sent
    back: replying would confirm the number is live.
    """

    _name = "whatsapp.blocklist"
    _description = "WhatsApp Blocked Number"
    _order = "create_date desc"
    _rec_name = "number"

    number = fields.Char(required=True, index=True)
    partner_id = fields.Many2one("res.partner", string="Contact")
    reason = fields.Selection(
        [
            ("spam", "Spam"),
            ("abuse", "Abusive"),
            ("test", "Test number"),
            ("other", "Other"),
        ],
        default="spam", required=True,
    )
    note = fields.Char()
    blocked_by = fields.Many2one(
        "res.users", default=lambda self: self.env.user, readonly=True
    )
    blocked_on = fields.Datetime(default=fields.Datetime.now, readonly=True)
    hit_count = fields.Integer(
        readonly=True, default=0,
        help="Messages dropped since the number was blocked.",
    )
    last_hit = fields.Datetime(readonly=True)

    _sql_constraints = [
        ("number_uniq", "unique(number)", "That number is already blocked."),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("number"):
                vals["number"] = normalize_number(vals["number"])
        return super().create(vals_list)

    @api.model
    def _entry_for(self, number):
        """Return the blocklist entry for a number, or an empty recordset."""
        number = normalize_number(number)
        if not number:
            return self.browse()
        return self.sudo().search([("number", "=", number)], limit=1)

    def _register_hit(self):
        self.ensure_one()
        self.sudo().write({
            "hit_count": self.hit_count + 1,
            "last_hit": fields.Datetime.now(),
        })

    @api.model
    def _block(self, number, reason="spam", note=None, partner=None):
        number = normalize_number(number)
        existing = self._entry_for(number)
        if existing:
            return existing
        return self.sudo().create({
            "number": number,
            "reason": reason,
            "note": note,
            "partner_id": partner.id if partner else False,
        })

    def _payload(self):
        self.ensure_one()
        return {
            "id": self.id,
            "number": self.number,
            "partner": self.partner_id.display_name or "",
            "reason": self.reason,
            "reason_label": dict(self._fields["reason"].selection)[self.reason],
            "note": self.note or "",
            "blocked_by": self.blocked_by.name or "",
            "blocked_on": fields.Datetime.to_string(self.blocked_on),
            "hits": self.hit_count,
        }

    def action_unblock(self):
        return self.unlink()
