# -*- coding: utf-8 -*-
import base64
import hashlib
import hmac
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

try:
    import requests
except ImportError:  # pragma: no cover - declared in external_dependencies
    requests = None

TWILIO_API_ROOT = "https://api.twilio.com/2010-04-01"
REQUEST_TIMEOUT = 20
E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


def normalize_number(number):
    """Return a bare E.164 number out of anything Twilio or a human may send.

    ``whatsapp:+9715012345``, ``+971 50 123 45`` and ``009715012345`` all
    collapse to ``+9715012345``.
    """
    if not number:
        return False
    value = str(number).strip()
    if value.lower().startswith("whatsapp:"):
        value = value[len("whatsapp:"):]
    value = re.sub(r"[^\d+]", "", value)
    if value.startswith("00"):
        value = "+" + value[2:]
    if value and not value.startswith("+"):
        value = "+" + value
    return value or False


class WhatsappAccount(models.Model):
    _name = "whatsapp.account"
    _description = "WhatsApp Account (Twilio)"
    _order = "sequence, id"

    name = fields.Char(required=True, help="Internal label, e.g. 'IBQ Support Line'.")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company
    )

    # -- Twilio credentials ------------------------------------------------
    account_sid = fields.Char(
        "Account SID", required=True,
        help="Starts with 'AC'. Twilio Console > Account Info.",
    )
    auth_token = fields.Char(
        "Auth Token", required=True,
        help="Twilio Auth Token. Also used to verify the X-Twilio-Signature "
             "header on inbound webhooks.",
    )
    sender_mode = fields.Selection(
        [("number", "WhatsApp Sender Number"), ("service", "Messaging Service")],
        default="number", required=True,
    )
    phone_number = fields.Char(
        "WhatsApp Sender",
        help="The WhatsApp-enabled number in E.164 format, e.g. +14155238886 "
             "(the Twilio sandbox number while testing).",
    )
    messaging_service_sid = fields.Char(
        "Messaging Service SID",
        help="Starts with 'MG'. Used instead of a fixed sender number.",
    )

    # -- Behaviour ---------------------------------------------------------
    verify_signature = fields.Boolean(
        "Verify Webhook Signature", default=True,
        help="Reject inbound webhooks whose X-Twilio-Signature does not match. "
             "Only disable while debugging behind a proxy that rewrites the URL.",
    )
    webhook_base_url = fields.Char(
        "Public Base URL",
        help="Leave empty to use the system parameter web.base.url. Set it when Odoo "
             "sits behind a tunnel (ngrok) whose external URL differs.",
    )
    inbound_webhook_url = fields.Char(compute="_compute_webhook_urls")
    status_webhook_url = fields.Char(compute="_compute_webhook_urls")

    team_id = fields.Many2one(
        "helpdesk.team", string="Default Helpdesk Team",
        help="Tickets created from this number land in this team.",
    )
    bot_flow_id = fields.Many2one(
        "whatsapp.bot.flow", string="Bot Flow",
        help="Scripted conversation played to inbound chats. Leave empty to hand "
             "every chat straight to an agent.",
    )
    auto_create_ticket = fields.Boolean(
        "Auto-create Ticket", default=True,
        help="Create a helpdesk ticket when a conversation reaches an agent, even if "
             "the bot flow did not create one.",
    )
    session_hours = fields.Integer(
        "Session Window (hours)", default=24,
        help="WhatsApp only allows free-form messages within this window after the "
             "customer's last message. Outside it, only approved templates go out.",
    )
    unknown_contact_action = fields.Selection(
        [("create", "Create a contact"), ("none", "Keep the number only")],
        default="create", string="Unknown Numbers", required=True,
    )
    allow_agent_commands = fields.Boolean(
        "Agent Commands", default=True,
        help="Let agents whose own WhatsApp number is on the roster run the "
             "queue by texting commands like '#assign 1042 sue' to this "
             "number. Commands never create a conversation and never reach a "
             "customer.",
    )
    ask_rating = fields.Boolean(
        "Ask for a Rating", default=True,
        help="When a chat an agent handled is closed, ask the customer to "
             "score it from 1 to 5.",
    )

    state = fields.Selection(
        [("draft", "Draft"), ("connected", "Connected"), ("error", "Error")],
        default="draft", readonly=True, copy=False,
    )
    last_error = fields.Char(readonly=True, copy=False)
    conversation_count = fields.Integer(compute="_compute_conversation_count")

    _sql_constraints = [
        ("account_sender_uniq", "unique(account_sid, phone_number)",
         "This Twilio account and sender pair is already configured."),
    ]

    # ------------------------------------------------------------------
    # Compute / constraints
    # ------------------------------------------------------------------
    def _compute_conversation_count(self):
        groups = self.env["whatsapp.conversation"]._read_group(
            [("account_id", "in", self.ids)], ["account_id"], ["__count"]
        )
        mapped = {account.id: count for account, count in groups}
        for record in self:
            record.conversation_count = mapped.get(record.id, 0)

    @api.depends("webhook_base_url")
    def _compute_webhook_urls(self):
        param = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        for record in self:
            base = (record.webhook_base_url or param or "").rstrip("/")
            record.inbound_webhook_url = base + "/whatsapp/twilio/inbound"
            record.status_webhook_url = base + "/whatsapp/twilio/status"

    @api.constrains("sender_mode", "phone_number", "messaging_service_sid")
    def _check_sender(self):
        for record in self:
            if record.sender_mode == "number":
                number = normalize_number(record.phone_number)
                if not number or not E164_RE.match(number):
                    raise ValidationError(_(
                        "The WhatsApp sender must be a valid E.164 number, "
                        "e.g. +14155238886."
                    ))
            elif not (record.messaging_service_sid or "").startswith("MG"):
                raise ValidationError(_("A Messaging Service SID starts with 'MG'."))

    @api.constrains("account_sid")
    def _check_account_sid(self):
        for record in self:
            if not (record.account_sid or "").startswith("AC"):
                raise ValidationError(_("A Twilio Account SID starts with 'AC'."))

    @api.onchange("phone_number")
    def _onchange_phone_number(self):
        if self.phone_number:
            self.phone_number = normalize_number(self.phone_number)

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------
    @api.model
    def _get_default_account(self):
        """The account used when nothing more specific is known."""
        param = self.env["ir.config_parameter"].sudo().get_param(
            "ibq_whatsapp.default_account_id"
        )
        if param:
            account = self.browse(int(param)).exists()
            if account:
                return account
        return self.search([("company_id", "in", self.env.companies.ids)], limit=1)

    @api.model
    def _find_for_inbound(self, to_number, account_sid=None):
        """Resolve the account addressed by an inbound Twilio webhook."""
        number = normalize_number(to_number)
        base_domain = [("account_sid", "=", account_sid)] if account_sid else []
        account = self.sudo().search(
            base_domain + [("phone_number", "=", number)], limit=1
        )
        if not account and account_sid:
            account = self.sudo().search([("account_sid", "=", account_sid)], limit=1)
        return account

    # ------------------------------------------------------------------
    # Signature validation
    # ------------------------------------------------------------------
    def _compute_signature(self, url, params):
        """Reproduce Twilio's request signature.

        Twilio concatenates the full URL with every POST parameter sorted by
        name, then signs the result with HMAC-SHA1 keyed on the auth token.
        """
        self.ensure_one()
        payload = url
        for key in sorted(params or {}):
            value = params[key]
            payload += key + ("" if value is None else str(value))
        digest = hmac.new(
            (self.sudo().auth_token or "").encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha1,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    def validate_signature(self, url, params, signature):
        self.ensure_one()
        if not self.verify_signature:
            return True
        if not signature:
            return False
        return hmac.compare_digest(self._compute_signature(url, params), signature)

    # ------------------------------------------------------------------
    # Twilio REST transport
    # ------------------------------------------------------------------
    def _twilio_call(self, method, path, data=None):
        """Low level Twilio call, returning the decoded JSON body.

        Transport failures raise UserError. HTTP errors come back as the
        parsed Twilio payload so callers can persist code and message.
        """
        self.ensure_one()
        if requests is None:
            raise UserError(_("The Python library 'requests' is required to reach Twilio."))
        account = self.sudo()
        url = "%s/Accounts/%s/%s" % (TWILIO_API_ROOT, account.account_sid, path.lstrip("/"))
        try:
            response = requests.request(
                method, url,
                data=data or {},
                auth=(account.account_sid, account.auth_token),
                timeout=REQUEST_TIMEOUT,
            )
        except Exception as exc:  # network layer, surfaced to the caller
            _logger.warning("Twilio %s %s failed: %s", method, url, exc)
            raise UserError(_("Could not reach Twilio: %s") % exc) from exc
        try:
            body = response.json()
        except ValueError:
            body = {"message": response.text, "status": response.status_code}
        body["_http_status"] = response.status_code
        body["_ok"] = response.ok
        return body

    def action_test_connection(self):
        for record in self:
            body = record._twilio_call("GET", ".json")
            if body.get("_ok"):
                record.write({"state": "connected", "last_error": False})
            else:
                message = body.get("message") or _("HTTP %s") % body.get("_http_status")
                record.write({"state": "error", "last_error": message})
                raise UserError(_("Twilio rejected the credentials: %s") % message)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": _("Connected"),
                "message": _("Twilio credentials are valid."),
                "sticky": False,
            },
        }

    def _sender_payload(self):
        self.ensure_one()
        if self.sender_mode == "service":
            return {"MessagingServiceSid": self.messaging_service_sid}
        return {"From": "whatsapp:%s" % normalize_number(self.phone_number)}

    def _send_raw(self, to_number, body=None, media_urls=None, content_sid=None,
                  content_variables=None, status_callback=None):
        """Push one message to Twilio and return the raw response dict."""
        self.ensure_one()
        payload = {"To": "whatsapp:%s" % normalize_number(to_number)}
        payload.update(self._sender_payload())
        if content_sid:
            payload["ContentSid"] = content_sid
            if content_variables:
                payload["ContentVariables"] = content_variables
        if body:
            payload["Body"] = body
        if media_urls:
            # requests encodes a list as repeated keys, which is what Twilio expects.
            payload["MediaUrl"] = media_urls
        if status_callback:
            payload["StatusCallback"] = status_callback
        return self._twilio_call("POST", "Messages.json", data=payload)

    # ------------------------------------------------------------------
    # Inbound intake
    # ------------------------------------------------------------------
    def _process_inbound_payload(self, params):
        """Store one inbound Twilio payload and let the conversation react.

        Lives here rather than in the controller so the whole intake path can
        be exercised without an HTTP request.
        """
        self.ensure_one()

        # Quick-reply buttons and list pickers carry their answer in their own
        # field; fall back to the plain text body otherwise.
        body = (params.get("ButtonPayload") or params.get("ListId")
                or params.get("Body") or "")

        # An agent texting a command is handled before anything else, so it
        # never opens a conversation, never reaches the queue, and never
        # reaches a customer.
        if self.allow_agent_commands:
            handled = self._handle_agent_command(params, body)
            if handled is not None:
                return handled

        blocked = self.env["whatsapp.blocklist"]._entry_for(params.get("From"))
        if blocked:
            # Drop it without a word: any reply confirms the number is live.
            blocked._register_hit()
            _logger.info("Dropped inbound WhatsApp from blocked number %s", blocked.number)
            return self.env["whatsapp.message"]

        conversation = self.env["whatsapp.conversation"].sudo()._get_or_create(
            self, params.get("From"), params.get("ProfileName")
        )

        try:
            media_count = int(params.get("NumMedia") or 0)
        except (TypeError, ValueError):
            media_count = 0
        media_urls = [
            params["MediaUrl%s" % index]
            for index in range(media_count)
            if params.get("MediaUrl%s" % index)
        ]

        message = self.env["whatsapp.message"].sudo().create({
            "conversation_id": conversation.id,
            "account_id": self.id,
            "direction": "inbound",
            "number": params.get("From"),
            "partner_id": conversation.partner_id.id,
            "body": body,
            "message_type": "media" if media_urls else "text",
            "media_url": "\n".join(media_urls),
            "twilio_sid": params.get("MessageSid") or params.get("SmsMessageSid"),
            "state": "received",
            "author_id": False,
        })
        if media_urls:
            message._download_media(media_urls)
        conversation._handle_inbound(message)
        return message

    # ------------------------------------------------------------------
    # Agent commands
    # ------------------------------------------------------------------
    def _handle_agent_command(self, params, body):
        """Run a texted command, or return None if this is not one.

        Returns the recorded inbound message when it handled the text, so the
        caller knows to stop. Commands are logged like any other message but
        carry no conversation, which keeps them out of the queue.
        """
        self.ensure_one()
        sender = params.get("From")
        agent = self.env["whatsapp.agent"]._find_by_phone(sender)
        if not agent:
            return None
        if not self.env["whatsapp.command"].looks_like_command(body):
            # An agent may also be a customer. Anything that is not a command
            # falls through to the normal conversation path.
            return None

        inbound = self.env["whatsapp.message"].sudo().create({
            "account_id": self.id,
            "direction": "inbound",
            "number": sender,
            "partner_id": agent.user_id.partner_id.id,
            "body": body,
            "twilio_sid": params.get("MessageSid") or params.get("SmsMessageSid"),
            "state": "received",
            "is_command": True,
            "author_id": False,
        })

        # Run as the agent, so their role decides what the command may do.
        reply = self.env["whatsapp.command"].with_user(agent.user_id).execute(body)
        _logger.info("WhatsApp command from %s: %s", agent.user_id.name, body)

        self.env["whatsapp.message"].sudo().create({
            "account_id": self.id,
            "direction": "outbound",
            "number": sender,
            "partner_id": agent.user_id.partner_id.id,
            "body": reply,
            "is_bot": True,
            "is_command": True,
            "author_id": False,
        }).action_send()
        return inbound

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_view_conversations(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id(
            "ibq_whatsapp_helpdesk.action_whatsapp_conversation"
        )
        action["domain"] = [("account_id", "=", self.id)]
        action["context"] = {"default_account_id": self.id}
        return action
