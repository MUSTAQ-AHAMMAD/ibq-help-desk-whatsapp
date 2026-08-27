# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class WhatsappTag(models.Model):
    """A label an agent puts on a conversation.

    Tags are what make the reports say something useful: without them every
    chat is just "a chat", and the only breakdown available is by agent or by
    department.
    """

    _name = "whatsapp.tag"
    _description = "WhatsApp Tag"
    _order = "sequence, name"

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    color = fields.Integer(
        default=0, help="Index into Odoo's tag palette, 0-11."
    )
    description = fields.Char(help="What this tag is for, shown to agents.")
    team_ids = fields.Many2many(
        "helpdesk.team", string="Departments",
        help="Restrict this tag to certain departments. Empty means everywhere.",
    )
    conversation_count = fields.Integer(compute="_compute_conversation_count")

    _sql_constraints = [
        ("name_uniq", "unique(name)", "That tag already exists."),
    ]

    @api.constrains("color")
    def _check_color(self):
        for tag in self:
            if not 0 <= tag.color <= 11:
                raise ValidationError(_("Tag colour must be between 0 and 11."))

    def _compute_conversation_count(self):
        groups = self.env["whatsapp.conversation"]._read_group(
            [("tag_ids", "in", self.ids)], ["tag_ids"], ["__count"]
        )
        mapped = {tag.id: count for tag, count in groups}
        for tag in self:
            tag.conversation_count = mapped.get(tag.id, 0)

    def _payload(self):
        self.ensure_one()
        return {
            "id": self.id,
            "name": self.name,
            "color": self.color,
            "description": self.description or "",
        }

    def action_view_conversations(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id(
            "ibq_whatsapp_helpdesk.action_whatsapp_conversation"
        )
        action["domain"] = [("tag_ids", "in", self.ids)]
        action["context"] = {}
        return action
