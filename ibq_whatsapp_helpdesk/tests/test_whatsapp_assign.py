# -*- coding: utf-8 -*-
"""Assigning a chat: from Odoo, and by texting a command to the support number."""
import itertools
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

SEND_PATH = (
    "odoo.addons.ibq_whatsapp_helpdesk.models.whatsapp_account."
    "WhatsappAccount._send_raw"
)

_sid_counter = itertools.count(1)
sent = []


def fake_send(self, to_number, body=None, **kwargs):
    """Record what would have gone to Twilio instead of sending it."""
    sent.append({"to": to_number, "body": body})
    return {"sid": "SM%032d" % next(_sid_counter), "status": "queued",
            "_ok": True, "_http_status": 201}


@tagged("post_install", "-at_install")
class TestWhatsappAssign(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.team = cls.env["helpdesk.team"].create({"name": "Assign Team"})
        cls.other_team = cls.env["helpdesk.team"].create({"name": "Other Team"})
        cls.account = cls.env["whatsapp.account"].create({
            "name": "Assign sender",
            "account_sid": "AC" + "3" * 32,
            "auth_token": "token",
            "phone_number": "+14155238889",
            "team_id": cls.team.id,
            "verify_signature": False,
        })
        cls.env["whatsapp.tag"].create({"name": "Billing"})

        cls.sue_user = cls._make_user("sue_a", "Sue Supervisor")
        cls.alex_user = cls._make_user("alex_a", "Alex Agent")
        cls.nadia_user = cls._make_user("nadia_a", "Nadia Agent")

        Agent = cls.env["whatsapp.agent"]
        cls.sue = Agent.create({
            "user_id": cls.sue_user.id, "role": "supervisor",
            "phone": "+971500000001", "status": "available",
        })
        cls.alex = Agent.create({
            "user_id": cls.alex_user.id, "role": "agent",
            "phone": "+971500000002", "status": "available",
        })
        cls.nadia = Agent.create({
            "user_id": cls.nadia_user.id, "role": "agent",
            "phone": "+971500000003", "status": "available",
            "display_alias": "Nadia from IBQ",
        })

    @classmethod
    def _make_user(cls, login, name):
        return cls.env["res.users"].create({
            "name": name, "login": login, "email": "%s@example.com" % login,
        })

    def setUp(self):
        super().setUp()
        sent.clear()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _chat(self, number="+971559000001", **values):
        conversation = self.env["whatsapp.conversation"]._get_or_create(
            self.account, number
        )
        ticket = self.env["helpdesk.ticket"].create({
            "name": "Printer jam", "team_id": self.team.id,
        })
        conversation.write(dict({
            "state": "agent",
            "ticket_id": ticket.id,
            "last_inbound_date": "2999-01-01 00:00:00",
        }, **values))
        ticket.write({"whatsapp_conversation_id": conversation.id,
                      "whatsapp_number": conversation.number})
        return conversation

    def _text(self, agent_phone, body):
        """Simulate an agent texting the support number."""
        with patch(SEND_PATH, fake_send):
            return self.account._process_inbound_payload({
                "From": "whatsapp:%s" % agent_phone,
                "To": "whatsapp:+14155238889",
                "Body": body,
                "MessageSid": "SM%s" % next(_sid_counter),
                "NumMedia": "0",
            })

    def _last_reply(self):
        return sent[-1]["body"] if sent else ""

    # ==================================================================
    # Assigning from Odoo
    # ==================================================================
    def test_assigning_the_chat_assigns_the_ticket(self):
        conversation = self._chat()
        with patch(SEND_PATH, fake_send):
            conversation.user_id = self.alex_user
        self.assertEqual(conversation.ticket_id.user_id, self.alex_user,
                         "the ticket has to follow the chat")

    def test_unassigning_the_chat_unassigns_the_ticket(self):
        conversation = self._chat()
        with patch(SEND_PATH, fake_send):
            conversation.user_id = self.alex_user
            conversation.user_id = False
        self.assertFalse(conversation.ticket_id.user_id)

    def test_changing_the_department_moves_the_ticket(self):
        conversation = self._chat()
        with patch(SEND_PATH, fake_send):
            conversation.team_id = self.other_team
        self.assertEqual(conversation.ticket_id.team_id, self.other_team)

    def test_assign_to_me_action(self):
        conversation = self._chat()
        with patch(SEND_PATH, fake_send):
            conversation.with_user(self.alex_user).action_assign_to_me()
        self.assertEqual(conversation.user_id, self.alex_user)
        self.assertEqual(conversation.ticket_id.user_id, self.alex_user)

    def test_assignment_leaves_a_trail_on_the_ticket(self):
        conversation = self._chat()
        with patch(SEND_PATH, fake_send):
            conversation.assign_to(self.alex_user, source="a test")
        self.assertTrue(any(
            "Assigned to Alex Agent" in (m.body or "")
            for m in conversation.ticket_id.message_ids
        ))

    def test_only_roster_members_can_be_assigned(self):
        outsider = self._make_user("outsider_a", "Otto Outsider")
        conversation = self._chat()
        with self.assertRaises(UserError):
            conversation.assign_to(outsider)

    def test_agents_cannot_assign_to_someone_else(self):
        conversation = self._chat()
        with self.assertRaises(AccessError):
            conversation.with_user(self.alex_user).assign_to(self.nadia_user)

    def test_supervisors_can_assign_to_someone_else(self):
        conversation = self._chat()
        with patch(SEND_PATH, fake_send):
            conversation.with_user(self.sue_user).assign_to(self.nadia_user)
        self.assertEqual(conversation.user_id, self.nadia_user)

    # ==================================================================
    # Telling the customer
    # ==================================================================
    def test_customer_is_told_who_picked_it_up(self):
        conversation = self._chat()
        with patch(SEND_PATH, fake_send):
            conversation.assign_to(self.alex_user)
        self.assertIn("Alex Agent will be looking after this",
                      [s["body"] for s in sent][-1])

    def test_customer_is_told_about_a_handover(self):
        conversation = self._chat()
        with patch(SEND_PATH, fake_send):
            conversation.assign_to(self.alex_user)
            sent.clear()
            conversation.assign_to(self.nadia_user)
        self.assertIn("Nadia from IBQ is taking over", self._last_reply(),
                      "the alias is what the customer should see")

    def test_no_announcement_outside_the_session_window(self):
        conversation = self._chat(last_inbound_date="2020-01-01 00:00:00")
        with patch(SEND_PATH, fake_send):
            conversation.assign_to(self.alex_user)
        self.assertFalse(sent, "an expired window must not be spent on routing news")

    def test_no_announcement_while_the_bot_still_owns_it(self):
        conversation = self._chat(state="bot")
        with patch(SEND_PATH, fake_send):
            conversation.assign_to(self.alex_user)
        self.assertFalse(sent)

    def test_no_announcement_when_the_agent_does_not_change(self):
        conversation = self._chat()
        with patch(SEND_PATH, fake_send):
            conversation.assign_to(self.alex_user)
            sent.clear()
            conversation.user_id = self.alex_user
        self.assertFalse(sent)

    # ==================================================================
    # Commands by text
    # ==================================================================
    def test_a_command_never_becomes_a_conversation(self):
        before = self.env["whatsapp.conversation"].search_count([])
        self._text("+971500000002", "#help")
        self.assertEqual(self.env["whatsapp.conversation"].search_count([]), before,
                         "an agent texting a command must not enter the queue")
        self.assertIn("#assign", self._last_reply())

    def test_the_reply_goes_back_to_the_agent_only(self):
        self._text("+971500000002", "#help")
        self.assertEqual(sent[-1]["to"], "+971500000002")

    def test_take_claims_the_chat_and_the_ticket(self):
        conversation = self._chat()
        ref = str(conversation.ticket_id.id)
        self._text("+971500000002", "#take %s" % ref)
        conversation.invalidate_recordset()
        self.assertEqual(conversation.user_id, self.alex_user)
        self.assertEqual(conversation.ticket_id.user_id, self.alex_user)
        self.assertIn("is yours", self._last_reply())

    def test_assign_by_name(self):
        conversation = self._chat()
        self._text("+971500000001",
                   "#assign %s nadia_a" % conversation.ticket_id.id)
        conversation.invalidate_recordset()
        self.assertEqual(conversation.user_id, self.nadia_user)

    def test_assign_by_customer_number_instead_of_ticket(self):
        conversation = self._chat("+971559000009")
        self._text("+971500000001", "#assign +971559000009 nadia_a")
        conversation.invalidate_recordset()
        self.assertEqual(conversation.user_id, self.nadia_user)

    def test_an_agent_cannot_assign_to_others_by_text(self):
        conversation = self._chat()
        self._text("+971500000002",
                   "#assign %s nadia_a" % conversation.ticket_id.id)
        conversation.invalidate_recordset()
        self.assertFalse(conversation.user_id)
        self.assertIn("does not allow", self._last_reply())

    def test_ambiguous_agent_name_asks_for_precision(self):
        self.env["whatsapp.agent"].create({
            "user_id": self._make_user("alex_b", "Alexandra Other").id,
            "role": "agent",
        })
        conversation = self._chat()
        self._text("+971500000001", "#assign %s alex" % conversation.ticket_id.id)
        self.assertIn("matches several people", self._last_reply())

    def test_unknown_ticket_is_reported(self):
        self._text("+971500000002", "#take 999999")
        self.assertIn("No chat found", self._last_reply())

    def test_unknown_command_is_reported(self):
        self._text("+971500000002", "#frobnicate 1")
        self.assertIn("Unknown command", self._last_reply())

    def test_close_by_text(self):
        conversation = self._chat()
        self._text("+971500000001", "#close %s" % conversation.ticket_id.id)
        conversation.invalidate_recordset()
        self.assertEqual(conversation.state, "closed")

    def test_note_by_text_stays_internal(self):
        conversation = self._chat()
        self._text("+971500000002",
                   "#note %s rang them back" % conversation.ticket_id.id)
        conversation.invalidate_recordset()
        self.assertTrue(any("rang them back" in n["body"]
                            for n in conversation._note_payload()))
        self.assertFalse(
            conversation.whatsapp_message_ids.filtered(
                lambda m: "rang them back" in (m.body or "")
            ),
            "a note must never reach the customer",
        )

    def test_tag_by_text(self):
        conversation = self._chat()
        self._text("+971500000001", "#tag %s billing" % conversation.ticket_id.id)
        conversation.invalidate_recordset()
        self.assertEqual(conversation.tag_ids.mapped("name"), ["Billing"])

    def test_status_by_text(self):
        self._text("+971500000002", "#status busy")
        self.assertEqual(self.alex.status, "busy")
        self._text("+971500000002", "#status nonsense")
        self.assertIn("must be one of", self._last_reply())

    def test_queue_and_mine(self):
        conversation = self._chat(needs_reply=True)
        self._text("+971500000001", "#queue")
        self.assertIn(str(conversation.ticket_id.id), self._last_reply())

        self._text("+971500000002", "#mine")
        self.assertIn("no open chats", self._last_reply())
        self._text("+971500000002", "#take %s" % conversation.ticket_id.id)
        self._text("+971500000002", "#mine")
        self.assertIn(str(conversation.ticket_id.id), self._last_reply())

    def test_who_reports_the_owner(self):
        conversation = self._chat()
        self._text("+971500000002", "#take %s" % conversation.ticket_id.id)
        self._text("+971500000001", "#who %s" % conversation.ticket_id.id)
        self.assertIn("Alex Agent", self._last_reply())

    def test_a_non_command_from_an_agent_is_a_normal_chat(self):
        """An agent may also be a customer; only '#' means a command."""
        before = self.env["whatsapp.conversation"].search_count([])
        self._text("+971500000002", "I need help with my own order")
        self.assertEqual(self.env["whatsapp.conversation"].search_count([]),
                         before + 1)

    def test_commands_can_be_switched_off_per_account(self):
        self.account.allow_agent_commands = False
        before = self.env["whatsapp.conversation"].search_count([])
        self._text("+971500000002", "#help")
        self.assertEqual(self.env["whatsapp.conversation"].search_count([]),
                         before + 1, "with commands off it is just a message")

    def test_a_stranger_texting_a_hash_is_not_a_command(self):
        before = self.env["whatsapp.conversation"].search_count([])
        self._text("+971559999999", "#take 1")
        self.assertEqual(self.env["whatsapp.conversation"].search_count([]),
                         before + 1,
                         "only numbers on the roster can run commands")
