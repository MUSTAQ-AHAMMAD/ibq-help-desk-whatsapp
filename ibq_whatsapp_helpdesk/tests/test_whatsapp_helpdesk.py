# -*- coding: utf-8 -*-
import itertools
from unittest.mock import patch

from markupsafe import Markup

from odoo.tests import TransactionCase, tagged

from ..models.whatsapp_account import normalize_number

SEND_PATH = (
    "odoo.addons.ibq_whatsapp_helpdesk.models.whatsapp_account."
    "WhatsappAccount._send_raw"
)


_sid_counter = itertools.count(1)


def fake_send(self, to_number, **kwargs):
    """Stand in for the Twilio REST call.

    The sid has to be unique per call, the way a real one is: the model puts
    a unique index on it, so a stub keyed on the number fails the moment you
    message the same person twice.
    """
    return {"sid": "SM%032d" % next(_sid_counter),
            "status": "queued", "_ok": True, "_http_status": 201}


@tagged("post_install", "-at_install")
class TestWhatsappHelpdesk(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.team = cls.env["helpdesk.team"].create({"name": "WhatsApp Test Team"})
        cls.flow = cls.env["whatsapp.bot.flow"].create({
            "name": "Test flow",
            "greeting": "Hello {name}!",
            "agent_keywords": "agent",
            "close_keywords": "stop",
            "restart_keywords": "menu",
            "max_invalid_attempts": 2,
        })
        cls.step_menu = cls.env["whatsapp.bot.step"].create({
            "flow_id": cls.flow.id, "name": "Menu", "step_type": "menu",
            "body": "Pick one:", "sequence": 10,
        })
        cls.step_subject = cls.env["whatsapp.bot.step"].create({
            "flow_id": cls.flow.id, "name": "Subject", "step_type": "question",
            "body": "Describe it.", "answer_key": "subject", "sequence": 20,
        })
        cls.step_ticket = cls.env["whatsapp.bot.step"].create({
            "flow_id": cls.flow.id, "name": "Ticket", "step_type": "ticket",
            "body": "Ticket #{ticket_ref} created.", "team_id": cls.team.id,
            "subject_key": "subject", "sequence": 30,
        })
        cls.step_agent = cls.env["whatsapp.bot.step"].create({
            "flow_id": cls.flow.id, "name": "Agent", "step_type": "agent",
            "body": "Connecting you.", "sequence": 40,
        })
        cls.step_subject.next_step_id = cls.step_ticket
        cls.step_ticket.next_step_id = cls.step_agent
        cls.flow.start_step_id = cls.step_menu
        cls.env["whatsapp.bot.option"].create({
            "step_id": cls.step_menu.id, "key": "1", "name": "Technical issue",
            "keywords": "broken,bug", "answer_key": "category",
            "next_step_id": cls.step_subject.id,
        })
        cls.env["whatsapp.bot.option"].create({
            "step_id": cls.step_menu.id, "key": "2", "name": "Talk to an agent",
            "next_step_id": cls.step_agent.id,
        })
        cls.account = cls.env["whatsapp.account"].create({
            "name": "Test sender",
            "account_sid": "AC" + "0" * 32,
            "auth_token": "s3cr3t-token",
            "phone_number": "+14155238886",
            "team_id": cls.team.id,
            "bot_flow_id": cls.flow.id,
            "verify_signature": True,
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _inbound(self, body, from_number="+971501234567", **extra):
        params = {
            "From": "whatsapp:%s" % from_number,
            "To": "whatsapp:+14155238886",
            "Body": body,
            "AccountSid": self.account.account_sid,
            "MessageSid": "SM%s" % abs(hash((from_number, body, len(extra)))),
            "ProfileName": "Test Customer",
            "NumMedia": "0",
        }
        params.update(extra)
        with patch(SEND_PATH, fake_send):
            return self.account._process_inbound_payload(params)

    # ------------------------------------------------------------------
    # Number normalisation
    # ------------------------------------------------------------------
    def test_normalize_number(self):
        for raw, expected in [
            ("whatsapp:+971501234567", "+971501234567"),
            ("+971 50 123 4567", "+971501234567"),
            ("00971501234567", "+971501234567"),
            ("971501234567", "+971501234567"),
            ("", False),
            (None, False),
        ]:
            self.assertEqual(normalize_number(raw), expected, raw)

    # ------------------------------------------------------------------
    # Webhook signature
    # ------------------------------------------------------------------
    def test_signature_round_trip(self):
        url = "https://odoo.example.com/whatsapp/twilio/inbound"
        params = {"From": "whatsapp:+971501234567", "Body": "hi", "NumMedia": "0"}
        signature = self.account._compute_signature(url, params)
        self.assertTrue(self.account.validate_signature(url, params, signature))

    def test_signature_rejects_tampering(self):
        url = "https://odoo.example.com/whatsapp/twilio/inbound"
        params = {"From": "whatsapp:+971501234567", "Body": "hi"}
        signature = self.account._compute_signature(url, params)
        tampered = dict(params, Body="transfer my balance")
        self.assertFalse(self.account.validate_signature(url, tampered, signature))
        self.assertFalse(self.account.validate_signature(url, params, None))

    def test_signature_skipped_when_disabled(self):
        self.account.verify_signature = False
        self.assertTrue(self.account.validate_signature("http://x", {}, None))

    # ------------------------------------------------------------------
    # Bot engine
    # ------------------------------------------------------------------
    def test_first_message_starts_the_flow(self):
        message = self._inbound("hi there")
        conversation = message.conversation_id
        self.assertEqual(conversation.state, "bot")
        self.assertEqual(conversation.number, "+971501234567")
        self.assertEqual(conversation.bot_pending_step_id, self.step_menu)
        bodies = conversation.whatsapp_message_ids.filtered(
            lambda m: m.direction == "outbound"
        ).mapped("body")
        self.assertTrue(any("Hello Test Customer!" in b for b in bodies))
        self.assertTrue(any("1. Technical issue" in b for b in bodies))

    def test_menu_choice_then_ticket_creation(self):
        self._inbound("hi")
        self._inbound("1")
        conversation = self.env["whatsapp.conversation"].search(
            [("number", "=", "+971501234567")], limit=1
        )
        self.assertEqual(conversation.bot_pending_step_id, self.step_subject)
        self.assertEqual(conversation._get_answers().get("category"), "Technical issue")

        self._inbound("The printer is jammed")
        conversation.invalidate_recordset()
        ticket = conversation.ticket_id
        self.assertTrue(ticket, "the ticket step should have created a ticket")
        self.assertEqual(ticket.name, "The printer is jammed")
        self.assertEqual(ticket.team_id, self.team)
        self.assertEqual(conversation.state, "agent",
                         "the flow ends on the agent hand-off step")

    def test_menu_matches_keyword_not_only_digit(self):
        self._inbound("hi")
        self._inbound("broken")
        conversation = self.env["whatsapp.conversation"].search(
            [("number", "=", "+971501234567")], limit=1
        )
        self.assertEqual(conversation.bot_pending_step_id, self.step_subject)

    def test_agent_keyword_short_circuits_the_flow(self):
        self._inbound("hi")
        self._inbound("agent")
        conversation = self.env["whatsapp.conversation"].search(
            [("number", "=", "+971501234567")], limit=1
        )
        self.assertEqual(conversation.state, "agent")
        self.assertTrue(conversation.ticket_id, "auto_create_ticket is on by default")

    def test_repeated_invalid_answers_reach_an_agent(self):
        self._inbound("hi")
        self._inbound("nonsense one")
        self._inbound("nonsense two")
        conversation = self.env["whatsapp.conversation"].search(
            [("number", "=", "+971501234567")], limit=1
        )
        self.assertEqual(conversation.state, "agent")

    def test_close_keyword_closes_and_next_message_reopens(self):
        self._inbound("hi")
        self._inbound("stop")
        conversation = self.env["whatsapp.conversation"].search(
            [("number", "=", "+971501234567")], limit=1
        )
        self.assertEqual(conversation.state, "closed")
        self._inbound("hello again")
        conversation.invalidate_recordset()
        self.assertEqual(conversation.state, "bot")
        self.assertEqual(conversation._get_answers(), {},
                         "a reopened chat starts from a clean slate")

    def test_button_payload_is_used_as_the_reply(self):
        self._inbound("hi")
        self._inbound("", ButtonPayload="2")
        conversation = self.env["whatsapp.conversation"].search(
            [("number", "=", "+971501234567")], limit=1
        )
        self.assertEqual(conversation.state, "agent")

    # ------------------------------------------------------------------
    # Session window
    # ------------------------------------------------------------------
    def test_free_text_blocked_outside_the_session_window(self):
        from odoo import fields
        from datetime import timedelta

        message = self._inbound("hi")
        conversation = message.conversation_id
        conversation.last_inbound_date = fields.Datetime.now() - timedelta(hours=25)
        conversation.invalidate_recordset()
        self.assertFalse(conversation.in_session)
        with self.assertRaises(Exception):
            conversation.send_text("Are you still there?")

    # ------------------------------------------------------------------
    # Delivery status callbacks
    # ------------------------------------------------------------------
    def test_status_callback_updates_the_message(self):
        message = self._inbound("hi")
        outbound = message.conversation_id.whatsapp_message_ids.filtered(
            lambda m: m.direction == "outbound"
        )[:1]
        outbound.twilio_sid = "SM_status_test"
        self.env["whatsapp.message"]._apply_status_callback({
            "MessageSid": "SM_status_test", "MessageStatus": "delivered",
        })
        self.assertEqual(outbound.state, "delivered")
        self.assertTrue(outbound.delivered_date)

        self.env["whatsapp.message"]._apply_status_callback({
            "MessageSid": "SM_status_test", "MessageStatus": "failed",
            "ErrorCode": "63016",
        })
        self.assertEqual(outbound.state, "failed")
        self.assertEqual(outbound.error_code, "63016")

    # ------------------------------------------------------------------
    # Templates
    # ------------------------------------------------------------------
    def test_template_rendering(self):
        template = self.env["whatsapp.template"].create({
            "name": "Ticket update", "code": "test_ticket_update",
            "body": "Ticket #{{1}} is now {{2}}.",
            "variable_ids": [
                (0, 0, {"index": 1, "name": "Ref", "source_type": "field",
                        "field_path": "id"}),
                (0, 0, {"index": 2, "name": "Stage", "source_type": "field",
                        "field_path": "stage_id.name"}),
            ],
        })
        ticket = self.env["helpdesk.ticket"].create({
            "name": "Broken printer", "team_id": self.team.id,
        })
        body, content_variables = template.render(ticket)
        self.assertIn("Ticket #%s is now" % ticket.id, body)
        self.assertIn('"1": "%s"' % ticket.id, content_variables)

    # ------------------------------------------------------------------
    # Chatter relay
    # ------------------------------------------------------------------
    def test_agent_chatter_reply_goes_out_over_whatsapp(self):
        self._inbound("hi")
        self._inbound("agent")
        conversation = self.env["whatsapp.conversation"].search(
            [("number", "=", "+971501234567")], limit=1
        )
        ticket = conversation.ticket_id
        before = len(conversation.whatsapp_message_ids)
        with patch(SEND_PATH, fake_send):
            # Markup, not a bare string: Odoo escapes plain strings passed as a
            # body, so a str would arrive at the customer as literal tags. Real
            # chatter posts are Markup.
            ticket.message_post(body=Markup("<p>We are on it.</p>"),
                                subtype_xmlid="mail.mt_comment")
        conversation.invalidate_recordset()
        relayed = conversation.whatsapp_message_ids.filtered(
            lambda m: m.body == "We are on it."
        )
        self.assertTrue(relayed, "the public reply should reach WhatsApp")
        self.assertGreater(len(conversation.whatsapp_message_ids), before)

    def test_internal_note_is_not_relayed(self):
        self._inbound("hi")
        self._inbound("agent")
        conversation = self.env["whatsapp.conversation"].search(
            [("number", "=", "+971501234567")], limit=1
        )
        with patch(SEND_PATH, fake_send):
            conversation.ticket_id.message_post(
                body=Markup("<p>Internal: check the logs.</p>"),
                subtype_xmlid="mail.mt_note",
            )
        conversation.invalidate_recordset()
        self.assertFalse(conversation.whatsapp_message_ids.filtered(
            lambda m: "check the logs" in (m.body or "")
        ))
