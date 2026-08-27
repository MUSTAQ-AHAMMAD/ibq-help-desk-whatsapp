# -*- coding: utf-8 -*-
import logging

from odoo import _, api, fields, models
from odoo.tools import html2plaintext

_logger = logging.getLogger(__name__)


class HelpdeskTeam(models.Model):
    _inherit = "helpdesk.team"

    whatsapp_account_id = fields.Many2one(
        "whatsapp.account", string="WhatsApp Account",
        help="Account used when this team messages a customer.",
    )
    whatsapp_log_mode = fields.Selection(
        [
            ("all", "Every message, including the bot"),
            ("human", "Customer and agent messages only"),
            ("none", "Do not log to the chatter"),
        ],
        string="Log WhatsApp in the Chatter", default="all", required=True,
        help="What gets mirrored into the ticket's chatter. The full "
             "transcript is always on the ticket regardless; this only "
             "controls how much of it also becomes chatter history.",
    )
    whatsapp_relay_chatter = fields.Boolean(
        "Relay Chatter to WhatsApp", default=True,
        help="Send public messages posted in a ticket's chatter to the customer "
             "over WhatsApp, when the ticket came from WhatsApp and the 24h "
             "session is still open.",
    )


class HelpdeskStage(models.Model):
    _inherit = "helpdesk.stage"

    whatsapp_template_id = fields.Many2one(
        "whatsapp.template", string="WhatsApp Template",
        help="Sent to the customer when a WhatsApp ticket reaches this stage.",
    )
    whatsapp_close_conversation = fields.Boolean(
        "Close WhatsApp Chat",
        help="Close the linked WhatsApp conversation when a ticket lands here.",
    )


class HelpdeskTicket(models.Model):
    _inherit = "helpdesk.ticket"

    whatsapp_conversation_id = fields.Many2one(
        "whatsapp.conversation", string="WhatsApp Chat", copy=False, index=True
    )
    whatsapp_number = fields.Char("WhatsApp Number", copy=False)
    whatsapp_message_ids = fields.One2many(
        "whatsapp.message", "ticket_id", string="WhatsApp Messages"
    )
    whatsapp_message_count = fields.Integer(compute="_compute_whatsapp_state")
    whatsapp_transcript_synced = fields.Boolean(
        compute="_compute_whatsapp_state",
        help="Every WhatsApp message on this ticket also appears in the chatter.",
    )
    whatsapp_in_session = fields.Boolean(
        compute="_compute_whatsapp_state",
        help="True while WhatsApp still allows free-form replies to this customer.",
    )

    @api.depends(
        "whatsapp_message_ids",
        "whatsapp_conversation_id.last_inbound_date",
        "whatsapp_conversation_id.state",
    )
    def _compute_whatsapp_state(self):
        for ticket in self:
            ticket.whatsapp_message_count = len(ticket.whatsapp_message_ids)
            ticket.whatsapp_in_session = bool(
                ticket.whatsapp_conversation_id.in_session
            )
            unlogged = ticket.whatsapp_message_ids.filtered(
                lambda m: not m.chatter_message_id
            )
            ticket.whatsapp_transcript_synced = not unlogged

    # ------------------------------------------------------------------
    # Chatter relay
    # ------------------------------------------------------------------
    @api.returns("mail.message", lambda value: value.id)
    def message_post(self, **kwargs):
        message = super().message_post(**kwargs)
        if self.env.context.get("ibq_whatsapp_relay_skip"):
            return message
        self._relay_message_to_whatsapp(message)
        return message

    def _relay_message_to_whatsapp(self, message):
        """Push an agent's public chatter reply out over WhatsApp.

        Internal notes and system logs stay in Odoo; only real comments reach
        the customer, and only while the session window is open.
        """
        self.ensure_one()
        conversation = self.whatsapp_conversation_id
        if not conversation or conversation.state == "closed":
            return
        if not self.team_id.whatsapp_relay_chatter:
            return
        comment_subtype = self.env.ref("mail.mt_comment", raise_if_not_found=False)
        if not comment_subtype or message.subtype_id != comment_subtype:
            return
        if message.author_id and message.author_id == conversation.partner_id:
            # Came from the customer already; do not echo it back.
            return
        body = html2plaintext(message.body or "").strip()
        if not body:
            return
        if not conversation.in_session:
            self.with_context(ibq_whatsapp_relay_skip=True).message_post(
                body=_(
                    "This reply was not sent over WhatsApp: the 24h session with "
                    "%s has expired. Use an approved template instead."
                ) % conversation.number,
                subtype_xmlid="mail.mt_note",
            )
            return
        try:
            conversation.send_text(body)
        except Exception as exc:  # never let a relay failure block the chatter
            _logger.warning("WhatsApp relay failed for ticket %s: %s", self.id, exc)

    # ------------------------------------------------------------------
    # Stage notifications
    # ------------------------------------------------------------------
    def write(self, vals):
        result = super().write(vals)
        if "stage_id" in vals:
            self._notify_whatsapp_stage_change()
        return result

    def _notify_whatsapp_stage_change(self):
        for ticket in self.filtered("whatsapp_conversation_id"):
            stage = ticket.stage_id
            if stage.whatsapp_template_id:
                ticket.whatsapp_conversation_id.send_template(
                    stage.whatsapp_template_id, record=ticket
                )
            if stage.whatsapp_close_conversation:
                ticket.whatsapp_conversation_id._close(notify=False)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_send_whatsapp(self):
        self.ensure_one()
        number = self.whatsapp_number or self.partner_id.mobile or self.partner_id.phone
        account = (
            self.whatsapp_conversation_id.account_id
            or self.team_id.whatsapp_account_id
            or self.env["whatsapp.account"]._get_default_account()
        )
        return {
            "type": "ir.actions.act_window",
            "res_model": "whatsapp.compose.message",
            "view_mode": "form",
            "target": "new",
            "name": _("Send WhatsApp Message"),
            "context": {
                "default_ticket_id": self.id,
                "default_conversation_id": self.whatsapp_conversation_id.id,
                "default_number": number,
                "default_account_id": account.id if account else False,
                "default_partner_id": self.partner_id.id,
            },
        }

    def action_sync_whatsapp_transcript(self):
        """Copy any WhatsApp message that is not yet in the chatter.

        Needed when a ticket is linked to a chat after the fact, and as a
        repair for anything written straight to the database.
        """
        for ticket in self:
            pending = ticket.whatsapp_message_ids.filtered(
                lambda m: not m.chatter_message_id
            ).sorted("id")
            pending._post_to_ticket()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": _("Transcript synced"),
                "message": _("The WhatsApp conversation is now in the chatter."),
                "sticky": False,
            },
        }

    def action_view_whatsapp_conversation(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "whatsapp.conversation",
            "res_id": self.whatsapp_conversation_id.id,
            "view_mode": "form",
        }
