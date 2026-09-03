# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .whatsapp_account import normalize_number

# Roles are ordered from most to least privileged; the index is used for
# "can this person act on that person" comparisons.
ROLES = ["owner", "admin", "supervisor", "agent"]

ROLE_GROUPS = {
    "owner": "ibq_whatsapp_helpdesk.group_whatsapp_manager",
    "admin": "ibq_whatsapp_helpdesk.group_whatsapp_manager",
    "supervisor": "ibq_whatsapp_helpdesk.group_whatsapp_supervisor",
    "agent": "ibq_whatsapp_helpdesk.group_whatsapp_user",
}

# What each role is allowed to do inside the dashboard. Everything the UI
# offers is gated on one of these keys, and every dashboard method that
# changes something asserts the same key server-side.
ROLE_RIGHTS = {
    "owner": {
        "view_all_chats", "reassign", "transfer", "manage_roster", "manage_roles",
        "manage_settings", "manage_canned", "manage_tags", "block", "export",
        "delete_data", "view_all_reports",
    },
    "admin": {
        "view_all_chats", "reassign", "transfer", "manage_roster", "manage_roles",
        "manage_settings", "manage_canned", "manage_tags", "block", "export",
        "view_all_reports",
    },
    "supervisor": {
        "view_all_chats", "reassign", "transfer", "manage_canned", "manage_tags",
        "block", "export", "view_all_reports",
    },
    "agent": set(),
}


