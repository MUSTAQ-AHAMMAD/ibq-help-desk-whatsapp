# -*- coding: utf-8 -*-
"""The Meta Cloud API provider: payload shape, signatures, and intake."""
import hashlib
import hmac
import json
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged

from ..models import meta_payload

META_CALL = (
    "odoo.addons.ibq_whatsapp_helpdesk.models.whatsapp_account."
    "WhatsappAccount._meta_call"
)

PHONE_NUMBER_ID = "109876543210987"


def envelope(*, messages=None, statuses=None, display="971501112222",
             phone_number_id=PHONE_NUMBER_ID, profile="Ann"):
    """Build a Meta webhook body around the given messages or statuses."""
    value = {
        "messaging_product": "whatsapp",
        "metadata": {
            "display_phone_number": display,
            "phone_number_id": phone_number_id,
        },
    }
    if messages:
        value["contacts"] = [{"profile": {"name": profile}, "wa_id": "971509998888"}]
        value["messages"] = messages
    if statuses:
        value["statuses"] = statuses
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": value}]}],
    }


def text_message(body="Hello", wamid="wamid.AAA", sender="971509998888"):
    return {"from": sender, "id": wamid, "timestamp": "1700000000",
            "type": "text", "text": {"body": body}}


@tagged("post_install", "-at_install")
class TestMetaPayload(TransactionCase):
    """The flattening layer, exercised without a database round trip."""

    def test_text_message_flattens_to_intake_keys(self):
        params = meta_payload.inbound_params(envelope(messages=[text_message()]))
        self.assertEqual(len(params), 1)
        self.assertEqual(params[0]["From"], "+971509998888")
        self.assertEqual(params[0]["To"], "+971501112222")
        self.assertEqual(params[0]["Body"], "Hello")
        self.assertEqual(params[0]["ProfileName"], "Ann")
        self.assertEqual(params[0]["MessageSid"], "wamid.AAA")
        self.assertEqual(params[0]["AccountSid"], PHONE_NUMBER_ID,
                         "the phone number id is what identifies the account")

    def test_bare_digits_become_e164(self):
        """Meta sends '971...'; everything downstream expects '+971...'."""
        params = meta_payload.inbound_params(envelope(messages=[text_message()]))
        self.assertTrue(params[0]["From"].startswith("+"))

    def test_button_reply_carries_its_id(self):
        message = {
            "from": "971509998888", "id": "wamid.B", "type": "interactive",
            "interactive": {"type": "button_reply",
                            "button_reply": {"id": "opt_2", "title": "Billing"}},
        }
        params = meta_payload.inbound_params(envelope(messages=[message]))[0]
        self.assertEqual(params["ButtonPayload"], "opt_2",
                         "the bot matches on the id, not the label")
        self.assertEqual(params["Body"], "Billing")

    def test_list_reply_uses_the_list_key(self):
        message = {
            "from": "971509998888", "id": "wamid.L", "type": "interactive",
            "interactive": {"type": "list_reply",
                            "list_reply": {"id": "row_3", "title": "Track a ticket"}},
        }
        params = meta_payload.inbound_params(envelope(messages=[message]))[0]
        self.assertEqual(params["ListId"], "row_3")
        self.assertNotIn("ButtonPayload", params)

    def test_template_quick_reply_button(self):
        message = {
            "from": "971509998888", "id": "wamid.Q", "type": "button",
            "button": {"payload": "reopen", "text": "Reopen"},
        }
        params = meta_payload.inbound_params(envelope(messages=[message]))[0]
        self.assertEqual(params["ButtonPayload"], "reopen")

    def test_media_passes_the_id_not_a_url(self):
        message = {
            "from": "971509998888", "id": "wamid.M", "type": "image",
            "image": {"id": "MEDIA123", "mime_type": "image/jpeg", "caption": "see this"},
        }
        params = meta_payload.inbound_params(envelope(messages=[message]))[0]
        self.assertEqual(params["NumMedia"], "1")
        self.assertEqual(params["MediaUrl0"], "MEDIA123")
        self.assertEqual(params["Body"], "see this",
                         "a caption is the customer talking and must not be lost")

    def test_several_messages_in_one_post(self):
        """Meta batches; handling only the first would silently drop chats."""
        payload = envelope(messages=[
            text_message("one", "wamid.1"),
            text_message("two", "wamid.2"),
        ])
        self.assertEqual(len(meta_payload.inbound_params(payload)), 2)

    def test_statuses_flatten_with_errors(self):
        payload = envelope(statuses=[{
            "id": "wamid.X", "status": "failed", "recipient_id": "971509998888",
            "errors": [{"code": 131047, "title": "Re-engagement message"}],
        }])
        status = meta_payload.status_params(payload)[0]
        self.assertEqual(status["MessageSid"], "wamid.X")
        self.assertEqual(status["MessageStatus"], "failed")
        self.assertEqual(status["ErrorCode"], "131047")
        self.assertEqual(status["ErrorMessage"], "Re-engagement message")

    def test_a_status_only_payload_yields_no_messages(self):
        payload = envelope(statuses=[{"id": "wamid.X", "status": "delivered"}])
        self.assertEqual(meta_payload.inbound_params(payload), [])
        self.assertEqual(len(meta_payload.status_params(payload)), 1)

    def test_non_message_fields_are_ignored(self):
        """Account-level updates share the webhook and must not be parsed."""
        payload = {"entry": [{"changes": [
            {"field": "account_review_update", "value": {"decision": "APPROVED"}},
        ]}]}
        self.assertEqual(meta_payload.inbound_params(payload), [])

    # ------------------------------------------------------------------
    # Outbound shape
    # ------------------------------------------------------------------
    def test_text_send_payload(self):
        payload = meta_payload.build_send_payload("+971509998888", body="Hi")
        self.assertEqual(payload["messaging_product"], "whatsapp")
        self.assertEqual(payload["to"], "971509998888", "Meta wants no leading +")
        self.assertEqual(payload["type"], "text")
        self.assertEqual(payload["text"]["body"], "Hi")
        self.assertFalse(payload["text"]["preview_url"])

    def test_template_send_payload(self):
        payload = meta_payload.build_send_payload(
            "+971509998888", template_name="ticket_created", template_language="en",
            template_components=meta_payload.build_template_components({"1": "Ann", "2": "42"}),
        )
        self.assertEqual(payload["type"], "template")
        self.assertEqual(payload["template"]["name"], "ticket_created")
        self.assertEqual(payload["template"]["language"], {"code": "en"})
        parameters = payload["template"]["components"][0]["parameters"]
        self.assertEqual([p["text"] for p in parameters], ["Ann", "42"],
                         "variables must stay in {{1}}, {{2}} order")

    def test_template_components_sort_numerically(self):
        """{{10}} must come after {{2}}, which string sorting gets wrong."""
        components = meta_payload.build_template_components(
            {"2": "two", "10": "ten", "1": "one"}
        )
        self.assertEqual(
            [p["text"] for p in components[0]["parameters"]], ["one", "two", "ten"]
        )


