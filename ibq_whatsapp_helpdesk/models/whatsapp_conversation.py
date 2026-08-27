# -*- coding: utf-8 -*-
import json
import logging
import re
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext

from .whatsapp_account import normalize_number

_logger = logging.getLogger(__name__)

PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
MAX_BOT_HOPS = 10


class WhatsappConversation(models.Model):
    """One WhatsApp thread with one phone number.

    The conversation is the unit the bot runs on: it holds the pointer to the
    current bot step, the answers collected so far, and the link to whatever
    helpdesk ticket came out of the chat.
    """

    _name = "whatsapp.conversation"
    _description = "WhatsApp Conversation"
    _inherit = ["mail.thread"]
    _order = "last_message_date desc, id desc"

    name = fields.Char(compute="_compute_name", store=True)
    active = fields.Boolean(default=True)
    account_id = fields.Many2one(
        "whatsapp.account", required=True, index=True, ondelete="restrict"
    )
    number = fields.Char(required=True, index=True, tracking=True)
    profile_name = fields.Char(
        "WhatsApp Name", help="Display name reported by WhatsApp for this number."
    )
    partner_id = fields.Many2one("res.partner", string="Contact", index=True, tracking=True)
    ticket_id = fields.Many2one("helpdesk.ticket", string="Ticket", index=True, tracking=True)
    team_id = fields.Many2one("helpdesk.team", string="Helpdesk Team")
    user_id = fields.Many2one(
        "res.users", string="Assigned Agent", tracking=True,
        help="Agent currently handling this chat.",
    )

    state = fields.Selection(
        [
            ("bot", "Bot"),
            ("agent", "With Agent"),
            ("closed", "Closed"),
        ],
        default="bot", required=True, index=True, tracking=True,
    )
    whatsapp_message_ids = fields.One2many(
        "whatsapp.message", "conversation_id", string="WhatsApp Messages"
    )
    # Deliberately two compute methods, not one: mixing a stored and a
    # non-stored field in the same group means reading the counter would
    # recompute and rewrite the date.
    message_count = fields.Integer(compute="_compute_message_count")
    last_message_date = fields.Datetime(compute="_compute_last_message_date", store=True)
    last_inbound_date = fields.Datetime(readonly=True, copy=False)
    last_outbound_date = fields.Datetime(readonly=True, copy=False)
    needs_reply = fields.Boolean(
        default=False, index=True,
        help="The customer sent something an agent has not answered yet.",
    )

    # -- bot state ---------------------------------------------------------
    bot_flow_id = fields.Many2one("whatsapp.bot.flow", string="Bot Flow")
    bot_step_id = fields.Many2one("whatsapp.bot.step", string="Current Step")
    bot_pending_step_id = fields.Many2one(
        "whatsapp.bot.step", string="Awaiting Answer For",
        help="Set while the bot waits for a reply to a question or menu step.",
    )
    answers = fields.Text(
        default="{}", help="JSON map of answers collected by the bot flow."
    )
    invalid_count = fields.Integer(default=0)

    # -- triage ------------------------------------------------------------
    tag_ids = fields.Many2many("whatsapp.tag", string="Tags")
    issue_id = fields.Many2one(
        "whatsapp.issue", string="Issue", index=True, ondelete="set null",
        help="What this chat was about, grouped with every other chat about "
             "the same thing.",
    )
    priority = fields.Selection(
        [("0", "Low"), ("1", "Normal"), ("2", "High"), ("3", "Urgent")],
        default="1", index=True, tracking=True,
    )

    # -- satisfaction ------------------------------------------------------
    rating_id = fields.Many2one(
        "whatsapp.rating", string="Rating", readonly=True, copy=False
    )
    rating_score = fields.Integer(
        related="rating_id.score_value", store=True, string="Score"
    )
    awaiting_rating = fields.Boolean(
        readonly=True, copy=False,
        help="A satisfaction question was sent and the next 1-5 reply is the answer.",
    )

    # -- session window ----------------------------------------------------
    session_expiry = fields.Datetime(compute="_compute_session", store=False)
    in_session = fields.Boolean(compute="_compute_session", store=False)

    # -- service metrics ---------------------------------------------------
    # Stamped as they happen rather than derived on read, so the dashboard can
    # aggregate them in SQL instead of walking every message.
    handoff_date = fields.Datetime(
        readonly=True, copy=False,
        help="When the chat first reached a human.",
    )
    first_agent_reply_date = fields.Datetime(readonly=True, copy=False)
    first_response_seconds = fields.Integer(
        "First Response (s)", readonly=True, copy=False,
        help="Seconds between the hand-off and the first agent reply.",
    )
    closed_date = fields.Datetime(readonly=True, copy=False)
    resolution_seconds = fields.Integer("Resolution (s)", readonly=True, copy=False)
    bot_resolved = fields.Boolean(
        readonly=True, copy=False,
        help="Closed without ever needing an agent.",
    )

    _sql_constraints = [
        ("number_account_uniq", "unique(number, account_id)",
         "There is already a conversation with this number on this account."),
    ]

    # ------------------------------------------------------------------
    # Compute
    # ------------------------------------------------------------------
    @api.depends("partner_id", "profile_name", "number")
    def _compute_name(self):
        for record in self:
            label = record.partner_id.display_name or record.profile_name
            record.name = "%s (%s)" % (label, record.number) if label else record.number

    @api.depends("whatsapp_message_ids")
    def _compute_message_count(self):
        for record in self:
            record.message_count = len(record.whatsapp_message_ids)

    @api.depends("whatsapp_message_ids", "whatsapp_message_ids.create_date")
    def _compute_last_message_date(self):
        for record in self:
            record.last_message_date = max(
                record.whatsapp_message_ids.mapped("create_date") or [False]
            ) or record.create_date

    @api.depends("last_inbound_date", "account_id.session_hours")
    def _compute_session(self):
        now = fields.Datetime.now()
        for record in self:
            hours = record.account_id.session_hours or 24
            if record.last_inbound_date:
                expiry = record.last_inbound_date + timedelta(hours=hours)
                record.session_expiry = expiry
                record.in_session = expiry > now
            else:
                record.session_expiry = False
                record.in_session = False

    # ------------------------------------------------------------------
    # Assignment
    # ------------------------------------------------------------------
    def write(self, vals):
        """Keep the linked ticket in step with the chat.

        Assigning the conversation is the same act as assigning the ticket, so
        doing it anywhere -- the dashboard, the list view, the form, a texted
        command -- has to land in both places. Before this, only the dashboard
        kept them together and the form view quietly did not.
        """
        notify = {}
        if "user_id" in vals and not self.env.context.get("ibq_whatsapp_no_notify"):
            for record in self:
                if record.user_id.id != vals["user_id"]:
                    notify[record.id] = record.user_id

        result = super().write(vals)

        if "user_id" in vals or "team_id" in vals:
            for record in self.filtered("ticket_id"):
                ticket_vals = {}
                if "user_id" in vals:
                    ticket_vals["user_id"] = record.user_id.id or False
                if "team_id" in vals and record.team_id:
                    ticket_vals["team_id"] = record.team_id.id
                if ticket_vals:
                    record.ticket_id.sudo().write(ticket_vals)

        for record in self:
            if record.id in notify:
                record._announce_assignment(notify[record.id])
        return result

    def assign_to(self, user, source=None):
        """Give the chat (and its ticket) to someone, with a trail.

        ``source`` names where the assignment came from, so the ticket note
        says whether it was the dashboard, a list view, or a texted command.
        """
        self.ensure_one()
        if user and user != self.user_id:
            # The test is whether they can work the queue at all, not whether
            # they have a roster row: a system administrator holds the group
            # without being on the roster, and assigning a ticket to someone
            # who cannot open it helps nobody.
            if not user.has_group("ibq_whatsapp_helpdesk.group_whatsapp_user"):
                raise UserError(_(
                    "%s has no WhatsApp access, so they cannot take a chat. "
                    "Add them to the roster first."
                ) % user.name)
            if user != self.env.user:
                self.env["whatsapp.agent"]._assert_right("reassign")

        previous = self.user_id
        self.user_id = user.id if user else False
        if self.ticket_id:
            self.ticket_id.sudo().message_post(
                body=_(
                    "Assigned to %(who)s by %(by)s%(via)s.",
                    who=user.name if user else _("nobody"),
                    by=self.env.user.name,
                    via=_(" via %s") % source if source else "",
                ),
                subtype_xmlid="mail.mt_note",
            )
        self._notify_dashboard("assigned")
        return previous

    def _announce_assignment(self, previous_user):
        """Tell the customer who is looking after them now.

        Only inside the 24h window -- outside it this would need an approved
        template, and a routing detail is not worth burning one on. Silent for
        chats the bot still owns, which have their own hand-off message.
        """
        self.ensure_one()
        if self.state != "agent" or not self.user_id or not self.in_session:
            return False
        agent = self.env["whatsapp.agent"].sudo().search(
            [("user_id", "=", self.user_id.id)], limit=1
        )
        name = agent.display_alias or self.user_id.name
        if previous_user:
            body = _("%s is taking over from here.") % name
        else:
            body = _("%s will be looking after this.") % name
        self.send_text(body, is_bot=True, force=True)
        return True

    # ------------------------------------------------------------------
    # Answers helpers
    # ------------------------------------------------------------------
    def _get_answers(self):
        self.ensure_one()
        try:
            return json.loads(self.answers or "{}")
        except ValueError:
            return {}

    def _set_answer(self, key, value):
        self.ensure_one()
        if not key:
            return
        data = self._get_answers()
        data[key] = value
        self.answers = json.dumps(data)

    def _format_text(self, text):
        """Fill {placeholders} from the collected answers plus a few builtins."""
        self.ensure_one()
        values = self._get_answers()
        values.setdefault("name", self.partner_id.name or self.profile_name or "")
        values.setdefault("number", self.number or "")
        values.setdefault("ticket", self.ticket_id.display_name or "")
        values.setdefault(
            "ticket_ref", self.ticket_id and str(self.ticket_id.id) or ""
        )
        return PLACEHOLDER_RE.sub(
            lambda match: str(values.get(match.group(1), match.group(0))), text or ""
        )

    # ------------------------------------------------------------------
    # Lookup / creation
    # ------------------------------------------------------------------
    @api.model
    def _get_or_create(self, account, number, profile_name=None):
        number = normalize_number(number)
        # active_test=False: an archived chat must be reused rather than
        # re-created, or the unique constraint below blows up on intake.
        conversation = self.sudo().with_context(active_test=False).search([
            ("number", "=", number),
            ("account_id", "=", account.id),
        ], limit=1)
        if conversation:
            values = {}
            if profile_name and not conversation.profile_name:
                values["profile_name"] = profile_name
            if not conversation.active:
                values["active"] = True
            if values:
                conversation.write(values)
            return conversation
        partner = self.env["res.partner"]._find_or_create_from_whatsapp(
            number, profile_name, create=account.unknown_contact_action == "create"
        )
        return self.sudo().create({
            "account_id": account.id,
            "number": number,
            "profile_name": profile_name,
            "partner_id": partner.id if partner else False,
            "team_id": account.team_id.id,
            "bot_flow_id": account.bot_flow_id.id,
            "state": "bot" if account.bot_flow_id else "agent",
        })

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------
    def _check_session(self):
        """Raise unless free-form text is currently allowed by WhatsApp."""
        self.ensure_one()
        if not self.in_session:
            raise UserError(_(
                "The 24h WhatsApp session with %(number)s has expired "
                "(last message received %(when)s). Send an approved template instead.",
                number=self.number,
                when=self.last_inbound_date or _("never"),
            ))

    def send_text(self, body, is_bot=False, force=False):
        """Queue a free-form text message and send it immediately."""
        self.ensure_one()
        if not (body or "").strip():
            return self.env["whatsapp.message"]
        if not force:
            self._check_session()
        message = self.env["whatsapp.message"].create({
            "conversation_id": self.id,
            "account_id": self.account_id.id,
            "direction": "outbound",
            "number": self.number,
            "partner_id": self.partner_id.id,
            "body": body,
            "message_type": "text",
            "is_bot": is_bot,
        })
        message.action_send()
        values = {"last_outbound_date": fields.Datetime.now()}
        if not is_bot:
            values["needs_reply"] = False
            if self.handoff_date and not self.first_agent_reply_date:
                now = fields.Datetime.now()
                values["first_agent_reply_date"] = now
                values["first_response_seconds"] = int(
                    (now - self.handoff_date).total_seconds()
                )
        self.write(values)
        self._notify_dashboard("message")
        return message

    def send_template(self, template, record=None, values=None):
        """Queue an approved template. Works outside the session window."""
        self.ensure_one()
        if isinstance(template, str):
            template = self.env["whatsapp.template"]._get_by_code(
                template, self.account_id
            )
        if not template:
            _logger.info("No WhatsApp template found for conversation %s", self.id)
            return self.env["whatsapp.message"]
        body, content_variables = template.render(
            record if record is not None else (self.ticket_id or self), values
        )
        message = self.env["whatsapp.message"].create({
            "conversation_id": self.id,
            "account_id": self.account_id.id,
            "direction": "outbound",
            "number": self.number,
            "partner_id": self.partner_id.id,
            "body": body,
            "message_type": "template" if template.content_sid else "text",
            "template_id": template.id,
            "content_variables": content_variables,
            "is_bot": True,
        })
        message.action_send()
        self.write({"last_outbound_date": fields.Datetime.now()})
        return message

    # ------------------------------------------------------------------
    # Inbound entry point
    # ------------------------------------------------------------------
    def _handle_inbound(self, message):
        """React to one freshly stored inbound message."""
        self.ensure_one()
        self.write({
            "last_inbound_date": fields.Datetime.now(),
            "needs_reply": True,
        })
        message._post_to_ticket()
        self._notify_dashboard("message")

        text = (message.body or "").strip()
        flow = self.bot_flow_id or self.account_id.bot_flow_id

        # A pending satisfaction question owns the next reply: a bare "5" is a
        # score, not a request to reopen the chat.
        if self.awaiting_rating:
            rating = self.env["whatsapp.rating"]._record_from_reply(self, text)
            if rating:
                self.write({"rating_id": rating.id, "awaiting_rating": False})
                self._notify_dashboard("rated")
                if self.in_session:
                    self.send_text(
                        _("Thank you, that helps us improve."),
                        is_bot=True, force=True,
                    )
                return True
            # Anything else means they want to talk, not to rate.
            self.awaiting_rating = False

        if flow and text:
            lowered = text.lower()
            if lowered in flow._keywords("close_keywords"):
                return self._close(notify=True)
            if lowered in flow._keywords("agent_keywords"):
                return self._handoff(reason=_("Customer asked for an agent."))
            if lowered in flow._keywords("restart_keywords"):
                self.write({"bot_pending_step_id": False, "invalid_count": 0})
                self.state = "bot"
                return self._bot_start(flow, greet=False)

        if self.state == "closed":
            # A new message on a closed chat starts a fresh session.
            self.write({
                "state": "bot" if flow else "agent",
                "answers": "{}",
                "bot_step_id": False,
                "bot_pending_step_id": False,
                "invalid_count": 0,
                "ticket_id": False,
                # The next round gets its own clocks; the previous round's
                # numbers stay on the closed messages already recorded.
                "handoff_date": False,
                "first_agent_reply_date": False,
                "first_response_seconds": 0,
                "closed_date": False,
                "bot_resolved": False,
            })
            if flow:
                return self._bot_start(flow)
            return self._handoff(reason=_("New chat on a closed conversation."))

        if self.state == "agent":
            return self._notify_agent(message)

        if not flow:
            return self._handoff(reason=_("No bot flow configured."))

        if not self.bot_step_id and not self.bot_pending_step_id:
            return self._bot_start(flow)
        return self._bot_advance(flow, text)

    def _notify_agent(self, message):
        """Flag the assigned agent that the customer wrote again.

        The chatter already carries the message; an activity is only raised
        when the setting asks for one, so busy chats do not spam the to-do list.
        """
        self.ensure_one()
        ticket = self.ticket_id
        if not (ticket and ticket.user_id):
            return True
        wants_activity = self.env["ir.config_parameter"].sudo().get_param(
            "ibq_whatsapp.activity_on_reply"
        ) == "True"
        if not wants_activity:
            return True
        pending = self.env["mail.activity"].sudo().search_count([
            ("res_model", "=", "helpdesk.ticket"),
            ("res_id", "=", ticket.id),
            ("user_id", "=", ticket.user_id.id),
        ])
        if not pending:
            ticket.sudo().activity_schedule(
                "mail.mail_activity_data_todo",
                summary=_("New WhatsApp message from %s") % self.name,
                note=(message.body or "")[:200],
                user_id=ticket.user_id.id,
            )
        return True

    # ------------------------------------------------------------------
    # Bot engine
    # ------------------------------------------------------------------
    def _bot_start(self, flow, greet=True):
        self.ensure_one()
        self.bot_flow_id = flow
        if greet and flow.greeting:
            self.send_text(self._format_text(flow.greeting), is_bot=True, force=True)
        if not flow.start_step_id:
            return self._handoff(reason=_("Bot flow has no first step."))
        return self._bot_run(flow, flow.start_step_id)

    def _bot_advance(self, flow, text):
        """Consume the customer's reply for the step we were waiting on."""
        self.ensure_one()
        step = self.bot_pending_step_id
        if not step:
            return self._bot_run(flow, self.bot_step_id.next_step_id) \
                if self.bot_step_id.next_step_id else self._handoff()

        if step.step_type == "menu":
            option = step._match_option(text)
            if not option:
                return self._bot_invalid(flow, step)
            self.invalid_count = 0
            if option.answer_key:
                self._set_answer(option.answer_key, option.name)
            self.bot_pending_step_id = False
            target = option.next_step_id or step.next_step_id
            return self._bot_run(flow, target)

        if step.step_type == "question":
            if not step._validate_answer(text):
                self.invalid_count += 1
                if self.invalid_count >= max(flow.max_invalid_attempts, 1):
                    return self._handoff(reason=_("Too many invalid answers."))
                self.send_text(
                    self._format_text(step.answer_error or flow.fallback_message),
                    is_bot=True, force=True,
                )
                return True
            self.invalid_count = 0
            self._set_answer(step.answer_key or "answer", text)
            self.bot_pending_step_id = False
            return self._bot_run(flow, step.next_step_id)

        self.bot_pending_step_id = False
        return self._bot_run(flow, step.next_step_id)

    def _bot_invalid(self, flow, step):
        self.ensure_one()
        self.invalid_count += 1
        if self.invalid_count >= max(flow.max_invalid_attempts, 1):
            return self._handoff(reason=_("Customer could not pick a valid option."))
        self.send_text(
            self._format_text(flow.fallback_message), is_bot=True, force=True
        )
        self.send_text(step._render_body(self), is_bot=True, force=True)
        return True

    def _bot_run(self, flow, step):
        """Play steps until one needs an answer or the flow ends.

        The hop counter stops a misconfigured flow (a step pointing back at
        itself) from looping forever inside a single webhook call.
        """
        self.ensure_one()
        hops = 0
        while step and hops < MAX_BOT_HOPS:
            hops += 1
            self.bot_step_id = step
            handler = getattr(self, "_bot_step_%s" % step.step_type, None)
            if handler is None:
                _logger.warning("Unknown bot step type %s", step.step_type)
                return self._handoff()
            step = handler(flow, step)
        if hops >= MAX_BOT_HOPS:
            _logger.warning(
                "Bot flow %s exceeded %s hops on conversation %s",
                flow.id, MAX_BOT_HOPS, self.id,
            )
            return self._handoff(reason=_("The scripted flow looped."))
        return True

    def _bot_step_message(self, flow, step):
        self.send_text(step._render_body(self), is_bot=True, force=True)
        return step.next_step_id

    def _bot_step_menu(self, flow, step):
        self.send_text(step._render_body(self), is_bot=True, force=True)
        self.bot_pending_step_id = step
        return None

    def _bot_step_question(self, flow, step):
        self.send_text(step._render_body(self), is_bot=True, force=True)
        self.bot_pending_step_id = step
        return None

    def _bot_step_ticket(self, flow, step):
        self._ensure_ticket(step=step)
        if step.body:
            self.send_text(step._render_body(self), is_bot=True, force=True)
        return step.next_step_id

    def _bot_step_agent(self, flow, step):
        if step.body:
            self.send_text(step._render_body(self), is_bot=True, force=True)
        self._handoff(reason=_("Bot step '%s'.") % step.name, announce=False)
        return None

    def _bot_step_end(self, flow, step):
        if step.body:
            self.send_text(step._render_body(self), is_bot=True, force=True)
        self._close(notify=False)
        return None

    # ------------------------------------------------------------------
    # Ticket lifecycle
    # ------------------------------------------------------------------
    def _ticket_values(self, step=None):
        self.ensure_one()
        answers = self._get_answers()
        subject_key = (step.subject_key if step else "subject") or "subject"
        subject = answers.get(subject_key) or answers.get("subject")
        if not subject:
            last_inbound = self.whatsapp_message_ids.filtered(
                lambda m: m.direction == "inbound"
            )[:1]
            subject = (last_inbound.body or _("WhatsApp request"))[:80]
        description_lines = [
            _("Opened from WhatsApp %s") % self.number,
        ]
        for key, value in answers.items():
            description_lines.append("%s: %s" % (key.replace("_", " ").title(), value))
        values = {
            "name": subject,
            "team_id": (step.team_id.id if step and step.team_id else False)
                       or self.team_id.id or self.account_id.team_id.id,
            "partner_id": self.partner_id.id or False,
            "description": "<br/>".join(description_lines),
        }
        if self.partner_id.email:
            values["partner_email"] = self.partner_id.email
        values["partner_phone"] = self.number
        if step:
            if step.ticket_priority:
                values["priority"] = step.ticket_priority
            if step.tag_ids:
                values["tag_ids"] = [(6, 0, step.tag_ids.ids)]
        return values

    def _ensure_ticket(self, step=None):
        """Return the linked ticket, creating it on first need."""
        self.ensure_one()
        if self.ticket_id and self.ticket_id.exists():
            return self.ticket_id
        ticket = self.env["helpdesk.ticket"].sudo().create(self._ticket_values(step))
        self.ticket_id = ticket
        self.team_id = ticket.team_id
        ticket.sudo().write({
            "whatsapp_conversation_id": self.id,
            "whatsapp_number": self.number,
        })
        # Replay whatever the customer and the bot already said, so the ticket
        # opens with the conversation that produced it rather than a blank
        # chatter.
        self.whatsapp_message_ids.sorted("id")._post_to_ticket()
        # The subject is settled by now, so this is the moment to work out
        # which issue the chat belongs to.
        self.env["whatsapp.issue"]._match_or_create(self)
        return ticket

    def action_create_ticket(self):
        self.ensure_one()
        ticket = self._ensure_ticket()
        return {
            "type": "ir.actions.act_window",
            "res_model": "helpdesk.ticket",
            "res_id": ticket.id,
            "view_mode": "form",
        }

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------
    def _handoff(self, reason=None, announce=True):
        """Stop the bot and put the chat in front of a human."""
        self.ensure_one()
        values = {
            "state": "agent",
            "bot_pending_step_id": False,
            "invalid_count": 0,
            "needs_reply": True,
        }
        if not self.handoff_date:
            values["handoff_date"] = fields.Datetime.now()
        self.write(values)
        if not self.user_id:
            agent = self.env["whatsapp.agent"]._route(self)
            if agent:
                # No announcement here: the hand-off message below already
                # tells the customer a person is picking this up.
                self.with_context(ibq_whatsapp_no_notify=True).user_id = agent.user_id
        if self.account_id.auto_create_ticket:
            self._ensure_ticket()
        if self.user_id and self.ticket_id and not self.ticket_id.user_id:
            self.ticket_id.sudo().user_id = self.user_id
        if self.ticket_id and reason:
            self.ticket_id.sudo().message_post(
                body=_("Handed over from the WhatsApp bot: %s") % reason,
                subtype_xmlid="mail.mt_note",
            )
        if announce:
            template = self.env["whatsapp.template"]._get_by_code(
                "agent_handoff", self.account_id
            )
            if template:
                self.send_template(template)
            elif self.in_session:
                self.send_text(
                    _("Thanks! I am connecting you with one of our agents."),
                    is_bot=True, force=True,
                )
        self._notify_dashboard("handoff")
        return True

    def action_assign_to_me(self):
        for record in self:
            record.assign_to(self.env.user, source=_("the conversation form"))
        return True

    def action_handoff(self):
        for record in self:
            record._handoff(reason=_("Manually taken over by %s.") % self.env.user.name)
        return True

    def _close(self, notify=True):
        self.ensure_one()
        now = fields.Datetime.now()
        self.write({
            "state": "closed",
            "bot_pending_step_id": False,
            "needs_reply": False,
            "closed_date": now,
            "resolution_seconds": int((now - self.create_date).total_seconds())
                                  if self.create_date else 0,
            "bot_resolved": not self.handoff_date,
        })
        if notify and self.in_session:
            self.send_text(
                _("This conversation is now closed. Message us any time to reopen it."),
                is_bot=True, force=True,
            )
        self._ask_for_rating()
        if not self.issue_id:
            self.env["whatsapp.issue"]._match_or_create(self)
        self._notify_dashboard("closed")
        return True

    def _ask_for_rating(self):
        """Send the satisfaction question, if this chat earned one.

        Only chats a person actually handled are worth asking about, and only
        once — a customer who reopens a chat three times should not be polled
        three times.
        """
        self.ensure_one()
        if not self.account_id.ask_rating or not self.handoff_date:
            return False
        if self.rating_id or self.awaiting_rating or not self.in_session:
            return False
        self.send_text(
            _("Before you go: how did we do? Reply with a number from 1 (poor) "
              "to 5 (great)."),
            is_bot=True, force=True,
        )
        self.awaiting_rating = True
        return True

    def action_close(self):
        for record in self:
            record._close(notify=True)
        return True

    def action_reopen(self):
        for record in self:
            record.write({"state": "agent", "needs_reply": True})
        return True

    def action_open_composer(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "whatsapp.compose.message",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_conversation_id": self.id,
                "default_number": self.number,
                "default_account_id": self.account_id.id,
            },
        }

    # ------------------------------------------------------------------
    # Live dashboard
    # ------------------------------------------------------------------
    def _notify_dashboard(self, event):
        """Push a nudge to every agent watching the dashboard.

        Sent on each agent's own partner channel rather than a shared custom
        one, so no extra websocket channel permissions are involved. The
        payload is deliberately thin: the client refetches what it needs.
        """
        self.ensure_one()
        agents = self.env["whatsapp.agent"].sudo().search([])
        partners = agents.user_id.partner_id
        if not partners:
            return
        payload = {
            "event": event,
            "conversation_id": self.id,
            "state": self.state,
            "needs_reply": self.needs_reply,
            "user_id": self.user_id.id or False,
        }
        notifications = [
            (partner, "ibq_whatsapp/dashboard", payload) for partner in partners
        ]
        self.env["bus.bus"].sudo()._sendmany(notifications)

    def _note_payload(self, limit=20):
        """Internal notes agents left on this chat, newest last.

        Notes live on the conversation's own chatter rather than in a bespoke
        model, so they follow Odoo's normal audit trail and never risk being
        sent to the customer by accident.
        """
        self.ensure_one()
        note_subtype = self.env.ref("mail.mt_note", raise_if_not_found=False)
        if not note_subtype:
            return []
        notes = self.message_ids.filtered(
            lambda m: m.subtype_id == note_subtype and m.body
        ).sorted("id")[-limit:]
        return [
            {
                "id": note.id,
                "author": note.author_id.display_name or _("System"),
                "body": html2plaintext(note.body),
                "date": fields.Datetime.to_string(note.date),
            }
            for note in notes
        ]

    def add_note(self, body):
        """Record an internal note. Never leaves Odoo."""
        self.ensure_one()
        if not (body or "").strip():
            raise UserError(_("The note is empty."))
        self.message_post(body=body, subtype_xmlid="mail.mt_note")
        return self._note_payload()

    def transfer_to(self, user=None, team=None, note=None):
        """Hand a chat to another agent or department, leaving a trail."""
        self.ensure_one()
        if team is not None:
            self.team_id = team.id if team else False
        if user is not None:
            self.assign_to(user, source=_("a transfer"))
        target = user.name if user else (team.name if team else _("the queue"))
        self.message_post(
            body=_("Transferred to %(target)s by %(who)s.%(note)s",
                   target=target, who=self.env.user.name,
                   note=(" %s" % note) if note else ""),
            subtype_xmlid="mail.mt_note",
        )
        self._notify_dashboard("transferred")
        return True

    def _chat_payload(self, message_limit=50):
        """Everything the dashboard chat console needs for one thread."""
        self.ensure_one()
        messages = self.whatsapp_message_ids.sorted("id")[-message_limit:]
        return {
            "id": self.id,
            "name": self.name,
            "number": self.number,
            "profile_name": self.profile_name or "",
            "partner": self.partner_id.display_name or "",
            "partner_id": self.partner_id.id or False,
            "state": self.state,
            "needs_reply": self.needs_reply,
            "in_session": self.in_session,
            "session_expiry": self.session_expiry and
                              fields.Datetime.to_string(self.session_expiry) or False,
            "ticket_id": self.ticket_id.id or False,
            "ticket_name": self.ticket_id.display_name or "",
            "user_id": self.user_id.id or False,
            "user_name": self.user_id.name or "",
            "team_id": self.team_id.id or False,
            "team_name": self.team_id.name or "",
            "priority": self.priority,
            "tags": [tag._payload() for tag in self.tag_ids],
            "rating": self.rating_id and {
                "score": self.rating_id.score_value,
                "sentiment": self.rating_id.sentiment,
                "comment": self.rating_id.comment or "",
            } or False,
            "awaiting_rating": self.awaiting_rating,
            "notes": self._note_payload(),
            "avatar": self.partner_id.id and
                      "/web/image/res.partner/%s/avatar_128" % self.partner_id.id or False,
            "answers": self._get_answers(),
            "messages": [
                {
                    "id": m.id,
                    "direction": m.direction,
                    "body": m.body or "",
                    "state": m.state,
                    "is_bot": m.is_bot,
                    "author": m.author_id.name or "",
                    "error": m.error_message or "",
                    "attachments": [
                        {"id": a.id, "name": a.name, "mimetype": a.mimetype or ""}
                        for a in m.attachment_ids
                    ],
                    "date": fields.Datetime.to_string(m.create_date),
                }
                for m in messages
            ],
        }

    def _queue_payload(self):
        """Compact shape for the dashboard's conversation list."""
        self.ensure_one()
        last = self.whatsapp_message_ids.sorted("id")[-1:]
        return {
            "id": self.id,
            "name": self.name,
            "number": self.number,
            "partner": self.partner_id.display_name or self.profile_name or self.number,
            "avatar": self.partner_id.id and
                      "/web/image/res.partner/%s/avatar_128" % self.partner_id.id or False,
            "state": self.state,
            "needs_reply": self.needs_reply,
            "in_session": self.in_session,
            "user_id": self.user_id.id or False,
            "user_name": self.user_id.name or "",
            "ticket_id": self.ticket_id.id or False,
            "team_id": self.team_id.id or False,
            "team_name": self.team_id.name or "",
            "priority": self.priority,
            "tags": [tag._payload() for tag in self.tag_ids],
            "rating_score": self.rating_score or 0,
            "preview": (last.body or "")[:90],
            "last_direction": last.direction or "",
            "last_message_date": self.last_message_date and
                                 fields.Datetime.to_string(self.last_message_date) or False,
        }

    # ------------------------------------------------------------------
    # Cron
    # ------------------------------------------------------------------
    @api.model
    def _cron_close_idle(self, idle_hours=48):
        """Close chats nobody has touched for a while, so the inbox stays honest."""
        deadline = fields.Datetime.now() - timedelta(hours=idle_hours or 0)
        stale = self.search([
            ("state", "!=", "closed"),
            ("last_message_date", "<", deadline),
        ])
        for conversation in stale:
            conversation._close(notify=False)
        return len(stale)
