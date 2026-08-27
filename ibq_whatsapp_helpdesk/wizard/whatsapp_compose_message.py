# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.whatsapp_account import normalize_number


class WhatsappComposeMessage(models.TransientModel):
    _name = "whatsapp.compose.message"
    _description = "Send a WhatsApp Message"

    account_id = fields.Many2one("whatsapp.account", required=True)
    conversation_id = fields.Many2one("whatsapp.conversation")
    ticket_id = fields.Many2one("helpdesk.ticket")
    partner_id = fields.Many2one("res.partner", string="Contact")
    number = fields.Char("To", required=True)

    composition_mode = fields.Selection(
        [("text", "Free text"), ("template", "Template")],
        default="text", required=True,
    )
    template_id = fields.Many2one(
        "whatsapp.template", string="Template",
        domain="['|', ('account_id', '=', account_id), ('account_id', '=', False)]",
    )
    body = fields.Text("Message")
    in_session = fields.Boolean(compute="_compute_session_state")
    session_warning = fields.Char(compute="_compute_session_state")

    @api.depends("conversation_id", "conversation_id.last_inbound_date", "composition_mode")
    def _compute_session_state(self):
        for wizard in self:
            wizard.in_session = bool(wizard.conversation_id.in_session)
            if wizard.conversation_id and not wizard.in_session:
                wizard.session_warning = _(
                    "The 24h session with %s has expired. Only an approved "
                    "template can be delivered right now."
                ) % wizard.number
            else:
                wizard.session_warning = False

    @api.onchange("number")
    def _onchange_number(self):
        if self.number:
            self.number = normalize_number(self.number)

    @api.onchange("template_id")
    def _onchange_template_id(self):
        if not self.template_id:
            return
        self.composition_mode = "template"
        record = self.ticket_id or self.conversation_id
        self.body = self.template_id.render(record)[0]

    @api.onchange("account_id", "number")
    def _onchange_find_conversation(self):
        if self.conversation_id or not (self.account_id and self.number):
            return
        self.conversation_id = self.env["whatsapp.conversation"].search([
            ("account_id", "=", self.account_id.id),
            ("number", "=", normalize_number(self.number)),
        ], limit=1)

    def action_send(self):
        self.ensure_one()
        if not self.number:
            raise UserError(_("A destination number is required."))
        conversation = self.conversation_id
        if not conversation:
            conversation = self.env["whatsapp.conversation"]._get_or_create(
                self.account_id, self.number,
                self.partner_id.name if self.partner_id else None,
            )
            conversation.state = "agent"
        if self.ticket_id and not self.ticket_id.whatsapp_conversation_id:
            self.ticket_id.write({
                "whatsapp_conversation_id": conversation.id,
                "whatsapp_number": conversation.number,
            })
            conversation.ticket_id = self.ticket_id

        if self.composition_mode == "template":
            if not self.template_id:
                raise UserError(_("Pick a template."))
            conversation.send_template(
                self.template_id, record=self.ticket_id or conversation
            )
        else:
            if not (self.body or "").strip():
                raise UserError(_("The message is empty."))
            conversation.send_text(self.body)
        return {"type": "ir.actions.act_window_close"}
