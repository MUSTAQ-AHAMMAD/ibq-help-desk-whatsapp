# -*- coding: utf-8 -*-
import itertools
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
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
class TestWhatsappDashboard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.dashboard = cls.env["whatsapp.dashboard"]
        cls.team = cls.env["helpdesk.team"].create({"name": "Dashboard Team"})

        cls.flow = cls.env["whatsapp.bot.flow"].create({
            "name": "Dashboard flow", "agent_keywords": "agent",
            "close_keywords": "stop", "restart_keywords": "menu",
        })
        cls.step_menu = cls.env["whatsapp.bot.step"].create({
            "flow_id": cls.flow.id, "name": "Menu", "step_type": "menu",
            "body": "Pick one:",
        })
        cls.flow.start_step_id = cls.step_menu
        cls.env["whatsapp.bot.option"].create({
            "step_id": cls.step_menu.id, "key": "1", "name": "Support",
        })

        cls.account = cls.env["whatsapp.account"].create({
            "name": "Dashboard sender",
            "account_sid": "AC" + "1" * 32,
            "auth_token": "token",
            "phone_number": "+14155238887",
            "team_id": cls.team.id,
            "bot_flow_id": cls.flow.id,
            "verify_signature": False,
        })

        cls.manager_group = cls.env.ref("ibq_whatsapp_helpdesk.group_whatsapp_manager")
        cls.user_group = cls.env.ref("ibq_whatsapp_helpdesk.group_whatsapp_user")

        cls.alice = cls._make_user("alice", "Alice Agent")
        cls.bob = cls._make_user("bob", "Bob Agent")

        # Explicit roles: leaving them out would make the first agent created
        # the Owner, which is right for a fresh install and wrong for a test.
        cls.agent_alice = cls.env["whatsapp.agent"].create({
            "user_id": cls.alice.id, "role": "agent",
            "status": "available", "max_active_chats": 3,
        })
        cls.agent_bob = cls.env["whatsapp.agent"].create({
            "user_id": cls.bob.id, "role": "agent",
            "status": "available", "max_active_chats": 3,
        })

    @classmethod
    def _make_user(cls, login, name):
        return cls.env["res.users"].create({
            "name": name, "login": login, "email": "%s@example.com" % login,
            "groups_id": [(4, cls.user_group.id)],
        })

    def _inbound(self, body, from_number="+971500000001", **extra):
        params = {
            "From": "whatsapp:%s" % from_number,
            "To": "whatsapp:+14155238887",
            "Body": body,
            "AccountSid": self.account.account_sid,
            "MessageSid": "SM%s" % abs(hash((from_number, body, len(extra)))),
            "ProfileName": "Dashboard Customer",
            "NumMedia": "0",
        }
        params.update(extra)
        with patch(SEND_PATH, fake_send):
            return self.account._process_inbound_payload(params)

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    def test_routing_picks_the_least_loaded_agent(self):
        conversation = self._inbound("hi").conversation_id
        # Give Alice two open chats so Bob is the lighter option.
        for number in ("+971500000010", "+971500000011"):
            other = self.env["whatsapp.conversation"]._get_or_create(
                self.account, number
            )
            other.write({"state": "agent", "user_id": self.alice.id})
        self.agent_alice.invalidate_recordset()

        picked = self.env["whatsapp.agent"]._route(conversation)
        self.assertEqual(picked, self.agent_bob)

    def test_routing_skips_unavailable_and_full_agents(self):
        conversation = self._inbound("hi").conversation_id
        self.agent_bob.status = "away"
        self.agent_alice.max_active_chats = 1
        self.env["whatsapp.conversation"]._get_or_create(
            self.account, "+971500000020"
        ).write({"state": "agent", "user_id": self.alice.id})
        self.agent_alice.invalidate_recordset()

        self.assertFalse(
            self.env["whatsapp.agent"]._route(conversation),
            "nobody is both available and under capacity",
        )

    def test_routing_respects_auto_assign_opt_out(self):
        conversation = self._inbound("hi").conversation_id
        self.agent_alice.auto_assign = False
        self.assertEqual(
            self.env["whatsapp.agent"]._route(conversation), self.agent_bob
        )

    def test_routing_respects_team_coverage(self):
        other_team = self.env["helpdesk.team"].create({"name": "Other"})
        self.agent_alice.team_ids = [(6, 0, [other_team.id])]
        self.agent_bob.team_ids = [(6, 0, [self.team.id])]
        conversation = self._inbound("hi").conversation_id
        self.assertEqual(
            self.env["whatsapp.agent"]._route(conversation), self.agent_bob
        )

    def test_handoff_assigns_an_agent_and_stamps_the_clock(self):
        self._inbound("hi")
        self._inbound("agent")
        conversation = self.env["whatsapp.conversation"].search(
            [("number", "=", "+971500000001")], limit=1
        )
        self.assertEqual(conversation.state, "agent")
        self.assertTrue(conversation.handoff_date)
        self.assertIn(conversation.user_id, self.alice | self.bob)

    # ------------------------------------------------------------------
    # Service metrics
    # ------------------------------------------------------------------
    def test_first_response_is_measured_from_the_handoff(self):
        self._inbound("hi")
        self._inbound("agent")
        conversation = self.env["whatsapp.conversation"].search(
            [("number", "=", "+971500000001")], limit=1
        )
        self.assertEqual(conversation.first_response_seconds, 0)

        with patch(SEND_PATH, fake_send):
            conversation.send_text("Hello, how can I help?")
        self.assertTrue(conversation.first_agent_reply_date)
        self.assertGreaterEqual(conversation.first_response_seconds, 0)
        self.assertFalse(conversation.needs_reply)

        first = conversation.first_agent_reply_date
        with patch(SEND_PATH, fake_send):
            conversation.send_text("Still there?")
        self.assertEqual(conversation.first_agent_reply_date, first,
                         "only the first reply moves the clock")

    def test_bot_only_chat_is_flagged_as_bot_resolved(self):
        self._inbound("hi")
        self._inbound("stop")
        conversation = self.env["whatsapp.conversation"].search(
            [("number", "=", "+971500000001")], limit=1
        )
        self.assertEqual(conversation.state, "closed")
        self.assertTrue(conversation.bot_resolved)
        self.assertTrue(conversation.resolution_seconds >= 0)

    def test_agent_chat_is_not_flagged_as_bot_resolved(self):
        self._inbound("hi")
        self._inbound("agent")
        conversation = self.env["whatsapp.conversation"].search(
            [("number", "=", "+971500000001")], limit=1
        )
        with patch(SEND_PATH, fake_send):
            conversation._close(notify=False)
        self.assertFalse(conversation.bot_resolved)

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------
    def test_dashboard_data_on_an_empty_database(self):
        data = self.env["whatsapp.dashboard"].get_dashboard_data({"period": "7d"})
        self.assertEqual(data["kpi"]["conversations"]["value"], 0)
        self.assertEqual(data["kpi"]["bot_rate"]["value"], 0)
        self.assertTrue(data["series"]["volume"], "the day axis is always drawn")
        # The roster travels in get_bootstrap, not in every stats refresh.
        self.assertEqual(len(self.env["whatsapp.dashboard"].get_bootstrap()["agents"]), 2)

    def test_dashboard_counts_traffic(self):
        self._inbound("hi")
        self._inbound("agent")
        data = self.env["whatsapp.dashboard"].get_dashboard_data({"period": "7d"})
        self.assertEqual(data["kpi"]["conversations"]["value"], 1)
        self.assertEqual(data["kpi"]["inbound"]["value"], 2)
        self.assertGreater(data["kpi"]["outbound"]["value"], 0)
        self.assertEqual(data["live"]["waiting"], 1)
        self.assertEqual(data["live"]["agents_online"], 2)
        self.assertEqual(
            sum(s["value"] for s in data["series"]["mix"]), 1,
            "every conversation lands in exactly one bucket",
        )

    def test_volume_series_is_capped_for_long_periods(self):
        data = self.env["whatsapp.dashboard"].get_dashboard_data({"period": "90d"})
        self.assertLessEqual(len(data["series"]["volume"]), 31)

    # ------------------------------------------------------------------
    # Queue and chat console
    # ------------------------------------------------------------------
    def test_queue_scopes(self):
        self._inbound("hi")
        result = self.env["whatsapp.dashboard"].get_conversations("bot")
        self.assertEqual(len(result["conversations"]), 1)
        self.assertEqual(result["counts"]["bot"], 1)
        self.assertEqual(result["counts"]["waiting"], 0)

        self._inbound("agent")
        result = self.env["whatsapp.dashboard"].get_conversations("waiting")
        self.assertEqual(len(result["conversations"]), 1)
        self.assertTrue(result["conversations"][0]["needs_reply"])

    def test_queue_search_matches_the_number(self):
        self._inbound("hi")
        hit = self.env["whatsapp.dashboard"].get_conversations("all", "500000001")
        self.assertEqual(len(hit["conversations"]), 1)
        miss = self.env["whatsapp.dashboard"].get_conversations("all", "999999")
        self.assertFalse(miss["conversations"])

    def test_sending_from_the_dashboard_takes_a_bot_chat_over(self):
        conversation = self._inbound("hi").conversation_id
        self.assertEqual(conversation.state, "bot")

        with patch(SEND_PATH, fake_send):
            payload = self.env["whatsapp.dashboard"].send_message(
                conversation.id, "Let me help you with that."
            )
        conversation.invalidate_recordset()
        self.assertEqual(conversation.state, "agent")
        self.assertEqual(conversation.user_id, self.env.user)
        self.assertEqual(payload["messages"][-1]["body"], "Let me help you with that.")
        self.assertFalse(payload["needs_reply"])

    def test_sending_an_empty_message_is_refused(self):
        conversation = self._inbound("hi").conversation_id
        with self.assertRaises(UserError):
            self.env["whatsapp.dashboard"].send_message(conversation.id, "   ")

    def test_sending_to_a_closed_chat_is_refused(self):
        conversation = self._inbound("hi").conversation_id
        with patch(SEND_PATH, fake_send):
            conversation._close(notify=False)
        with self.assertRaises(UserError):
            self.env["whatsapp.dashboard"].send_message(conversation.id, "hello?")

    def test_conversation_actions(self):
        conversation = self._inbound("hi").conversation_id
        with patch(SEND_PATH, fake_send):
            self.env["whatsapp.dashboard"].act_on_conversation(
                conversation.id, "take_over"
            )
        conversation.invalidate_recordset()
        self.assertEqual(conversation.state, "agent")
        self.assertEqual(conversation.user_id, self.env.user)

        self.env["whatsapp.dashboard"].assign_conversation(conversation.id, self.bob.id)
        conversation.invalidate_recordset()
        self.assertEqual(conversation.user_id, self.bob)

        with patch(SEND_PATH, fake_send):
            self.env["whatsapp.dashboard"].act_on_conversation(conversation.id, "close")
        conversation.invalidate_recordset()
        self.assertEqual(conversation.state, "closed")

    def test_unknown_action_is_refused(self):
        conversation = self._inbound("hi").conversation_id
        with self.assertRaises(UserError):
            self.env["whatsapp.dashboard"].act_on_conversation(conversation.id, "nuke")

    # ------------------------------------------------------------------
    # Roster management
    # ------------------------------------------------------------------
    def test_candidates_exclude_existing_agents(self):
        candidates = self.env["whatsapp.dashboard"].get_candidate_users()
        ids = [c["id"] for c in candidates]
        self.assertNotIn(self.alice.id, ids)
        self.assertNotIn(self.bob.id, ids)

    def test_manager_can_add_and_remove_agents(self):
        newcomer = self._make_user("carol", "Carol Agent")
        agents = self.env["whatsapp.dashboard"].add_agents([newcomer.id], [self.team.id])
        self.assertEqual(len(agents), 3)

        added = self.env["whatsapp.agent"].search([("user_id", "=", newcomer.id)])
        self.assertEqual(added.team_ids, self.team)

        agents = self.env["whatsapp.dashboard"].remove_agent(added.id)
        self.assertEqual(len(agents), 2)

    def test_adding_the_same_user_twice_is_a_no_op(self):
        agents = self.env["whatsapp.dashboard"].add_agents([self.alice.id])
        self.assertEqual(len(agents), 2)

    def test_non_manager_cannot_change_the_roster(self):
        dashboard = self.env["whatsapp.dashboard"].with_user(self.alice)
        with self.assertRaises(AccessError):
            dashboard.add_agents([self.env.user.id])
        with self.assertRaises(AccessError):
            dashboard.remove_agent(self.agent_bob.id)
        with self.assertRaises(AccessError):
            dashboard.update_agent(self.agent_bob.id, {"max_active_chats": 99})

    def test_an_agent_may_change_their_own_presence(self):
        dashboard = self.env["whatsapp.dashboard"].with_user(self.alice)
        dashboard.update_agent(self.agent_alice.id, {"status": "busy"})
        self.assertEqual(self.agent_alice.status, "busy")

        # ...but not somebody else's, and not their own capacity.
        with self.assertRaises(AccessError):
            dashboard.update_agent(self.agent_bob.id, {"status": "offline"})
        with self.assertRaises(AccessError):
            dashboard.update_agent(self.agent_alice.id, {"max_active_chats": 99})

    def test_set_my_status_requires_a_roster_entry(self):
        outsider = self._make_user("dave", "Dave Nobody")
        with self.assertRaises(UserError):
            self.env["whatsapp.dashboard"].with_user(outsider).set_my_status("available")

        payload = self.env["whatsapp.dashboard"].with_user(self.alice).set_my_status(
            "available"
        )
        self.assertEqual(payload["status"], "available")
        self.assertTrue(payload["is_me"])

    def test_removing_an_agent_with_open_chats_is_refused(self):
        self.env["whatsapp.conversation"]._get_or_create(
            self.account, "+971500000030"
        ).write({"state": "agent", "user_id": self.alice.id})
        with self.assertRaises(UserError):
            self.env["whatsapp.dashboard"].remove_agent(self.agent_alice.id)

    def test_workload_reflects_open_chats(self):
        for number in ("+971500000040", "+971500000041"):
            self.env["whatsapp.conversation"]._get_or_create(
                self.account, number
            ).write({"state": "agent", "user_id": self.alice.id, "needs_reply": True})
        self.agent_alice.invalidate_recordset()
        self.assertEqual(self.agent_alice.active_chat_count, 2)
        self.assertEqual(self.agent_alice.waiting_chat_count, 2)
        self.assertEqual(self.agent_alice.load_percent, 67)
