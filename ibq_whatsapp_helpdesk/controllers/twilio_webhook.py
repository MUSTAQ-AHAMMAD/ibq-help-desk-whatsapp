# -*- coding: utf-8 -*-
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
XML_HEADERS = [("Content-Type", "text/xml; charset=utf-8")]


class TwilioWebhookController(http.Controller):
    """Endpoints Twilio calls back into.

    Both routes answer with empty TwiML: replies are pushed through the REST
    API instead, which keeps the bot free to send several messages in a row
    and to send nothing at all.
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _form_params(self):
        return request.httprequest.form.to_dict(flat=True)

    def _signed_url(self, account):
        """Rebuild the URL exactly as Twilio saw it when it signed the request."""
        url = request.httprequest.url
        if account and account.webhook_base_url:
            base = account.webhook_base_url.rstrip("/")
            url = base + request.httprequest.full_path.rstrip("?")
        return url

    def _reject(self, reason, params=None):
        _logger.warning("Rejected Twilio webhook: %s (%s)", reason, params or {})
        return request.make_response(EMPTY_TWIML, headers=XML_HEADERS)

    # ------------------------------------------------------------------
    # Inbound messages
    # ------------------------------------------------------------------
    @http.route(
        "/whatsapp/twilio/inbound",
        type="http", auth="public", methods=["POST"], csrf=False, save_session=False,
    )
    def inbound(self, **post):
        params = self._form_params()
        env = request.env
        if env["ir.config_parameter"].sudo().get_param("ibq_whatsapp.log_webhooks") == "True":
            _logger.info("Twilio inbound payload: %s", params)

        account = env["whatsapp.account"].sudo()._find_for_inbound(
            params.get("To"), params.get("AccountSid")
        )
        if not account:
            return self._reject("no matching WhatsApp account", params)

        signature = request.httprequest.headers.get("X-Twilio-Signature")
        if not account.validate_signature(self._signed_url(account), params, signature):
            return self._reject("invalid X-Twilio-Signature", {"To": params.get("To")})

        message_sid = params.get("MessageSid") or params.get("SmsMessageSid")
        if message_sid and env["whatsapp.message"].sudo().search_count(
            [("twilio_sid", "=", message_sid)]
        ):
            # Twilio retries on timeout; do not replay the bot for a duplicate.
            return request.make_response(EMPTY_TWIML, headers=XML_HEADERS)

        try:
            account._process_inbound_payload(params)
        except Exception:  # always answer Twilio with valid TwiML
            _logger.exception("Failed to process inbound WhatsApp message %s", message_sid)
            request.env.cr.rollback()
        return request.make_response(EMPTY_TWIML, headers=XML_HEADERS)

    # ------------------------------------------------------------------
    # Delivery status callbacks
    # ------------------------------------------------------------------
    @http.route(
        "/whatsapp/twilio/status",
        type="http", auth="public", methods=["POST"], csrf=False, save_session=False,
    )
    def status(self, **post):
        params = self._form_params()
        env = request.env
        account = env["whatsapp.account"].sudo()._find_for_inbound(
            params.get("From"), params.get("AccountSid")
        )
        if account and not account.validate_signature(
            self._signed_url(account), params,
            request.httprequest.headers.get("X-Twilio-Signature"),
        ):
            return self._reject("invalid signature on status callback")
        try:
            env["whatsapp.message"].sudo()._apply_status_callback(params)
        except Exception:  # noqa: BLE001
            _logger.exception("Failed to apply Twilio status callback: %s", params)
            request.env.cr.rollback()
        return request.make_response(EMPTY_TWIML, headers=XML_HEADERS)

    # ------------------------------------------------------------------
    # Health check, handy when wiring up ngrok
    # ------------------------------------------------------------------
    @http.route("/whatsapp/twilio/health", type="http", auth="public", methods=["GET"])
    def health(self, **kwargs):
        count = request.env["whatsapp.account"].sudo().search_count([])
        return request.make_response(
            "ok: %s WhatsApp account(s) configured" % count,
            headers=[("Content-Type", "text/plain")],
        )
