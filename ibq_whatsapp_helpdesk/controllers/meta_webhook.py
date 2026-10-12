# -*- coding: utf-8 -*-
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class MetaWebhookController(http.Controller):
    """The single callback Meta posts to.

    Unlike Twilio, Meta uses one URL for everything: inbound messages and
    delivery receipts arrive on the same endpoint, and a GET on it performs
    the one-off subscription handshake.

    Meta retries any response that is not a 2xx, so this answers 200 even for
    payloads it cannot use. A webhook that returns an error gets redelivered
    for hours and eventually disables the subscription.
    """

    def _ok(self, body="OK", status=200, content_type="text/plain"):
        return request.make_response(
            body, status=status, headers=[("Content-Type", content_type)]
        )

    # ------------------------------------------------------------------
    # Subscription handshake
    # ------------------------------------------------------------------
    @http.route(
        "/whatsapp/meta/webhook",
        type="http", auth="public", methods=["GET"], csrf=False, save_session=False,
    )
    def verify(self, **kwargs):
        """Echo hub.challenge when the verify token matches.

        Meta calls this once, when the webhook is first subscribed in the app
        dashboard. Returning anything but the bare challenge fails setup.
        """
        mode = kwargs.get("hub.mode")
        token = kwargs.get("hub.verify_token")
        challenge = kwargs.get("hub.challenge") or ""

        if mode != "subscribe":
            return self._ok("unexpected hub.mode", status=400)

        accounts = request.env["ibq.whatsapp.account"].sudo().search(
            [("provider", "=", "meta")]
        )
        if any(account.check_meta_verify_token(token) for account in accounts):
            _logger.info("Meta webhook subscription verified")
            return self._ok(challenge)

        _logger.warning("Meta webhook verification failed: token did not match")
        return self._ok("verification failed", status=403)

    # ------------------------------------------------------------------
    # Inbound messages and delivery receipts
    # ------------------------------------------------------------------
    @http.route(
        "/whatsapp/meta/webhook",
        type="http", auth="public", methods=["POST"], csrf=False, save_session=False,
    )
    def inbound(self, **kwargs):
        # The exact bytes, before any parsing: the signature is computed over
        # them, and re-serialising the parsed JSON would change the digest.
        raw_body = request.httprequest.get_data()
        try:
            payload = json.loads(raw_body or b"{}")
        except ValueError:
            _logger.warning("Meta webhook sent a body that is not JSON")
            return self._ok()

        env = request.env
        if env["ir.config_parameter"].sudo().get_param("ibq_whatsapp.log_webhooks") == "True":
            _logger.info("Meta inbound payload: %s", payload)

        account = env["ibq.whatsapp.account"].sudo()._account_for_meta_payload(payload)
        if not account:
            _logger.warning("Meta webhook arrived with no matching WhatsApp account")
            return self._ok()

        signature = request.httprequest.headers.get("X-Hub-Signature-256")
        if not account.validate_meta_signature(raw_body, signature):
            _logger.warning(
                "Rejected Meta webhook: invalid X-Hub-Signature-256 for account %s",
                account.name,
            )
            return self._ok()

        try:
            account._process_meta_webhook(payload)
        except Exception:  # always answer 200 so Meta stops retrying
            _logger.exception("Failed to handle Meta webhook")
            request.env.cr.rollback()
        return self._ok()

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------
    @http.route("/whatsapp/meta/health", type="http", auth="public", methods=["GET"])
    def health(self, **kwargs):
        count = request.env["ibq.whatsapp.account"].sudo().search_count(
            [("provider", "=", "meta")]
        )
        return self._ok("ok: %s Meta WhatsApp account(s) configured" % count)
