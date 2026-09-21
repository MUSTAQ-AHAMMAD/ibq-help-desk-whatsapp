# -*- coding: utf-8 -*-
"""Just enough Helpdesk for the WhatsApp module to install on Community.

Only the fields ``ibq_whatsapp_helpdesk`` actually reads or writes are defined
here. Anything the real Enterprise app does beyond that — SLA policies, ratings,
timesheets, the portal, the website form — is out of scope on purpose.
"""
from odoo import api, fields, models


class HelpdeskTeam(models.Model):
    _name = "helpdesk.team"
    _description = "Helpdesk Team"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        "res.company", default=lambda self: self.env.company
    )
    member_ids = fields.Many2many("res.users", string="Members")
    stage_ids = fields.Many2many("helpdesk.stage", string="Stages")
    ticket_count = fields.Integer(compute="_compute_ticket_count")

    def _compute_ticket_count(self):
        groups = self.env["helpdesk.ticket"]._read_group(
            [("team_id", "in", self.ids)], ["team_id"], ["__count"]
        )
        mapped = {team.id: count for team, count in groups}
        for team in self:
            team.ticket_count = mapped.get(team.id, 0)


class HelpdeskStage(models.Model):
    _name = "helpdesk.stage"
    _description = "Helpdesk Stage"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    fold = fields.Boolean()
    team_ids = fields.Many2many("helpdesk.team", string="Teams")


class HelpdeskTag(models.Model):
    _name = "helpdesk.tag"
    _description = "Helpdesk Tag"
    _order = "name"

    name = fields.Char(required=True)
    color = fields.Integer()

    _sql_constraints = [("name_uniq", "unique(name)", "Tag already exists.")]


class HelpdeskTicket(models.Model):
    _name = "helpdesk.ticket"
    _description = "Helpdesk Ticket"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "priority desc, id desc"

    name = fields.Char("Subject", required=True, tracking=True)
    description = fields.Html()
    team_id = fields.Many2one("helpdesk.team", string="Team", tracking=True)
    stage_id = fields.Many2one(
        "helpdesk.stage", string="Stage", tracking=True,
        group_expand="_read_group_stage_ids", default=lambda self: self._default_stage(),
    )
    user_id = fields.Many2one("res.users", string="Assigned to", tracking=True)
    partner_id = fields.Many2one("res.partner", string="Customer", tracking=True)
    partner_email = fields.Char("Email")
    partner_phone = fields.Char("Phone")
    priority = fields.Selection(
        [("0", "Low"), ("1", "Medium"), ("2", "High"), ("3", "Urgent")],
        default="1",
    )
    tag_ids = fields.Many2many("helpdesk.tag", string="Tags")
    company_id = fields.Many2one(
        "res.company", default=lambda self: self.env.company
    )

    @api.model
    def _default_stage(self):
        return self.env["helpdesk.stage"].search([], order="sequence", limit=1)

    @api.model
    def _read_group_stage_ids(self, stages, domain, order=None):
        return stages.search([], order="sequence")
