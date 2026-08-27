# -*- coding: utf-8 -*-
import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")


class WhatsappBotFlow(models.Model):
    """A scripted conversation played to inbound WhatsApp chats.

    A flow is a small state machine: the conversation stores a pointer to the
    current step plus the answers collected so far, and each inbound message
    advances the pointer. Everything is configured from the UI, so support
    leads can change the script without touching Python.
    """

    _name = "whatsapp.bot.flow"
    _description = "WhatsApp Bot Flow"
    _order = "name"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    step_ids = fields.One2many("whatsapp.bot.step", "flow_id", string="Steps")
    start_step_id = fields.Many2one(
        "whatsapp.bot.step", string="First Step",
        domain="[('flow_id', '=', id)]",
        help="Step played when a chat starts, or restarts after being idle.",
    )
    greeting = fields.Text(
        help="Optional one-off line sent before the first step, e.g. "
             "'Hello {name}, welcome to IBQ Support.' Supports {name}.",
    )
    fallback_message = fields.Text(
        default="Sorry, I did not get that. Please reply with one of the "
                "options above, or type AGENT to reach a person.",
        help="Sent when a reply matches no option on a menu step.",
    )
    max_invalid_attempts = fields.Integer(
        default=3,
        help="After this many unmatched replies in a row, the chat is handed "
             "to an agent instead of looping.",
    )
    agent_keywords = fields.Char(
        default="agent,human,operator,help me,talk to agent",
        help="Comma-separated. Any of these, at any point, hands the chat to an agent.",
    )
    close_keywords = fields.Char(
        default="stop,end,bye,close",
        help="Comma-separated. Any of these closes the conversation.",
    )
    restart_keywords = fields.Char(
        default="menu,restart,start over",
        help="Comma-separated. Any of these returns the chat to the first step.",
    )

    def _keywords(self, field_name):
        self.ensure_one()
        raw = self[field_name] or ""
        return [word.strip().lower() for word in raw.split(",") if word.strip()]

    @api.constrains("start_step_id", "step_ids")
    def _check_start_step(self):
        for flow in self:
            if flow.start_step_id and flow.start_step_id.flow_id != flow:
                raise ValidationError(_("The first step must belong to this flow."))


class WhatsappBotStep(models.Model):
    _name = "whatsapp.bot.step"
    _description = "WhatsApp Bot Step"
    _order = "flow_id, sequence, id"

    flow_id = fields.Many2one(
        "whatsapp.bot.flow", required=True, ondelete="cascade", index=True
    )
    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    code = fields.Char(help="Optional technical key, handy when scripting.")
    step_type = fields.Selection(
        [
            ("message", "Send a message"),
            ("menu", "Ask a multiple choice"),
            ("question", "Ask and store an answer"),
            ("ticket", "Create a helpdesk ticket"),
            ("agent", "Hand over to an agent"),
            ("end", "Close the conversation"),
        ],
        default="message", required=True,
    )
    body = fields.Text(
        help="Text sent to the customer. Supports {name} and {answer_key} "
             "placeholders filled from the collected answers.",
    )
    option_ids = fields.One2many("whatsapp.bot.option", "step_id", string="Options")
    next_step_id = fields.Many2one(
        "whatsapp.bot.step", string="Next Step",
        domain="[('flow_id', '=', flow_id), ('id', '!=', id)]",
        help="Where to go once this step is done. Leave empty to stop here.",
    )

    # -- question steps ----------------------------------------------------
    answer_key = fields.Char(
        help="Key the customer's reply is stored under, e.g. 'subject' or "
             "'order_ref'. Reusable in later bodies as {order_ref}.",
    )
    answer_format = fields.Selection(
        [("text", "Any text"), ("email", "Email"), ("number", "Digits only")],
        default="text",
    )
    answer_error = fields.Char(
        default="That does not look right, could you send it again?",
        help="Sent when the reply fails the expected format.",
    )

    # -- ticket steps ------------------------------------------------------
    team_id = fields.Many2one("helpdesk.team", string="Helpdesk Team")
    ticket_priority = fields.Selection(
        [("0", "Low"), ("1", "Medium"), ("2", "High"), ("3", "Urgent")],
        default="1",
    )
    tag_ids = fields.Many2many("helpdesk.tag", string="Ticket Tags")
    subject_key = fields.Char(
        default="subject",
        help="Answer key used as the ticket title. Falls back to the step name.",
    )

    def _render_body(self, conversation):
        """Substitute {name} / {answer_key} placeholders from the conversation."""
        self.ensure_one()
        text = self.body or ""
        if self.step_type == "menu" and self.option_ids:
            lines = ["%s. %s" % (option.key, option.name)
                     for option in self.option_ids.sorted("sequence")]
            text = (text + "\n\n" + "\n".join(lines)).strip()
        return conversation._format_text(text)

    def _match_option(self, text):
        """Find the option a customer's reply selects.

        Matches the option key ('2'), or any of its keywords, case-insensitive.
        """
        self.ensure_one()
        needle = (text or "").strip().lower()
        if not needle:
            return self.env["whatsapp.bot.option"]
        for option in self.option_ids.sorted("sequence"):
            if needle == (option.key or "").strip().lower():
                return option
            keywords = [k.strip().lower()
                        for k in (option.keywords or "").split(",") if k.strip()]
            if needle in keywords or needle == (option.name or "").strip().lower():
                return option
        return self.env["whatsapp.bot.option"]

    def _validate_answer(self, text):
        value = (text or "").strip()
        if not value:
            return False
        if self.answer_format == "email":
            return bool(EMAIL_RE.match(value))
        if self.answer_format == "number":
            return value.replace(" ", "").isdigit()
        return True


class WhatsappBotOption(models.Model):
    _name = "whatsapp.bot.option"
    _description = "WhatsApp Bot Menu Option"
    _order = "sequence, id"

    step_id = fields.Many2one(
        "whatsapp.bot.step", required=True, ondelete="cascade", index=True
    )
    sequence = fields.Integer(default=10)
    key = fields.Char(
        required=True,
        help="What the customer types to pick this option, usually a digit.",
    )
    name = fields.Char(required=True, help="Label shown next to the key.")
    keywords = fields.Char(
        help="Comma-separated alternatives that also select this option, "
             "e.g. 'billing,invoice,payment'.",
    )
    next_step_id = fields.Many2one(
        "whatsapp.bot.step", string="Go To",
        help="Step played when this option is picked.",
    )
    answer_key = fields.Char(
        help="Store the option label under this key, e.g. 'category'.",
    )

    _sql_constraints = [
        ("key_uniq", "unique(step_id, key)",
         "Two options on the same step cannot share a key."),
    ]