class WhatsappAgent(models.Model):
    """A person who answers WhatsApp chats, and what they are allowed to do.

    Separate from ``helpdesk.team.member_ids`` on purpose: someone can belong
    to a helpdesk team without working the WhatsApp queue, and the queue needs
    its own presence, capacity and role signals.

    The ``role`` field is the single place access is decided. Changing it also
    grants or revokes the matching Odoo security group, so there is no second
    place to keep in sync.
    """

    _name = "whatsapp.agent"
    _description = "WhatsApp Agent"
    _order = "role, sequence, id"
    _rec_name = "user_id"

    user_id = fields.Many2one(
        "res.users", required=True, ondelete="cascade", index=True,
        domain="[('share', '=', False)]",
    )
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    role = fields.Selection(
        [
            ("owner", "Owner"),
            ("admin", "Administrator"),
            ("supervisor", "Supervisor"),
            ("agent", "Agent"),
        ],
        default="agent", required=True, index=True,
        help="Owner: everything, including transferring ownership.\n"
             "Administrator: roster, settings, every chat.\n"
             "Supervisor: every chat in their departments, plus canned replies "
             "and tags.\n"
             "Agent: their own chats and their own presence.",
    )
    role_rights = fields.Char(compute="_compute_role_rights")

    team_ids = fields.Many2many(
        "helpdesk.team", string="Departments",
        help="Chats routed to these departments can reach this agent, and a "
             "supervisor only oversees the departments listed here. "
             "Leave empty to cover every department.",
    )
    account_ids = fields.Many2many(
        "whatsapp.account", string="Senders",
        help="WhatsApp numbers this agent covers. Leave empty to cover all.",
    )

    phone = fields.Char(
        "Their WhatsApp Number",
        help="This agent's own number, in E.164. Set it to let them run the "
             "queue by texting commands to the support number, e.g. "
             "'#assign 1042 sue'. Leave empty to disable that for them.",
    )

    display_alias = fields.Char(
        "Shown to Customers",
        help="Name used when this agent's replies are signed. Falls back to "
             "the user's own name.",
    )
    signature = fields.Char(
        help="Appended to this agent's outgoing messages, e.g. '— Sara, IBQ Support'.",
    )

    status = fields.Selection(
        [
            ("available", "Available"),
            ("busy", "Busy"),
            ("away", "Away"),
            ("offline", "Offline"),
        ],
        default="offline", required=True, index=True,
        help="Only Available agents receive automatically routed chats.",
    )
    auto_assign = fields.Boolean(
        "Accept Routed Chats", default=True,
        help="Uncheck to keep the agent in the roster but out of the rotation.",
    )
    max_active_chats = fields.Integer(
        "Capacity", default=5,
        help="How many open chats this agent handles at once before routing "
             "skips them.",
    )

    active_chat_count = fields.Integer(compute="_compute_workload")
    waiting_chat_count = fields.Integer(compute="_compute_workload")
    load_percent = fields.Integer(compute="_compute_workload")

    _sql_constraints = [
        ("user_uniq", "unique(user_id)",
         "This user is already in the WhatsApp agent roster."),
        ("phone_uniq", "unique(phone)",
         "Another agent already uses that WhatsApp number."),
    ]

    @api.onchange("phone")
    def _onchange_phone(self):
        if self.phone:
            self.phone = normalize_number(self.phone)

    @api.model
    def _normalise_phone(self, vals_list):
        """Store phones in E.164 whatever shape they were typed in."""
        for vals in vals_list:
            if vals.get("phone"):
                vals["phone"] = normalize_number(vals["phone"])
        return vals_list

    @api.model
    def _find_by_phone(self, number):
        """The roster entry that owns an inbound number, if any.

        Used to tell an agent texting a command apart from a customer texting
        for help.
        """
        number = normalize_number(number)
        if not number:
            return self.browse()
        return self.sudo().search([("phone", "=", number)], limit=1)

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    @api.constrains("max_active_chats")
    def _check_capacity(self):
        for agent in self:
            if agent.max_active_chats < 1:
                raise ValidationError(_("Capacity must be at least 1 chat."))

    @api.constrains("role")
    def _check_single_owner(self):
        if self.env.context.get("ibq_whatsapp_skip_owner_check"):
            return
        owners = self.sudo().search_count([("role", "=", "owner")])
        if owners > 1:
            raise ValidationError(_(
                "There can only be one Owner. Promote the new owner and the "
                "previous one becomes an Administrator automatically."
            ))

    # ------------------------------------------------------------------
    # Compute
    # ------------------------------------------------------------------
    def _compute_role_rights(self):
        for agent in self:
            agent.role_rights = ",".join(sorted(ROLE_RIGHTS.get(agent.role, ())))

    def _compute_workload(self):
        conversations = self.env["whatsapp.conversation"].sudo()
        open_groups = conversations._read_group(
            [("user_id", "in", self.user_id.ids), ("state", "=", "agent")],
            ["user_id"], ["__count"],
        )
        waiting_groups = conversations._read_group(
            [("user_id", "in", self.user_id.ids), ("state", "=", "agent"),
             ("needs_reply", "=", True)],
            ["user_id"], ["__count"],
        )
        open_map = {user.id: count for user, count in open_groups}
        waiting_map = {user.id: count for user, count in waiting_groups}
        for agent in self:
            active = open_map.get(agent.user_id.id, 0)
            agent.active_chat_count = active
            agent.waiting_chat_count = waiting_map.get(agent.user_id.id, 0)
            agent.load_percent = min(100, round(active * 100.0 / (agent.max_active_chats or 1)))

    # ------------------------------------------------------------------
    # Group synchronisation
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        # The very first person on the roster owns it, otherwise a fresh
        # install has nobody who can grant anyone else access.
        if not self.sudo().search_count([]):
            for index, vals in enumerate(vals_list):
                if index == 0 and not vals.get("role"):
                    vals["role"] = "owner"
        agents = super().create(self._normalise_phone(vals_list))
        agents._sync_user_groups()
        return agents

    def write(self, vals):
        if vals.get("role") == "owner":
            # Ownership is singular: promoting someone steps the old owner
            # down. This has to happen *before* the promotion is written,
            # because the constraint is checked when the write flushes and
            # would otherwise see two owners at once.
            demoted = self.sudo().search([
                ("role", "=", "owner"), ("id", "not in", self.ids),
            ])
            if demoted:
                super(WhatsappAgent, demoted.with_context(
                    ibq_whatsapp_skip_owner_check=True
                )).write({"role": "admin"})
                demoted._sync_user_groups()
        if vals.get("phone"):
            vals = dict(vals, phone=normalize_number(vals["phone"]))
        result = super().write(vals)
        if {"role", "active"} & set(vals):
            self._sync_user_groups()
        return result

    def unlink(self):
        users = self.user_id
        result = super().unlink()
        self._revoke_groups(users)
        return result

    def _whatsapp_groups(self):
        return {
            key: self.env.ref(xmlid, raise_if_not_found=False)
            for key, xmlid in {
                "user": "ibq_whatsapp_helpdesk.group_whatsapp_user",
                "supervisor": "ibq_whatsapp_helpdesk.group_whatsapp_supervisor",
                "manager": "ibq_whatsapp_helpdesk.group_whatsapp_manager",
            }.items()
        }

    def _sync_user_groups(self):
        """Grant exactly the group the role implies, and drop the others.

        Only the three WhatsApp groups are touched; whatever else the user has
        is left alone.
        """
        groups = self._whatsapp_groups()
        for agent in self:
            target = self.env.ref(ROLE_GROUPS[agent.role], raise_if_not_found=False)
            if not target:
                continue
            commands = []
            for group in groups.values():
                if not group:
                    continue
                if group == target and agent.active:
                    commands.append((4, group.id))
                elif group != target:
                    commands.append((3, group.id))
            if not agent.active:
                commands = [(3, g.id) for g in groups.values() if g]
            if commands:
                agent.user_id.sudo().write({"groups_id": commands})

    def _revoke_groups(self, users):
        groups = [g for g in self._whatsapp_groups().values() if g]
        if users and groups:
            users.sudo().write({"groups_id": [(3, g.id) for g in groups]})

    # ------------------------------------------------------------------
    # Permissions
    # ------------------------------------------------------------------
    def has_right(self, right):
        self.ensure_one()
        return right in ROLE_RIGHTS.get(self.role, set())

    def can_act_on(self, other):
        """Whether this agent may change another roster entry's role.

        An owner may act on anyone. An admin may act on supervisors and agents
        but not on another admin or the owner, so administrators cannot lock
        each other out.
        """
        self.ensure_one()
        if self == other:
            return self.role == "owner"
        if self.role == "owner":
            return True
        if self.role == "admin":
            return other.role in ("supervisor", "agent")
        return False

    @api.model
    def _for_user(self, user=None):
        user = user or self.env.user
        return self.sudo().search([("user_id", "=", user.id)], limit=1)

    @api.model
    def _current(self):
        """The roster entry for the caller, or an empty recordset."""
        return self._for_user(self.env.user)

    @api.model
    def _assert_right(self, right):
        agent = self._current()
        if agent and agent.has_right(right):
            return agent
        # A system administrator who is not on the roster still gets in, so an
        # install is never locked out of its own configuration.
        if self.env.user.has_group("base.group_system"):
            return agent
        raise AccessError(_(
            "Your WhatsApp role does not allow this (%s)."
        ) % right.replace("_", " "))

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    @api.model
    def _route(self, conversation):
        """Pick the least loaded available agent for a conversation.

        Returns an empty recordset when nobody qualifies, which leaves the chat
        unassigned in the queue rather than dumping it on someone at capacity.
        """
        candidates = self.sudo().search([
            ("status", "=", "available"), ("auto_assign", "=", True),
        ])
        if conversation.team_id:
            candidates = candidates.filtered(
                lambda a: not a.team_ids or conversation.team_id in a.team_ids
            )
        if conversation.account_id:
            candidates = candidates.filtered(
                lambda a: not a.account_ids or conversation.account_id in a.account_ids
            )
        candidates = candidates.filtered(
            lambda a: a.active_chat_count < a.max_active_chats
        )
        if not candidates:
            return self.browse()
        return min(candidates, key=lambda a: (a.load_percent, a.active_chat_count, a.id))

    # ------------------------------------------------------------------
    # Presence
    # ------------------------------------------------------------------
    def _notify_added(self):
        """Tell each new agent, in Odoo, that they are on the WhatsApp roster."""
        for agent in self:
            agent.user_id.sudo().partner_id.message_post(
                body=_(
                    "You were added to the WhatsApp team as %(role)s by %(who)s. "
                    "Open WhatsApp &gt; Dashboard and set yourself Available to "
                    "start receiving chats.",
                    role=dict(self._fields["role"].selection)[agent.role],
                    who=self.env.user.name,
                ),
                subtype_xmlid="mail.mt_comment",
                partner_ids=[agent.user_id.partner_id.id],
            )
        return True

    def action_set_available(self):
        return self.write({"status": "available"})

    def action_set_offline(self):
        return self.write({"status": "offline"})

    def _dashboard_payload(self):
        """Shape used by the dashboard roster."""
        self.ensure_one()
        me = self._current()
        return {
            "id": self.id,
            "user_id": self.user_id.id,
            "name": self.user_id.name,
            "alias": self.display_alias or self.user_id.name,
            "email": self.user_id.email or "",
            "avatar": "/web/image/res.users/%s/avatar_128" % self.user_id.id,
            "role": self.role,
            "role_label": dict(self._fields["role"].selection)[self.role],
            "status": self.status,
            "auto_assign": self.auto_assign,
            "active_chats": self.active_chat_count,
            "waiting_chats": self.waiting_chat_count,
            "capacity": self.max_active_chats,
            "load_percent": self.load_percent,
            "departments": self.team_ids.mapped("name"),
            "department_ids": self.team_ids.ids,
            "is_me": self.user_id == self.env.user,
            # A system administrator who never joined the roster still has to
            # be able to appoint the first Owner, so fall back to their Odoo
            # group rather than leaving every row read-only.
            "can_edit": bool(me.can_act_on(self)) if me
                        else self.env.user.has_group("base.group_system"),
        }
