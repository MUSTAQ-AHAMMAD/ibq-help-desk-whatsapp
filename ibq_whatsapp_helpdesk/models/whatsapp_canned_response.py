# -*- coding: utf-8 -*-
import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

SHORTCUT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class WhatsappCannedResponse(models.Model):
    """A saved reply an agent inserts by typing its shortcut.

    The same sentences get typed dozens of times a day; a shortcut turns them
    into two keystrokes and keeps the wording consistent across the team.
    """

    _name = "whatsapp.canned.response"
    _description = "WhatsApp Canned Response"
    _order = "sequence, shortcut"
    _rec_name = "shortcut"

    shortcut = fields.Char(
        required=True,
        help="Typed as /shortcut in the composer. Lowercase letters, digits, "
             "hyphen and underscore.",
    )
    name = fields.Char("Title", required=True, help="What agents see in the picker.")
    body = fields.Text(
        required=True,
        help="Supports {name}, {number}, {agent}, {ticket_ref} and any answer "
             "the bot collected, e.g. {order_ref}.",
    )
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    owner_id = fields.Many2one(
        "res.users", string="Private to",
        help="Set to keep this reply personal. Leave empty to share it with "
             "the whole team.",
    )
    team_ids = fields.Many2many(
        "helpdesk.team", string="Departments",
        help="Restrict to certain departments. Empty means everywhere.",
    )
    usage_count = fields.Integer(readonly=True, copy=False, default=0)
    last_used = fields.Datetime(readonly=True, copy=False)

    _sql_constraints = [
        ("shortcut_owner_uniq", "unique(shortcut, owner_id)",
         "That shortcut is already taken."),
    ]

    @api.constrains("shortcut")
    def _check_shortcut(self):
        for record in self:
            if not SHORTCUT_RE.match(record.shortcut or ""):
                raise ValidationError(_(
                    "A shortcut looks like 'thanks' or 'order-status': lowercase "
                    "letters, digits, hyphen and underscore, starting with a "
                    "letter or digit."
                ))

    @api.onchange("shortcut")
    def _onchange_shortcut(self):
        if self.shortcut:
            self.shortcut = re.sub(r"[^a-z0-9_-]", "", self.shortcut.strip().lower())

    # ------------------------------------------------------------------
    # Lookup and rendering
    # ------------------------------------------------------------------
    @api.model
    def _available_domain(self, team=None):
        """Shared replies plus the caller's own private ones."""
        domain = ["|", ("owner_id", "=", False), ("owner_id", "=", self.env.uid)]
        if team:
            domain += ["|", ("team_ids", "=", False), ("team_ids", "in", team.ids)]
        return domain

    @api.model
    def _search_for_agent(self, search=None, team=None, limit=40):
        domain = self._available_domain(team)
        if search:
            needle = search.lstrip("/")
            domain += ["|", "|",
                       ("shortcut", "ilike", needle),
                       ("name", "ilike", needle),
                       ("body", "ilike", needle)]
        return self.search(domain, limit=limit, order="usage_count desc, sequence, shortcut")

    def render(self, conversation=None):
        """Fill the placeholders from a conversation, leaving unknowns intact."""
        self.ensure_one()
        if not conversation:
            return self.body or ""
        values = conversation._get_answers()
        values.setdefault("name", conversation.partner_id.name
                          or conversation.profile_name or "")
        values.setdefault("number", conversation.number or "")
        values.setdefault("ticket_ref", conversation.ticket_id
                          and str(conversation.ticket_id.id) or "")
        agent = self.env["whatsapp.agent"]._current()
        values.setdefault("agent", agent.display_alias or self.env.user.name)
        return PLACEHOLDER_RE.sub(
            lambda m: str(values.get(m.group(1), m.group(0))), self.body or ""
        )

    def _register_use(self):
        for record in self:
            record.sudo().write({
                "usage_count": record.usage_count + 1,
                "last_used": fields.Datetime.now(),
            })

    def _payload(self, conversation=None):
        self.ensure_one()
        return {
            "id": self.id,
            "shortcut": self.shortcut,
            "name": self.name,
            "body": self.render(conversation),
            "raw_body": self.body or "",
            "is_private": bool(self.owner_id),
            "usage_count": self.usage_count,
        }
