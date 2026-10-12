# -*- coding: utf-8 -*-
import base64
import hashlib
import hmac
import json
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from . import meta_payload

_logger = logging.getLogger(__name__)

try:
    import requests
except ImportError:  # pragma: no cover - declared in external_dependencies
    requests = None

TWILIO_API_ROOT = "https://api.twilio.com/2010-04-01"
META_API_ROOT = "https://graph.facebook.com"
META_DEFAULT_VERSION = "v21.0"
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
    _name = "ibq.whatsapp.account"
    _description = "WhatsApp Account"
    _order = "sequence, id"

    name = fields.Char(required=True, help="Internal label, e.g. 'IBQ Support Line'.")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company
    )

    provider = fields.Selection(
        [("meta", "Meta WhatsApp Cloud API"), ("twilio", "Twilio")],
        default="meta", required=True,
        help="Who carries the messages. Meta is WhatsApp's own API and bills "
             "you directly; Twilio resells it with a per-message fee on top.",
    )

    # -- Meta Cloud API credentials ---------------------------------------
    meta_phone_number_id = fields.Char(
        "Phone Number ID",
        help="Meta app dashboard > WhatsApp > API Setup. A numeric id, not the "
             "phone number itself. It identifies the account on inbound webhooks.",
    )
    meta_waba_id = fields.Char(
        "WhatsApp Business Account ID",
        help="The WABA id shown next to the phone number id. Used to list "
             "your approved message templates.",
    )
    meta_access_token = fields.Char(
        "Access Token",
        help="A System User token with whatsapp_business_messaging. Generate a "
             "permanent one in Business Settings; the 24h test token expires.",
    )
    meta_app_secret = fields.Char(
        "App Secret",
        help="Meta app dashboard > App Settings > Basic. Used to verify the "
             "X-Hub-Signature-256 header on inbound webhooks.",
    )
    meta_verify_token = fields.Char(
        "Webhook Verify Token",
        help="Any string you choose. Paste the same value into Meta's webhook "
             "setup; it is echoed back once during subscription.",
    )
    meta_api_version = fields.Char(
        "Graph API Version", default=META_DEFAULT_VERSION,
        help="The Graph API version to call, e.g. v21.0.",
    )

    # -- Twilio credentials ------------------------------------------------
    account_sid = fields.Char(
        "Account SID",
        help="Starts with 'AC'. Twilio Console > Account Info.",
    )
    auth_token = fields.Char(
        "Auth Token",
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
        "ibq.whatsapp.bot.flow", string="Bot Flow",
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
        groups = self.env["ibq.whatsapp.conversation"]._read_group(
            [("account_id", "in", self.ids)], ["account_id"], ["__count"]
        )
        mapped = {account.id: count for account, count in groups}
        for record in self:
            record.conversation_count = mapped.get(record.id, 0)

    @api.depends("webhook_base_url", "provider")
    def _compute_webhook_urls(self):
        param = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        for record in self:
            base = (record.webhook_base_url or param or "").rstrip("/")
            if record.provider == "meta":
                # Meta posts inbound messages and delivery receipts to one
                # callback URL, so both fields show the same endpoint.
                record.inbound_webhook_url = base + "/whatsapp/meta/webhook"
                record.status_webhook_url = base + "/whatsapp/meta/webhook"
            else:
                record.inbound_webhook_url = base + "/whatsapp/twilio/inbound"
                record.status_webhook_url = base + "/whatsapp/twilio/status"

    def init(self):
        """Keep accounts that predate the provider field on Twilio.

        New records default to Meta, but a column default would also be
        applied to every existing row on upgrade, silently repointing working
        Twilio senders at an API they have no credentials for. A row carrying
        a Twilio SID and no Meta phone number id can only be Twilio.
        """
        super().init()
        self.env.cr.execute(
            """
            UPDATE ibq_whatsapp_account
               SET provider = 'twilio'
             WHERE provider = 'meta'
               AND COALESCE(account_sid, '') <> ''
               AND COALESCE(meta_phone_number_id, '') = ''
            """
        )

    @api.constrains("provider", "sender_mode", "phone_number", "messaging_service_sid")
    def _check_sender(self):
        for record in self:
            if record.provider == "meta" or record.sender_mode == "number":
                number = normalize_number(record.phone_number)
                if not number or not E164_RE.match(number):
                    raise ValidationError(_(
                        "The WhatsApp sender must be a valid E.164 number, "
                        "e.g. +14155238886."
                    ))
            elif not (record.messaging_service_sid or "").startswith("MG"):
                raise ValidationError(_("A Messaging Service SID starts with 'MG'."))

    @api.constrains("provider", "account_sid", "auth_token")
    def _check_account_sid(self):
        for record in self:
            if record.provider != "twilio":
                continue
            if not (record.account_sid or "").startswith("AC"):
                raise ValidationError(_("A Twilio Account SID starts with 'AC'."))
            if not record.auth_token:
                raise ValidationError(_("A Twilio account needs an Auth Token."))

    @api.constrains("provider", "meta_phone_number_id", "meta_access_token")
    def _check_meta_credentials(self):
        for record in self:
            if record.provider != "meta":
                continue
            if not (record.meta_phone_number_id or "").strip().isdigit():
                raise ValidationError(_(
                    "The Meta Phone Number ID is the numeric id from the app "
                    "dashboard, not the phone number itself."
                ))
            if not record.meta_access_token:
                raise ValidationError(_("A Meta account needs an Access Token."))

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
        """Resolve the account addressed by an inbound webhook.

        ``account_sid`` is Twilio's AccountSid or Meta's phone number id --
        both are flattened into the same key by the time intake sees them.
        The id is the reliable handle, so it is tried on its own when the
        number does not match; a sender saved in the wrong format should not
        silently drop every inbound message.
        """
        number = normalize_number(to_number)
        identifiers = (
            ["|", ("account_sid", "=", account_sid),
             ("meta_phone_number_id", "=", account_sid)]
            if account_sid else []
        )
        account = self.sudo().search(
            identifiers + [("phone_number", "=", number)], limit=1
        )
        if not account and account_sid:
            account = self.sudo().search(identifiers, limit=1)
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

    def _compute_meta_signature(self, raw_body):
        """Reproduce Meta's X-Hub-Signature-256.

        Meta signs the raw request body -- not the URL, and not the parsed
        parameters -- with HMAC-SHA256 keyed on the app secret. The body must
        be the exact bytes received: re-serialising the parsed JSON changes
        whitespace and key order and the digest no longer matches.
        """
        self.ensure_one()
        digest = hmac.new(
            (self.sudo().meta_app_secret or "").encode("utf-8"),
            raw_body or b"",
            hashlib.sha256,
        ).hexdigest()
        return "sha256=" + digest

    def validate_meta_signature(self, raw_body, signature):
        self.ensure_one()
        if not self.verify_signature:
            return True
        if not signature or not self.sudo().meta_app_secret:
            return False
        return hmac.compare_digest(
            self._compute_meta_signature(raw_body), signature.strip()
        )

    def check_meta_verify_token(self, token):
        """Meta's one-off subscription handshake."""
        self.ensure_one()
        expected = (self.sudo().meta_verify_token or "").strip()
        return bool(expected) and hmac.compare_digest(expected, (token or "").strip())

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

    # ------------------------------------------------------------------
    # Meta Cloud API transport
    # ------------------------------------------------------------------
    def _meta_url(self, path):
        self.ensure_one()
        version = (self.meta_api_version or META_DEFAULT_VERSION).strip("/")
        return "%s/%s/%s" % (META_API_ROOT, version, str(path).lstrip("/"))

    def _meta_call(self, method, path, json_body=None, params=None):
        """Low level Graph call, returning a dict shaped like the Twilio one.

        Normalised to the same ``_ok`` / ``code`` / ``message`` keys the Twilio
        path returns, so callers and the failure-recording code stay provider
        agnostic.
        """
        self.ensure_one()
        if requests is None:
            raise UserError(_("The Python library 'requests' is required to reach Meta."))
        account = self.sudo()
        url = account._meta_url(path)
        try:
            response = requests.request(
                method, url,
                json=json_body,
                params=params,
                headers={"Authorization": "Bearer %s" % (account.meta_access_token or "")},
                timeout=REQUEST_TIMEOUT,
            )
        except Exception as exc:  # network layer, surfaced to the caller
            _logger.warning("Meta %s %s failed: %s", method, url, exc)
            raise UserError(_("Could not reach the Meta API: %s") % exc) from exc
        try:
            body = response.json()
        except ValueError:
            body = {"message": response.text, "status": response.status_code}
        if not isinstance(body, dict):
            body = {"data": body}
        # Graph reports failures in an "error" object rather than a flat code.
        error = body.get("error") or {}
        if error:
            body["code"] = error.get("code")
            body["message"] = error.get("message") or error.get("type")
        # A send returns {"messages": [{"id": "wamid..."}]}; expose it as "sid"
        # so the message model records it exactly as it does a Twilio sid.
        messages = body.get("messages") or []
        if messages and isinstance(messages, list):
            body["sid"] = (messages[0] or {}).get("id")
            # Meta acknowledges acceptance, not delivery; statuses arrive later
            # on the webhook.
            body.setdefault("status", "accepted")
        body["_http_status"] = response.status_code
        body["_ok"] = response.ok and not error
        return body

    def action_test_connection(self):
        """Check the credentials and record the verdict on the account.

        A failure is reported as a notification rather than raised: a
        UserError rolls the transaction back, which would discard the very
        ``state`` and ``last_error`` the form is meant to show afterwards.
        """
        failures = []
        for record in self:
            if record.provider == "meta":
                error = record._test_connection_meta()
            else:
                error = record._test_connection_twilio()
            if error:
                failures.append("%s: %s" % (record.name, error))

        if failures:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "type": "danger",
                    "title": _("Connection failed"),
                    "message": "\n".join(failures),
                    "sticky": True,
                },
            }
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": _("Connected"),
                "message": _("Credentials are valid."),
                "sticky": False,
            },
        }

    def _test_connection_twilio(self):
        """Returns an error string, or None when the credentials are good."""
        self.ensure_one()
        try:
            body = self._twilio_call("GET", ".json")
        except UserError as exc:
            self.write({"state": "error", "last_error": str(exc)})
            return str(exc)
        if body.get("_ok"):
            self.write({"state": "connected", "last_error": False})
            return None
        message = body.get("message") or _("HTTP %s") % body.get("_http_status")
        self.write({"state": "error", "last_error": message})
        return message

    def _test_connection_meta(self):
        """Verify the token can actually read the configured phone number.

        Reads the phone number record rather than the token's own metadata:
        a token that is valid but lacks access to this number would otherwise
        pass the check and fail on the first real send.

        Returns an error string, or None when the credentials are good.
        """
        self.ensure_one()
        try:
            body = self._meta_call(
                "GET", self.meta_phone_number_id,
                params={"fields": "display_phone_number,verified_name,quality_rating"},
            )
        except UserError as exc:
            self.write({"state": "error", "last_error": str(exc)})
            return str(exc)
        if not body.get("_ok"):
            message = body.get("message") or _("HTTP %s") % body.get("_http_status")
            self.write({"state": "error", "last_error": message})
            return message

        # The number Meta holds is authoritative; a mistyped sender here is a
        # common cause of inbound never matching an account.
        reported = normalize_number(body.get("display_phone_number"))
        values = {"state": "connected", "last_error": False}
        if reported and reported != normalize_number(self.phone_number):
            values["phone_number"] = reported
            _logger.info(
                "Corrected WhatsApp sender on %s from %s to %s (Meta is authoritative)",
                self.name, self.phone_number, reported,
            )
        self.write(values)
        return None

    def _sender_payload(self):
        self.ensure_one()
        if self.sender_mode == "service":
            return {"MessagingServiceSid": self.messaging_service_sid}
        return {"From": "whatsapp:%s" % normalize_number(self.phone_number)}

    def _send_raw(self, to_number, body=None, media_urls=None, content_sid=None,
                  content_variables=None, status_callback=None, template=None):
        """Push one message to the provider and return the raw response dict.

        Both branches return the same keys (``_ok``, ``sid``, ``status``,
        ``code``, ``message``) so the caller never has to know which provider
        carried it.
        """
        self.ensure_one()
        if self.provider == "meta":
            return self._send_raw_meta(
                to_number, body=body, media_urls=media_urls,
                content_variables=content_variables, template=template,
            )
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

    def _send_raw_meta(self, to_number, body=None, media_urls=None,
                       content_variables=None, template=None):
        """Send one message through the Meta Cloud API.

        Meta has no status-callback URL: delivery receipts arrive on the same
        webhook as inbound messages, so there is nothing to pass here.
        """
        self.ensure_one()
        template_name = template_language = None
        components = []
        if template:
            template_name = template.meta_template_name or template.code
            template_language = template.lang_code or "en"
            components = meta_payload.build_template_components(
                self._decode_variables(content_variables)
            )
        payload = meta_payload.build_send_payload(
            normalize_number(to_number),
            body=body,
            media_ids=media_urls or None,
            template_name=template_name,
            template_language=template_language,
            template_components=components,
        )
        return self._meta_call(
            "POST", "%s/messages" % self.meta_phone_number_id, json_body=payload
        )

    @api.model
    def _decode_variables(self, content_variables):
        """Templates store variables as the JSON Twilio wants; Meta needs a dict."""
        if not content_variables:
            return {}
        if isinstance(content_variables, dict):
            return content_variables
        try:
            decoded = json.loads(content_variables)
        except (TypeError, ValueError):
            _logger.warning("Could not decode template variables: %r", content_variables)
            return {}
        return decoded if isinstance(decoded, dict) else {}

    def _meta_media_url(self, media_id):
        """Resolve a Meta media id into a time-limited download URL."""
        self.ensure_one()
        body = self._meta_call("GET", media_id)
        if not body.get("_ok"):
            _logger.warning(
                "Could not resolve Meta media %s: %s", media_id, body.get("message")
            )
            return None
        return body.get("url")

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

        blocked = self.env["ibq.whatsapp.blocklist"]._entry_for(params.get("From"))
        if blocked:
            # Drop it without a word: any reply confirms the number is live.
            blocked._register_hit()
            _logger.info("Dropped inbound WhatsApp from blocked number %s", blocked.number)
            return self.env["ibq.whatsapp.message"]

        conversation = self.env["ibq.whatsapp.conversation"].sudo()._get_or_create(
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

        message = self.env["ibq.whatsapp.message"].sudo().create({
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
    # Meta webhook intake
    # ------------------------------------------------------------------
    @api.model
    def _account_for_meta_payload(self, payload):
        """The account a Meta webhook belongs to, by phone number id.

        Resolved before signature checking, because the app secret that
        verifies the signature lives on the account record.
        """
        for value in meta_payload.iter_message_events(payload):
            phone_number_id = (value.get("metadata") or {}).get("phone_number_id")
            if phone_number_id:
                account = self.sudo().search(
                    [("provider", "=", "meta"),
                     ("meta_phone_number_id", "=", phone_number_id)], limit=1
                )
                if account:
                    return account
        return self.sudo().search([("provider", "=", "meta")], limit=1)

    def _process_meta_webhook(self, payload):
        """Handle one decoded Meta webhook body.

        Returns the number of inbound messages taken in. Statuses and messages
        can arrive in the same request, so both are drained here.
        """
        self.ensure_one()
        Message = self.env["ibq.whatsapp.message"].sudo()

        for status in meta_payload.status_params(payload):
            try:
                Message._apply_status_callback(status)
            except Exception:  # one bad receipt must not drop the batch
                _logger.exception("Failed to apply Meta status update: %s", status)

        taken = 0
        for params in meta_payload.inbound_params(payload):
            sid = params.get("MessageSid")
            if sid and Message.search_count([("twilio_sid", "=", sid)]):
                # Meta retries until it gets a 200; never replay the bot.
                continue
            try:
                self._process_inbound_payload(params)
                taken += 1
            except Exception:
                _logger.exception("Failed to process inbound Meta message %s", sid)
                self.env.cr.rollback()
        return taken

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
        agent = self.env["ibq.whatsapp.agent"]._find_by_phone(sender)
        if not agent:
            return None
        if not self.env["ibq.whatsapp.command"].looks_like_command(body):
            # An agent may also be a customer. Anything that is not a command
            # falls through to the normal conversation path.
            return None

        inbound = self.env["ibq.whatsapp.message"].sudo().create({
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
        reply = self.env["ibq.whatsapp.command"].with_user(agent.user_id).execute(body)
        _logger.info("WhatsApp command from %s: %s", agent.user_id.name, body)

        self.env["ibq.whatsapp.message"].sudo().create({
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
