# -*- coding: utf-8 -*-
"""The ticket transcript, and grouping chats into repeated vs one-off issues."""
import itertools
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from ..models.whatsapp_issue import similarity, tokenise

SEND_PATH = (
    "odoo.addons.ibq_whatsapp_helpdesk.models.whatsapp_account."
    "WhatsappAccount._send_raw"
)
_sid = itertools.count(1)


def fake_send(self, to_number, **kwargs):
    return {"sid": "SM%032d" % next(_sid), "status": "queued",
            "_ok": True, "_http_status": 201}


@tagged("post_install", "-at_install")
class TestWhatsappIssue(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.team = cls.env["helpdesk.team"].create({"name": "Issue Team"})
        cls.account = cls.env["whatsapp.account"].create({
            "name": "Issue sender",
            "account_sid": "AC" + "4" * 32,
            "auth_token": "token",
            "phone_number": "+14155238890",
            "team_id": cls.team.id,
            "verify_signature": False,
        })

    def _chat(self, number, subject, **values):
        conversation = self.env["whatsapp.conversation"]._get_or_create(
            self.account, number
        )
        conversation.write(dict({
            "answers": '{"subject": %s}' % _json(subject),
            "team_id": self.team.id,
            "last_inbound_date": "2999-01-01 00:00:00",
        }, **values))
        return conversation

    # ==================================================================
    # Normalising a subject
    # ==================================================================
    def test_tokenise_drops_noise(self):
        self.assertEqual(
            tokenise("Hi, please can you help, my printer is jammed again"),
            {"help", "printer", "jammed"},
        )

    def test_tokenise_drops_reference_numbers(self):
        self.assertEqual(tokenise("Invoice 4471 shows the wrong VAT"),
                         tokenise("Invoice 5120 shows the wrong VAT"),
                         "a reference number must not split one issue in two")

    def test_similarity_is_symmetric_and_bounded(self):
        left, right = {"printer", "jammed"}, {"printer", "jammed", "floor"}
        self.assertEqual(similarity(left, right), similarity(right, left))
        self.assertEqual(similarity(left, left), 1.0)
        self.assertEqual(similarity(left, set()), 0.0)

    # ==================================================================
    # Grouping
    # ==================================================================
    def test_similar_subjects_become_one_issue(self):
        first = self._chat("+971570000001", "The office printer is jammed")
        second = self._chat("+971570000002", "printer jammed again")
        Issue = self.env["whatsapp.issue"]
        Issue._match_or_create(first)
        Issue._match_or_create(second)
        self.assertTrue(first.issue_id)
        self.assertEqual(first.issue_id, second.issue_id)
        self.assertEqual(first.issue_id.occurrence_count, 2)
        self.assertEqual(first.issue_id.kind, "repeated")
        self.assertEqual(first.issue_id.contact_count, 2)

    def test_different_subjects_stay_separate(self):
        first = self._chat("+971570000010", "The office printer is jammed")
        second = self._chat("+971570000011", "Refund for the cancelled order")
        Issue = self.env["whatsapp.issue"]
        Issue._match_or_create(first)
        Issue._match_or_create(second)
        self.assertNotEqual(first.issue_id, second.issue_id)
        self.assertEqual(first.issue_id.kind, "unique")

    def test_a_chat_with_no_words_is_left_unclassified(self):
        conversation = self._chat("+971570000020", "hi")
        self.assertFalse(self.env["whatsapp.issue"]._match_or_create(conversation))
        self.assertFalse(conversation.issue_id)

    def test_filler_never_becomes_an_issue(self):
        """"Anyone there?" is someone waiting, not a problem to catalogue."""
        for number, filler in (
            ("+971570000021", "Anyone there?"),
            ("+971570000022", "hello?"),
            ("+971570000023", "Thanks"),
            ("+971570000024", "printer"),
        ):
            conversation = self._chat(number, filler)
            self.env["whatsapp.issue"]._match_or_create(conversation)
            self.assertFalse(conversation.issue_id,
                             "%r should not become an issue" % filler)

    def test_subject_falls_back_to_the_first_real_message(self):
        conversation = self.env["whatsapp.conversation"]._get_or_create(
            self.account, "+971570000030"
        )
        for body in ("Hi", "The scanner shows error E-042"):
            self.env["whatsapp.message"].create({
                "conversation_id": conversation.id,
                "account_id": self.account.id,
                "direction": "inbound", "number": conversation.number,
                "body": body, "state": "received",
            })
        subject = self.env["whatsapp.issue"]._subject_of(conversation)
        self.assertIn("scanner", subject.lower(),
                      "the opening 'hi' describes nothing and must be skipped")

    def test_classification_happens_when_the_ticket_is_created(self):
        conversation = self._chat("+971570000040", "Wi-Fi keeps dropping")
        with patch(SEND_PATH, fake_send):
            conversation._ensure_ticket()
        self.assertTrue(conversation.issue_id)

    def test_cron_catches_up(self):
        self._chat("+971570000050", "The payment link expired")
        self._chat("+971570000051", "payment link has expired again")
        classified = self.env["whatsapp.issue"]._cron_classify()
        self.assertGreaterEqual(classified, 2)
        self.assertFalse(self.env["whatsapp.conversation"].search_count([
            ("number", "in", ["+971570000050", "+971570000051"]),
            ("issue_id", "=", False),
        ]))

    def test_merging_two_issues(self):
        first = self._chat("+971570000060", "Cannot log in to the portal")
        second = self._chat("+971570000061", "Password reset never arrives")
        Issue = self.env["whatsapp.issue"]
        Issue._match_or_create(first)
        Issue._match_or_create(second)
        self.assertNotEqual(first.issue_id, second.issue_id)

        pair = first.issue_id | second.issue_id
        pair.action_merge()
        first.invalidate_recordset()
        second.invalidate_recordset()
        self.assertEqual(first.issue_id, second.issue_id)
        self.assertEqual(first.issue_id.occurrence_count, 2)

    # ==================================================================
    # The report
    # ==================================================================
    def test_issue_report_separates_repeated_from_one_off(self):
        for number, subject in (
            ("+971570000070", "The office printer is jammed"),
            ("+971570000071", "office printer jammed"),
            ("+971570000072", "printer is jammed in the office"),
            ("+971570000073", "Wrong item delivered to my address"),
        ):
            self.env["whatsapp.issue"]._match_or_create(self._chat(number, subject))

        report = self.env["whatsapp.dashboard"].get_reports(
            {"period": "30d", "team_id": self.team.id}
        )
        issues = report["issues"]
        self.assertGreaterEqual(issues["repeated_count"], 1)
        self.assertGreaterEqual(issues["unique_count"], 1)
        self.assertEqual(issues["distinct_count"],
                         issues["repeated_count"] + issues["unique_count"])
        top = issues["rows"][0]
        self.assertEqual(top["count"], 3)
        self.assertEqual(top["kind"], "repeated")

    def test_issue_report_counts_customers_who_came_back(self):
        conversation = self._chat("+971570000080", "Order has not arrived")
        self.env["whatsapp.issue"]._match_or_create(conversation)
        report = self.env["whatsapp.dashboard"].get_reports(
            {"period": "30d", "team_id": self.team.id}
        )
        self.assertGreaterEqual(report["issues"]["contacts"], 1)
        self.assertIn("repeat_contacts", report["issues"])

    def test_a_chat_created_right_now_is_inside_the_period(self):
        """fields.Datetime.now() drops microseconds.

        A record written in the current second therefore has a create_date
        *after* a truncated "now", and silently fell outside its own reporting
        window.
        """
        conversation = self._chat("+971570000095", "Brand new problem today")
        self.env["whatsapp.issue"]._match_or_create(conversation)
        report = self.env["whatsapp.dashboard"].get_reports(
            {"period": "30d", "team_id": self.team.id}
        )
        self.assertEqual(report["issues"]["distinct_count"], 1)

    def test_issue_export(self):
        self.env["whatsapp.issue"]._match_or_create(
            self._chat("+971570000090", "Screen flickers after the update")
        )
        result = self.env["whatsapp.dashboard"].export_report(
            "issues", {"period": "30d", "team_id": self.team.id}
        )
        self.assertTrue(result["name"].endswith(".csv"))
        self.assertGreaterEqual(result["rows"], 1)

    # ==================================================================
    # The transcript on the ticket
    # ==================================================================
    def _conversation_with_history(self, number="+971570000100"):
        conversation = self._chat(number, "The office printer is jammed")
        for direction, body, is_bot in (
            ("inbound", "Hi", False),
            ("outbound", "Hello! What can we help with?", True),
            ("inbound", "The office printer is jammed", False),
        ):
            self.env["whatsapp.message"].create({
                "conversation_id": conversation.id,
                "account_id": self.account.id,
                "direction": direction, "number": conversation.number,
                "body": body, "is_bot": is_bot,
                "state": "received" if direction == "inbound" else "delivered",
            })
        return conversation

    def test_ticket_opens_with_the_conversation_already_in_it(self):
        conversation = self._conversation_with_history()
        with patch(SEND_PATH, fake_send):
            ticket = conversation._ensure_ticket()
        logged = [m.body for m in ticket.message_ids if "WhatsApp" in (m.body or "")]
        self.assertEqual(len(logged), 3, "the whole exchange should be replayed")
        self.assertTrue(any("printer is jammed" in b for b in logged))
        self.assertTrue(ticket.whatsapp_transcript_synced)

    def test_mirroring_never_duplicates(self):
        conversation = self._conversation_with_history("+971570000101")
        with patch(SEND_PATH, fake_send):
            ticket = conversation._ensure_ticket()
        before = len(ticket.message_ids)
        conversation.whatsapp_message_ids._post_to_ticket()
        ticket.action_sync_whatsapp_transcript()
        self.assertEqual(len(ticket.message_ids), before,
                         "re-syncing must not double the history")

    def test_sync_button_backfills_messages_written_directly(self):
        conversation = self._conversation_with_history("+971570000102")
        ticket = self.env["helpdesk.ticket"].create({
            "name": "Manual", "team_id": self.team.id,
        })
        # Linked after the fact, the way an import or a fixup would do it.
        conversation.ticket_id = ticket
        ticket.whatsapp_conversation_id = conversation
        self.assertFalse(ticket.whatsapp_transcript_synced)

        ticket.action_sync_whatsapp_transcript()
        ticket.invalidate_recordset()
        self.assertTrue(ticket.whatsapp_transcript_synced)
        self.assertTrue(any("printer is jammed" in (m.body or "")
                            for m in ticket.message_ids))

    def test_log_mode_human_skips_the_bot(self):
        self.team.whatsapp_log_mode = "human"
        conversation = self._conversation_with_history("+971570000103")
        with patch(SEND_PATH, fake_send):
            ticket = conversation._ensure_ticket()
        logged = [m.body for m in ticket.message_ids if "WhatsApp" in (m.body or "")]
        self.assertEqual(len(logged), 2)
        self.assertFalse(any("What can we help with" in b for b in logged))

    def test_log_mode_none_keeps_the_chatter_clean(self):
        self.team.whatsapp_log_mode = "none"
        conversation = self._conversation_with_history("+971570000104")
        with patch(SEND_PATH, fake_send):
            ticket = conversation._ensure_ticket()
        self.assertFalse([m for m in ticket.message_ids
                          if "WhatsApp" in (m.body or "")])
        self.assertEqual(len(conversation.whatsapp_message_ids), 3,
                         "the transcript itself is always kept on the ticket")

    def test_line_breaks_survive_into_the_chatter(self):
        """A bot menu is multi-line; the chatter must show it as lines.

        escape() returns Markup, and Markup.replace() escapes its arguments,
        so the naive version printed a literal "<br/>" to the reader.
        """
        conversation = self._chat("+971570000106", "Menu test")
        self.env["whatsapp.message"].create({
            "conversation_id": conversation.id,
            "account_id": self.account.id,
            "direction": "outbound", "number": conversation.number,
            "body": "Pick one:\n1. Technical\n2. Billing",
            "is_bot": True, "state": "delivered",
        })
        with patch(SEND_PATH, fake_send):
            ticket = conversation._ensure_ticket()
        bodies = "".join(m.body or "" for m in ticket.message_ids)
        # Odoo's sanitiser rewrites <br/> as <br>, so assert the meaning.
        self.assertIn("<br", bodies)
        self.assertNotIn("&lt;br", bodies, "the tag must not reach the reader")

    def test_the_chatter_credits_who_said_it(self):
        conversation = self._conversation_with_history("+971570000107")
        agent = self.env["res.users"].create({
            "name": "Cara Agent", "login": "cara_i", "email": "cara@example.com",
        })
        self.env["whatsapp.message"].create({
            "conversation_id": conversation.id,
            "account_id": self.account.id,
            "direction": "outbound", "number": conversation.number,
            "body": "On my way up.", "author_id": agent.id, "state": "delivered",
        })
        with patch(SEND_PATH, fake_send):
            ticket = conversation._ensure_ticket()
        authors = {m.author_id.name for m in ticket.message_ids
                   if "WhatsApp" in (m.body or "")}
        self.assertIn("Cara Agent", authors)
        self.assertIn(conversation.partner_id.name, authors)

    def test_message_bodies_are_escaped_not_rendered(self):
        conversation = self._chat("+971570000105", "Script test")
        self.env["whatsapp.message"].create({
            "conversation_id": conversation.id,
            "account_id": self.account.id,
            "direction": "inbound", "number": conversation.number,
            "body": "<script>alert(1)</script>", "state": "received",
        })
        with patch(SEND_PATH, fake_send):
            ticket = conversation._ensure_ticket()
        bodies = "".join(m.body or "" for m in ticket.message_ids)
        self.assertNotIn("<script>", bodies,
                         "a customer's message must never become live markup")
        self.assertIn("&lt;script&gt;", bodies)


def _json(value):
    import json
    return json.dumps(value)