@tagged("post_install", "-at_install")
class TestMetaAccount(TransactionCase):
    """Credentials, signatures and intake on the account record."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.team = cls.env["helpdesk.team"].create({"name": "Meta Team"})
        cls.account = cls.env["ibq.whatsapp.account"].create({
            "name": "Meta sender",
            "provider": "meta",
            "meta_phone_number_id": PHONE_NUMBER_ID,
            "meta_access_token": "EAAG-token",
            "meta_app_secret": "app-secret",
            "meta_verify_token": "my-verify-token",
            "phone_number": "+971501112222",
            "team_id": cls.team.id,
        })

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------
    def test_meta_account_needs_no_twilio_credentials(self):
        self.assertFalse(self.account.account_sid)
        self.assertEqual(self.account.state, "draft")

    def test_phone_number_id_must_be_numeric(self):
        with self.assertRaises(ValidationError):
            self.account.write({"meta_phone_number_id": "+971501112222"})

    def test_meta_account_needs_an_access_token(self):
        with self.assertRaises(ValidationError):
            self.account.write({"meta_access_token": False})

    def test_twilio_account_still_validates_its_own_way(self):
        with self.assertRaises(ValidationError):
            self.env["ibq.whatsapp.account"].create({
                "name": "bad twilio", "provider": "twilio",
                "account_sid": "not-a-sid", "auth_token": "x",
                "phone_number": "+14155238886", "team_id": self.team.id,
            })

    def test_webhook_urls_follow_the_provider(self):
        self.assertTrue(self.account.inbound_webhook_url.endswith("/whatsapp/meta/webhook"))
        self.assertEqual(self.account.inbound_webhook_url,
                         self.account.status_webhook_url,
                         "Meta posts messages and receipts to one URL")

    # ------------------------------------------------------------------
    # Signatures
    # ------------------------------------------------------------------
    def _sign(self, raw, secret=b"app-secret"):
        return "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest()

    def test_valid_signature_accepted(self):
        raw = json.dumps(envelope(messages=[text_message()])).encode()
        self.assertTrue(self.account.validate_meta_signature(raw, self._sign(raw)))

    def test_tampered_body_rejected(self):
        raw = json.dumps(envelope(messages=[text_message()])).encode()
        signature = self._sign(raw)
        self.assertFalse(
            self.account.validate_meta_signature(raw + b" ", signature),
            "a signature must not survive the body changing",
        )

    def test_wrong_secret_rejected(self):
        raw = b'{"a":1}'
        self.assertFalse(
            self.account.validate_meta_signature(raw, self._sign(raw, b"other-secret"))
        )

    def test_missing_signature_rejected(self):
        self.assertFalse(self.account.validate_meta_signature(b"{}", None))

    def test_signature_check_can_be_disabled_for_local_debugging(self):
        self.account.verify_signature = False
        self.assertTrue(self.account.validate_meta_signature(b"{}", None))

    def test_verify_token_handshake(self):
        self.assertTrue(self.account.check_meta_verify_token("my-verify-token"))
        self.assertFalse(self.account.check_meta_verify_token("guess"))
        self.assertFalse(self.account.check_meta_verify_token(""))

    def test_blank_verify_token_never_matches(self):
        """An unset token must not turn the handshake into a free pass."""
        self.account.meta_verify_token = False
        self.assertFalse(self.account.check_meta_verify_token(""))
        self.assertFalse(self.account.check_meta_verify_token("anything"))

    # ------------------------------------------------------------------
    # Account resolution
    # ------------------------------------------------------------------
    def test_account_resolved_by_phone_number_id(self):
        payload = envelope(messages=[text_message()])
        found = self.env["ibq.whatsapp.account"]._account_for_meta_payload(payload)
        self.assertEqual(found, self.account)

    def test_wrong_sender_number_still_resolves(self):
        """The id is authoritative; a mistyped sender must not drop inbound."""
        self.account.phone_number = "+9715011122"   # a digit short, as a human would
        payload = envelope(messages=[text_message()])
        found = self.env["ibq.whatsapp.account"]._account_for_meta_payload(payload)
        self.assertEqual(found, self.account)

    # ------------------------------------------------------------------
    # Intake
    # ------------------------------------------------------------------
    def test_webhook_creates_a_conversation(self):
        with patch(META_CALL, return_value={"_ok": True, "sid": "wamid.OUT",
                                            "status": "accepted"}):
            taken = self.account._process_meta_webhook(
                envelope(messages=[text_message("My order is late")])
            )
        self.assertEqual(taken, 1)
        conversation = self.env["ibq.whatsapp.conversation"].search(
            [("number", "=", "+971509998888")], limit=1
        )
        self.assertTrue(conversation)
        self.assertEqual(conversation.account_id, self.account)
        self.assertTrue(conversation.whatsapp_message_ids.filtered(
            lambda m: m.body == "My order is late"
        ))

    def test_a_replayed_message_is_ignored(self):
        """Meta retries until it gets a 200; the bot must not answer twice."""
        payload = envelope(messages=[text_message("hello", "wamid.DUP")])
        with patch(META_CALL, return_value={"_ok": True, "sid": "x", "status": "accepted"}):
            self.assertEqual(self.account._process_meta_webhook(payload), 1)
            self.assertEqual(self.account._process_meta_webhook(payload), 0)
        self.assertEqual(
            self.env["ibq.whatsapp.message"].search_count(
                [("twilio_sid", "=", "wamid.DUP")]
            ), 1,
        )

    def test_status_update_marks_the_message_delivered(self):
        message = self.env["ibq.whatsapp.message"].create({
            "account_id": self.account.id, "direction": "outbound",
            "number": "+971509998888", "body": "hi", "twilio_sid": "wamid.SENT",
            "state": "sent",
        })
        self.account._process_meta_webhook(
            envelope(statuses=[{"id": "wamid.SENT", "status": "delivered"}])
        )
        self.assertEqual(message.state, "delivered")
        self.assertTrue(message.delivered_date)

    def test_failed_status_records_the_provider_error(self):
        message = self.env["ibq.whatsapp.message"].create({
            "account_id": self.account.id, "direction": "outbound",
            "number": "+971509998888", "body": "hi", "twilio_sid": "wamid.BAD",
            "state": "sent",
        })
        self.account._process_meta_webhook(envelope(statuses=[{
            "id": "wamid.BAD", "status": "failed",
            "errors": [{"code": 131047, "title": "Re-engagement message"}],
        }]))
        self.assertEqual(message.state, "failed")
        self.assertEqual(message.error_code, "131047")
        self.assertIn("Re-engagement", message.error_message)

    def test_messages_and_statuses_in_one_request(self):
        """Meta can batch both; draining only one half loses the other."""
        sent = self.env["ibq.whatsapp.message"].create({
            "account_id": self.account.id, "direction": "outbound",
            "number": "+971509998888", "body": "earlier", "twilio_sid": "wamid.E",
            "state": "sent",
        })
        payload = envelope(messages=[text_message("and a new one", "wamid.N")])
        payload["entry"][0]["changes"][0]["value"]["statuses"] = [
            {"id": "wamid.E", "status": "read"}
        ]
        with patch(META_CALL, return_value={"_ok": True, "sid": "x", "status": "accepted"}):
            self.assertEqual(self.account._process_meta_webhook(payload), 1)
        self.assertEqual(sent.state, "read")

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    def test_send_goes_through_the_graph_api(self):
        captured = {}

        def fake_call(self, method, path, json_body=None, params=None):
            captured.update(method=method, path=path, body=json_body)
            return {"_ok": True, "sid": "wamid.OUT", "status": "accepted"}

        with patch(META_CALL, fake_call):
            self.account._send_raw("+971509998888", body="Hello there")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "%s/messages" % PHONE_NUMBER_ID)
        self.assertEqual(captured["body"]["text"]["body"], "Hello there")

    def test_graph_error_is_surfaced_like_a_twilio_one(self):
        """_send_one records code and message; both providers must supply them."""
        message = self.env["ibq.whatsapp.message"].create({
            "account_id": self.account.id, "direction": "outbound",
            "number": "+971509998888", "body": "hi",
        })
        with patch(META_CALL, return_value={
            "_ok": False, "code": 131030, "message": "Recipient not in allowed list",
            "_http_status": 400,
        }):
            message._send_one()
        self.assertEqual(message.state, "failed")
        self.assertEqual(message.error_code, "131030")
        self.assertIn("allowed list", message.error_message)

    def test_test_connection_corrects_a_mistyped_sender(self):
        """Meta knows the real number; a human-typed one is often wrong."""
        self.account.phone_number = "+9715011122"
        with patch(META_CALL, return_value={
            "_ok": True, "display_phone_number": "+971501112222",
            "verified_name": "IBQ",
        }):
            self.account.action_test_connection()
        self.assertEqual(self.account.state, "connected")
        self.assertEqual(self.account.phone_number, "+971501112222")

    def test_test_connection_reports_a_rejected_token(self):
        """The verdict must persist: a UserError here would roll it back."""
        with patch(META_CALL, return_value={
            "_ok": False, "message": "Invalid OAuth access token", "_http_status": 401,
        }):
            action = self.account.action_test_connection()
        self.assertEqual(self.account.state, "error")
        self.assertIn("Invalid OAuth", self.account.last_error)
        self.assertEqual(action["params"]["type"], "danger")
        self.assertIn("Invalid OAuth", action["params"]["message"])

    def test_a_failed_test_does_not_wipe_a_previous_success(self):
        """Only the tested account changes; a batch must not cross-contaminate."""
        other = self.account.copy({
            "name": "second", "meta_phone_number_id": "222222222222222",
        })
        with patch(META_CALL, return_value={
            "_ok": True, "display_phone_number": "+971501112222",
        }):
            other.action_test_connection()
        self.assertEqual(other.state, "connected")
        with patch(META_CALL, return_value={"_ok": False, "message": "nope"}):
            self.account.action_test_connection()
        self.assertEqual(other.state, "connected")
        self.assertEqual(self.account.state, "error")
