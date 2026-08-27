# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


class WhatsappInviteMember(models.TransientModel):
    """Add people to the WhatsApp team from a menu, not only the dashboard.

    Same rules as the dashboard roster: only someone with the roster right can
    open it, and only the Owner may hand out Administrator.
    """

    _name = "whatsapp.invite.member"
    _description = "Add WhatsApp Team Members"

    user_ids = fields.Many2many(
        "res.users", string="Team Members", required=True,
        domain="[('share', '=', False), ('id', 'not in', existing_user_ids)]",
        help="Internal users to put on the WhatsApp roster.",
    )
    existing_user_ids = fields.Many2many(
        "res.users", "whatsapp_invite_existing_rel", "wizard_id", "user_id",
        compute="_compute_existing_user_ids",
    )
    role = fields.Selection(
        [
            ("agent", "Agent"),
            ("supervisor", "Supervisor"),
            ("admin", "Administrator"),
        ],
        default="agent", required=True,
        help="Agent: their own chats. Supervisor: every chat in their "
             "departments. Administrator: the roster and every setting.",
    )
    team_ids = fields.Many2many(
        "helpdesk.team", string="Departments",
        help="Leave empty to cover every department.",
    )
    account_ids = fields.Many2many(
        "whatsapp.account", string="Senders",
        help="Leave empty to cover every WhatsApp number.",
    )
    max_active_chats = fields.Integer("Capacity", default=5)
    status = fields.Selection(
        [
            ("available", "Available"),
            ("offline", "Offline"),
        ],
        default="offline", required=True, string="Initial Presence",
        help="Start them Offline unless they are ready to take chats now.",
    )
    notify = fields.Boolean(
        "Send a Notification", default=True,
        help="Post a message in Odoo telling them they were added.",
    )

    @api.depends("user_ids")
    def _compute_existing_user_ids(self):
        on_roster = self.env["whatsapp.agent"].sudo().search([]).user_id
        for wizard in self:
            wizard.existing_user_ids = on_roster

    @api.constrains("max_active_chats")
    def _check_capacity(self):
        for wizard in self:
            if wizard.max_active_chats < 1:
                raise UserError(_("Capacity must be at least 1 chat."))

    def action_add(self):
        self.ensure_one()
        me = self.env["whatsapp.agent"]._assert_right("manage_roster")
        if self.role == "admin" and me and me.role != "owner":
            raise AccessError(_("Only the Owner can create another Administrator."))

        agents = self.env["whatsapp.agent"].sudo()
        created = agents.browse()
        for user in self.user_ids:
            if agents.search_count([("user_id", "=", user.id)]):
                continue
            created |= agents.create({
                "user_id": user.id,
                "role": self.role,
                "status": self.status,
                "max_active_chats": self.max_active_chats,
                "team_ids": [(6, 0, self.team_ids.ids)],
                "account_ids": [(6, 0, self.account_ids.ids)],
            })

        if not created:
            raise UserError(_("Everyone selected is already on the roster."))
        if self.notify:
            created._notify_added()

        return {
            "type": "ir.actions.act_window",
            "name": _("WhatsApp Team"),
            "res_model": "whatsapp.agent",
            "view_mode": "tree,form",
            "domain": [("id", "in", created.ids)],
        }
