# -*- coding: utf-8 -*-
"""Roles, permissions, and the tawk.to-style agent tooling.

The point of most of these tests is the boundary: not that a button is hidden,
but that the server refuses the call when someone gets past the button.
"""
import itertools
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged

SEND_PATH = (
    "odoo.addons.ibq_whatsapp_helpdesk.models.whatsapp_account."
    "WhatsappAccount._send_raw"
)


_sid_counter = itertools.count(1)


def fake_send(self, to_number, **kwargs):
    """Unique sid per call: whatsapp.message puts a unique index on it."""
    return {"sid": "SM%032d" % next(_sid_counter), "status": "queued",
            "_ok": True, "_http_status": 201}


@tagged("post_install", "-at_install")
class TestWhatsappRoles(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.dashboard = cls.env["whatsapp.dashboard"]
        cls.sales = cls.env["helpdesk.team"].create({"name": "Sales"})
        cls.tech = cls.env["helpdesk.team"].create({"name": "Tech"})

        cls.account = cls.env["whatsapp.account"].create({
            "name": "Roles sender",
            "account_sid": "AC" + "2" * 32,
            "auth_token": "token",
            "phone_number": "+14155238888",
            "team_id": cls.tech.id,
            "verify_signature": False,
        })

        cls.group_user = cls.env.ref("ibq_whatsapp_helpdesk.group_whatsapp_user")
        cls.group_supervisor = cls.env.ref(
            "ibq_whatsapp_helpdesk.group_whatsapp_supervisor"
        )
        cls.group_manager = cls.env.ref("ibq_whatsapp_helpdesk.group_whatsapp_manager")

        cls.owner_user = cls._make_user("owner1", "Olive Owner")
        cls.admin_user = cls._make_user("admin1", "Adam Admin")
        cls.super_user = cls._make_user("super1", "Sue Supervisor")
        cls.agent_user = cls._make_user("agent1", "Alex Agent")
        cls.other_user = cls._make_user("agent2", "Ada Agent")

        Agent = cls.env["whatsapp.agent"]
        cls.owner = Agent.create({"user_id": cls.owner_user.id, "role": "owner"})
        cls.admin = Agent.create({"user_id": cls.admin_user.id, "role": "admin"})
        cls.supervisor = Agent.create({
            "user_id": cls.super_user.id, "role": "supervisor",
            "team_ids": [(6, 0, [cls.tech.id])],
        })
        cls.agent = Agent.create({
            "user_id": cls.agent_user.id, "role": "agent", "status": "available",
        })
        cls.other = Agent.create({"user_id": cls.other_user.id, "role": "agent"})

    @classmethod
    def _make_user(cls, login, name):
        return cls.env["res.users"].create({
            "name": name, "login": login, "email": "%s@example.com" % login,
        })

    def _as(self, user):
        return self.env["whatsapp.dashboard"].with_user(user)

    def _conversation(self, number="+971511000001", **values):
        conversation = self.env["whatsapp.conversation"]._get_or_create(
            self.account, number
        )
        if values:
            conversation.write(values)
        return conversation

    # ==================================================================
    # Role to Odoo group mapping
    # ==================================================================
    def test_role_grants_the_matching_odoo_group(self):
        self.assertTrue(self.owner_user.has_group(
            "ibq_whatsapp_helpdesk.group_whatsapp_manager"))
        self.assertTrue(self.super_user.has_group(
            "ibq_whatsapp_helpdesk.group_whatsapp_supervisor"))
        self.assertFalse(self.super_user.has_group(
            "ibq_whatsapp_helpdesk.group_whatsapp_manager"))
        self.assertTrue(self.agent_user.has_group(
            "ibq_whatsapp_helpdesk.group_whatsapp_user"))
        self.assertFalse(self.agent_user.has_group(
            "ibq_whatsapp_helpdesk.group_whatsapp_supervisor"))

    def test_promotion_and_demotion_move_the_group(self):
        self.agent.role = "supervisor"
        self.assertTrue(self.agent_user.has_group(
            "ibq_whatsapp_helpdesk.group_whatsapp_supervisor"))

        self.agent.role = "agent"
        self.assertFalse(self.agent_user.has_group(
            "ibq_whatsapp_helpdesk.group_whatsapp_supervisor"))
        self.assertTrue(self.agent_user.has_group(
            "ibq_whatsapp_helpdesk.group_whatsapp_user"))

    def test_removing_someone_revokes_their_access(self):
        self.other.unlink()
        self.assertFalse(self.other_user.has_group(
            "ibq_whatsapp_helpdesk.group_whatsapp_user"))

    def test_archiving_revokes_access_too(self):
        self.other.active = False
        self.assertFalse(self.other_user.has_group(
            "ibq_whatsapp_helpdesk.group_whatsapp_user"))

    def test_first_agent_on_an_empty_roster_becomes_the_owner(self):
        self.env["whatsapp.agent"].search([]).unlink()
        user = self._make_user("first1", "First Person")
        agent = self.env["whatsapp.agent"].create({"user_id": user.id})
        self.assertEqual(agent.role, "owner",
                         "a fresh install must have somebody who can configure it")

    def test_ownership_is_singular_and_transfers(self):
        self.admin.role = "owner"
        self.owner.invalidate_recordset()
        self.assertEqual(self.admin.role, "owner")
        self.assertEqual(self.owner.role, "admin",
                         "promoting a new owner steps the previous one down")
        self.assertEqual(
            self.env["whatsapp.agent"].search_count([("role", "=", "owner")]), 1
        )

    def test_two_owners_cannot_be_written_directly(self):
        with self.assertRaises(ValidationError):
            self.env["whatsapp.agent"].create({
                "user_id": self._make_user("dup1", "Dup").id,
                "role": "owner",
            }).flush_recordset()

    # ==================================================================
    # Rights
    # ==================================================================
    def test_rights_by_role(self):
        self.assertIn("manage_roles", self._as(self.owner_user)._rights())
        self.assertIn("delete_data", self._as(self.owner_user)._rights())
        self.assertNotIn("delete_data", self._as(self.admin_user)._rights())
        self.assertIn("manage_roster", self._as(self.admin_user)._rights())
        self.assertNotIn("manage_roster", self._as(self.super_user)._rights())
        self.assertIn("view_all_chats", self._as(self.super_user)._rights())
        self.assertEqual(self._as(self.agent_user)._rights(), set())

    def test_only_the_owner_may_promote_to_administrator(self):
        self._as(self.owner_user).update_agent(self.agent.id, {"role": "admin"})
        self.assertEqual(self.agent.role, "admin")

        # An admin may not touch another admin.
        with self.assertRaises(AccessError):
            self._as(self.admin_user).update_agent(self.agent.id, {"role": "agent"})

    def test_admin_may_manage_supervisors_and_agents(self):
        self._as(self.admin_user).update_agent(self.other.id, {"role": "supervisor"})
        self.assertEqual(self.other.role, "supervisor")

    def test_admin_cannot_demote_the_owner(self):
        with self.assertRaises(AccessError):
            self._as(self.admin_user).update_agent(self.owner.id, {"role": "agent"})

    def test_supervisor_cannot_change_the_roster(self):
        with self.assertRaises(AccessError):
            self._as(self.super_user).add_agents([self.other_user.id])
        with self.assertRaises(AccessError):
            self._as(self.super_user).update_agent(self.agent.id, {"role": "admin"})

    def test_agent_may_change_only_their_own_presence_and_signature(self):
        self._as(self.agent_user).update_agent(
            self.agent.id, {"status": "busy", "signature": "— Alex"}
        )
        self.assertEqual(self.agent.status, "busy")
        self.assertEqual(self.agent.signature, "— Alex")

        with self.assertRaises(AccessError):
            self._as(self.agent_user).update_agent(
                self.agent.id, {"max_active_chats": 99}
            )
        with self.assertRaises(AccessError):
            self._as(self.agent_user).update_agent(self.other.id, {"status": "away"})

    def test_the_owner_cannot_be_removed(self):
        with self.assertRaises(UserError):
            self._as(self.owner_user).remove_agent(self.owner.id)

    # ==================================================================
    # Scoping
    # ==================================================================
    def test_agents_only_see_their_own_and_unassigned_chats(self):
        mine = self._conversation("+971511000010",
                                  state="agent", user_id=self.agent_user.id)
        theirs = self._conversation("+971511000011",
                                    state="agent", user_id=self.other_user.id)
        free = self._conversation("+971511000012", state="agent")

        visible = self._as(self.agent_user).get_conversations("all")
        ids = [c["id"] for c in visible["conversations"]]
        self.assertIn(mine.id, ids)
        self.assertIn(free.id, ids)
        self.assertNotIn(theirs.id, ids)

    def test_agents_cannot_open_someone_elses_chat(self):
        theirs = self._conversation("+971511000020",
                                    state="agent", user_id=self.other_user.id)
        with self.assertRaises(AccessError):
            self._as(self.agent_user).get_conversation(theirs.id)

    def test_supervisors_are_scoped_to_their_departments(self):
        tech_chat = self._conversation("+971511000030",
                                       state="agent", team_id=self.tech.id,
                                       user_id=self.other_user.id)
        sales_chat = self._conversation("+971511000031",
                                        state="agent", team_id=self.sales.id,
                                        user_id=self.other_user.id)
        visible = self._as(self.super_user).get_conversations("all")
        ids = [c["id"] for c in visible["conversations"]]
        self.assertIn(tech_chat.id, ids)
        self.assertNotIn(sales_chat.id, ids,
                         "Sue only supervises Tech")

    def test_admins_see_everything(self):
        sales_chat = self._conversation("+971511000040",
                                        state="agent", team_id=self.sales.id,
                                        user_id=self.other_user.id)
        visible = self._as(self.admin_user).get_conversations("all")
        self.assertIn(sales_chat.id, [c["id"] for c in visible["conversations"]])

    def test_agents_only_report_on_themselves(self):
        self._conversation("+971511000050", state="agent",
                           user_id=self.other_user.id, handoff_date="2026-01-01 09:00:00",
                           first_response_seconds=120)
        data = self._as(self.agent_user).get_dashboard_data({"period": "365d"})
        self.assertEqual(data["kpi"]["conversations"]["value"], 0,
                         "someone else's chat is not the agent's number")

    def test_agent_kpis_exclude_other_peoples_messages(self):
        """The message counters must carry the same scope as the chat list.

        A conversation domain has to be re-pointed at whatsapp.message before
        it can filter messages; getting that wrong silently shows an agent the
        whole team's volume.
        """
        theirs = self._conversation("+971511000055", state="agent",
                                    user_id=self.other_user.id)
        self.env["whatsapp.message"].create({
            "conversation_id": theirs.id, "account_id": self.account.id,
            "direction": "inbound", "number": theirs.number,
            "body": "not yours", "state": "received",
        })
        mine = self._conversation("+971511000056", state="agent",
                                  user_id=self.agent_user.id)
        self.env["whatsapp.message"].create({
            "conversation_id": mine.id, "account_id": self.account.id,
            "direction": "inbound", "number": mine.number,
            "body": "mine", "state": "received",
        })

        data = self._as(self.agent_user).get_dashboard_data({"period": "7d"})
        self.assertEqual(data["kpi"]["inbound"]["value"], 1)

        everything = self._as(self.admin_user).get_dashboard_data({"period": "7d"})
        self.assertEqual(everything["kpi"]["inbound"]["value"], 2)

    def test_agents_cannot_reassign_to_others(self):
        mine = self._conversation("+971511000060",
                                  state="agent", user_id=self.agent_user.id)
        with self.assertRaises(AccessError):
            self._as(self.agent_user).assign_conversation(mine.id, self.other_user.id)
        # ...but may take a chat themselves.
        free = self._conversation("+971511000061", state="agent")
        self._as(self.agent_user).assign_conversation(free.id, self.agent_user.id)
        self.assertEqual(free.user_id, self.agent_user)

    # ==================================================================
    # Canned responses
    # ==================================================================
    def test_canned_shortcut_must_be_well_formed(self):
        with self.assertRaises(ValidationError):
            self.env["whatsapp.canned.response"].create({
                "shortcut": "Not A Shortcut", "name": "x", "body": "y",
            })

    def test_canned_rendering_fills_placeholders(self):
        conversation = self._conversation("+971511000070")
        conversation.write({"answers": '{"order_ref": "SO4471"}'})
        canned = self.env["whatsapp.canned.response"].create({
            "shortcut": "order-status", "name": "Order status",
            "body": "Hi {name}, order {order_ref} is on its way. {unknown}",
        })
        rendered = canned.render(conversation)
        self.assertIn("SO4471", rendered)
        self.assertIn("{unknown}", rendered,
                      "an unknown placeholder is left visible, not blanked")

    def test_private_canned_replies_are_not_shared(self):
        self.env["whatsapp.canned.response"].create({
            "shortcut": "mine", "name": "Mine", "body": "x",
            "owner_id": self.agent_user.id,
        })
        self.env["whatsapp.canned.response"].create({
            "shortcut": "shared", "name": "Shared", "body": "y",
        })
        mine = self._as(self.agent_user).get_canned_responses()
        theirs = self._as(self.other_user).get_canned_responses()
        self.assertEqual({c["shortcut"] for c in mine}, {"mine", "shared"})
        self.assertEqual({c["shortcut"] for c in theirs}, {"shared"})

    def test_agents_cannot_create_shared_replies(self):
        with self.assertRaises(AccessError):
            self._as(self.agent_user).save_canned_response({
                "shortcut": "team", "name": "Team", "body": "x",
            })
        # A private one is fine.
        self._as(self.agent_user).save_canned_response({
            "shortcut": "personal", "name": "Personal", "body": "x",
            "is_private": True,
        })
        self.assertTrue(self.env["whatsapp.canned.response"].search(
            [("shortcut", "=", "personal"), ("owner_id", "=", self.agent_user.id)]
        ))

    def test_supervisors_may_manage_the_shared_library(self):
        self._as(self.super_user).save_canned_response({
            "shortcut": "greeting", "name": "Greeting", "body": "Hello!",
        })
        record = self.env["whatsapp.canned.response"].search(
            [("shortcut", "=", "greeting")]
        )
        self.assertTrue(record)
        self.assertFalse(record.owner_id)

    def test_sending_a_canned_reply_counts_its_use(self):
        conversation = self._conversation("+971511000080")
        conversation.write({"state": "agent", "user_id": self.env.uid,
                            "last_inbound_date": "2999-01-01 00:00:00"})
        canned = self.env["whatsapp.canned.response"].create({
            "shortcut": "thanks", "name": "Thanks", "body": "Thank you!",
        })
        with patch(SEND_PATH, fake_send):
            self.env["whatsapp.dashboard"].send_message(
                conversation.id, "Thank you!", canned_id=canned.id
            )
        self.assertEqual(canned.usage_count, 1)

    # ==================================================================
    # Tags
    # ==================================================================
    def test_agents_cannot_manage_tags_but_can_apply_them(self):
        tag = self.env["whatsapp.tag"].create({"name": "Billing"})
        with self.assertRaises(AccessError):
            self._as(self.agent_user).save_tag({"name": "Sneaky"})

        mine = self._conversation("+971511000090",
                                  state="agent", user_id=self.agent_user.id)
        payload = self._as(self.agent_user).set_conversation_tags(mine.id, [tag.id])
        self.assertEqual([t["name"] for t in payload["tags"]], ["Billing"])

    def test_tag_colour_is_bounded(self):
        with self.assertRaises(ValidationError):
            self.env["whatsapp.tag"].create({"name": "Bad", "color": 99})

    # ==================================================================
    # Satisfaction
    # ==================================================================
    def test_closing_an_agent_chat_asks_for_a_rating(self):
        conversation = self._conversation("+971511000100")
        conversation.write({
            "state": "agent", "handoff_date": "2026-01-01 09:00:00",
            "last_inbound_date": "2999-01-01 00:00:00",
        })
        with patch(SEND_PATH, fake_send):
            conversation._close(notify=False)
        self.assertTrue(conversation.awaiting_rating)
        self.assertTrue(any(
            "1 (poor)" in (m.body or "")
            for m in conversation.whatsapp_message_ids
        ))

    def test_a_bot_only_chat_is_not_asked_for_a_rating(self):
        conversation = self._conversation("+971511000101")
        conversation.write({"last_inbound_date": "2999-01-01 00:00:00"})
        with patch(SEND_PATH, fake_send):
            conversation._close(notify=False)
        self.assertFalse(conversation.awaiting_rating)

    def test_a_numeric_reply_becomes_the_rating(self):
        conversation = self._conversation("+971511000102")
        conversation.write({
            "state": "closed", "awaiting_rating": True,
            "user_id": self.agent_user.id, "team_id": self.tech.id,
            "last_inbound_date": "2999-01-01 00:00:00",
        })
        message = self.env["whatsapp.message"].create({
            "conversation_id": conversation.id, "account_id": self.account.id,
            "direction": "inbound", "number": conversation.number,
            "body": "5", "state": "received",
        })
        with patch(SEND_PATH, fake_send):
            conversation._handle_inbound(message)
        self.assertTrue(conversation.rating_id)
        self.assertEqual(conversation.rating_id.score_value, 5)
        self.assertEqual(conversation.rating_id.sentiment, "happy")
        self.assertEqual(conversation.state, "closed",
                         "answering the survey must not reopen the chat")
        self.assertFalse(conversation.awaiting_rating)

    def test_a_non_numeric_reply_reopens_instead_of_rating(self):
        conversation = self._conversation("+971511000103")
        conversation.write({
            "state": "closed", "awaiting_rating": True,
            "last_inbound_date": "2999-01-01 00:00:00",
        })
        message = self.env["whatsapp.message"].create({
            "conversation_id": conversation.id, "account_id": self.account.id,
            "direction": "inbound", "number": conversation.number,
            "body": "actually it is still broken", "state": "received",
        })
        with patch(SEND_PATH, fake_send):
            conversation._handle_inbound(message)
        self.assertFalse(conversation.rating_id)
        self.assertNotEqual(conversation.state, "closed")

    def test_sentiment_bands(self):
        bands = {}
        for score in ("1", "2", "3", "4", "5"):
            conversation = self._conversation("+97151100011%s" % score)
            rating = self.env["whatsapp.rating"].create({
                "conversation_id": conversation.id, "score": score,
            })
            bands[score] = rating.sentiment
        self.assertEqual(bands["1"], "unhappy")
        self.assertEqual(bands["3"], "neutral")
        self.assertEqual(bands["5"], "happy")

    # ==================================================================
    # Blocklist
    # ==================================================================
    def test_blocked_numbers_never_reach_a_conversation(self):
        self.env["whatsapp.blocklist"]._block("+971511000200", "spam")
        before = self.env["whatsapp.conversation"].search_count([])
        result = self.account._process_inbound_payload({
            "From": "whatsapp:+971511000200",
            "To": "whatsapp:+14155238888",
            "Body": "buy my thing",
            "MessageSid": "SMblocked1",
            "NumMedia": "0",
        })
        self.assertFalse(result)
        self.assertEqual(self.env["whatsapp.conversation"].search_count([]), before)
        entry = self.env["whatsapp.blocklist"]._entry_for("+971511000200")
        self.assertEqual(entry.hit_count, 1)

    def test_blocking_normalises_the_number(self):
        entry = self.env["whatsapp.blocklist"]._block("whatsapp:+971 51 100 0300")
        self.assertEqual(entry.number, "+971511000300")

    def test_agents_cannot_block(self):
        with self.assertRaises(AccessError):
            self._as(self.agent_user).block_number("+971511000400")

    def test_supervisors_can_block_and_unblock(self):
        entries = self._as(self.super_user).block_number("+971511000401", "abuse")
        self.assertTrue(any(e["number"] == "+971511000401" for e in entries))
        entry_id = next(e["id"] for e in entries if e["number"] == "+971511000401")
        remaining = self._as(self.super_user).unblock_number(entry_id)
        self.assertFalse(any(e["id"] == entry_id for e in remaining))

    def test_blocking_closes_an_open_chat(self):
        conversation = self._conversation("+971511000402", state="agent")
        self._as(self.admin_user).block_number("+971511000402")
        conversation.invalidate_recordset()
        self.assertEqual(conversation.state, "closed")

    # ==================================================================
    # Notes and transfer
    # ==================================================================
    def test_notes_stay_internal(self):
        conversation = self._conversation("+971511000500",
                                          state="agent", user_id=self.agent_user.id)
        payload = self._as(self.agent_user).add_note(conversation.id, "Called them back")
        self.assertTrue(any("Called them back" in n["body"] for n in payload["notes"]))
        self.assertFalse(conversation.whatsapp_message_ids,
                         "a note must never become a WhatsApp message")

    def test_empty_notes_are_refused(self):
        conversation = self._conversation("+971511000501",
                                          state="agent", user_id=self.agent_user.id)
        with self.assertRaises(UserError):
            self._as(self.agent_user).add_note(conversation.id, "   ")

    def test_transfer_moves_the_chat_and_leaves_a_trail(self):
        conversation = self._conversation("+971511000502",
                                          state="agent", user_id=self.other_user.id,
                                          team_id=self.sales.id)
        self._as(self.admin_user).transfer_conversation(
            conversation.id, user_id=self.agent_user.id, team_id=self.tech.id
        )
        conversation.invalidate_recordset()
        self.assertEqual(conversation.user_id, self.agent_user)
        self.assertEqual(conversation.team_id, self.tech)
        self.assertTrue(any(
            "Transferred to" in (m.body or "") for m in conversation.message_ids
        ))

    def test_agents_cannot_transfer(self):
        conversation = self._conversation("+971511000503",
                                          state="agent", user_id=self.agent_user.id)
        with self.assertRaises(AccessError):
            self._as(self.agent_user).transfer_conversation(
                conversation.id, user_id=self.other_user.id
            )

    # ==================================================================
    # Reports and export
    # ==================================================================
    def test_reports_shape(self):
        reports = self._as(self.admin_user).get_reports({"period": "30d"})
        for key in ("leaderboard", "heatmap", "tags", "departments",
                    "csat", "response_buckets"):
            self.assertIn(key, reports)
        self.assertEqual(len(reports["heatmap"]["grid"]), 7)
        self.assertEqual(len(reports["heatmap"]["grid"][0]), 24)
        self.assertEqual(len(reports["csat"]["distribution"]), 5)

    def test_response_buckets_classify_waits(self):
        for index, seconds in enumerate((30, 200, 800, 2000, 7200)):
            conversation = self._conversation("+97151100060%s" % index)
            conversation.write({
                "handoff_date": "2026-08-20 09:00:00",
                "first_response_seconds": seconds,
            })
        reports = self._as(self.admin_user).get_reports({
            "date_from": "2026-08-01 00:00:00", "date_to": "2026-09-01 00:00:00",
        })
        rows = {r["label"]: r["value"] for r in reports["response_buckets"]["rows"]}
        self.assertEqual(rows["under 1 min"], 1)
        self.assertEqual(rows["1-5 min"], 1)
        self.assertEqual(rows["5-15 min"], 1)
        self.assertEqual(rows["15-60 min"], 1)
        self.assertEqual(rows["over 1 hour"], 1)

    def test_export_produces_a_downloadable_attachment(self):
        result = self._as(self.admin_user).export_report(
            "conversations", {"period": "30d"}
        )
        self.assertTrue(result["url"].startswith("/web/content/"))
        self.assertTrue(result["name"].endswith(".csv"))
        attachment_id = int(result["url"].split("/")[-1].split("?")[0])
        attachment = self.env["ir.attachment"].browse(attachment_id)
        self.assertEqual(attachment.mimetype, "text/csv")

    def test_agents_cannot_export(self):
        with self.assertRaises(AccessError):
            self._as(self.agent_user).export_report("leaderboard", {"period": "7d"})

    def test_unknown_export_is_refused(self):
        with self.assertRaises(UserError):
            self._as(self.admin_user).export_report("everything", {"period": "7d"})

    # ==================================================================
    # Bootstrap and monitoring
    # ==================================================================
    def test_bootstrap_reports_the_callers_role(self):
        boot = self._as(self.super_user).get_bootstrap()
        self.assertTrue(boot["on_roster"])
        self.assertEqual(boot["me"]["role"], "supervisor")
        self.assertIn("view_all_chats", boot["rights"])
        self.assertNotIn("manage_roster", boot["rights"])

    def test_bootstrap_for_someone_not_on_the_roster(self):
        outsider = self._make_user("outsider1", "Otto Outsider")
        outsider.groups_id = [(4, self.group_user.id)]
        boot = self._as(outsider).get_bootstrap()
        self.assertFalse(boot["on_roster"])
        self.assertEqual(boot["rights"], [])

    def test_monitoring_board_has_three_columns(self):
        self._conversation("+971511000700", state="agent", needs_reply=True)
        self._conversation("+971511000701", state="agent", needs_reply=False)
        self._conversation("+971511000702", state="bot")
        board = self._as(self.admin_user).get_monitoring()
        keys = [c["key"] for c in board["columns"]]
        self.assertEqual(keys, ["waiting", "active", "bot"])
        self.assertEqual({c["key"]: c["count"] for c in board["columns"]}["waiting"], 1)

    def test_contacts_group_by_number(self):
        conversation = self._conversation("+971511000800")
        contacts = self._as(self.admin_user).get_contacts()
        entry = next(c for c in contacts["contacts"]
                     if c["number"] == conversation.number)
        self.assertEqual(entry["chats"], 1)
        self.assertFalse(entry["blocked"])

    # ==================================================================
    # Invite wizard
    # ==================================================================
    def test_invite_wizard_adds_people(self):
        newcomer = self._make_user("newbie1", "Nina Newbie")
        wizard = self.env["whatsapp.invite.member"].with_user(self.owner_user).create({
            "user_ids": [(6, 0, [newcomer.id])],
            "role": "supervisor",
            "team_ids": [(6, 0, [self.tech.id])],
            "max_active_chats": 7,
            "notify": False,
        })
        wizard.action_add()
        agent = self.env["whatsapp.agent"].search([("user_id", "=", newcomer.id)])
        self.assertEqual(agent.role, "supervisor")
        self.assertEqual(agent.max_active_chats, 7)
        self.assertEqual(agent.team_ids, self.tech)
        self.assertTrue(newcomer.has_group(
            "ibq_whatsapp_helpdesk.group_whatsapp_supervisor"))

    def test_invite_wizard_refuses_duplicates(self):
        wizard = self.env["whatsapp.invite.member"].with_user(self.owner_user).create({
            "user_ids": [(6, 0, [self.agent_user.id])], "notify": False,
        })
        with self.assertRaises(UserError):
            wizard.action_add()

    def test_only_the_owner_may_invite_an_administrator(self):
        newcomer = self._make_user("newbie2", "Nate Newbie")
        wizard = self.env["whatsapp.invite.member"].with_user(self.admin_user).create({
            "user_ids": [(6, 0, [newcomer.id])], "role": "admin", "notify": False,
        })
        with self.assertRaises(AccessError):
            wizard.action_add()
