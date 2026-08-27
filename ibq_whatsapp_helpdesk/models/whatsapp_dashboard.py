# -*- coding: utf-8 -*-
"""RPC surface for the WhatsApp dashboard client action.

Everything the OWL dashboard needs lives here as ``@api.model`` methods, so
the JavaScript never has to know how the statistics are assembled.

Two layers of access apply, and both matter:

* Odoo's own ACLs and record rules, because every read goes through the ORM
  as the logged-in user.
* The WhatsApp role on ``whatsapp.agent``, which decides what the dashboard
  offers and is re-checked here on every method that changes something. The
  UI hiding a button is a convenience, not the control.
"""
import base64
import csv
import io
from datetime import date, datetime, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .whatsapp_agent import ROLE_RIGHTS

PERIODS = {"today": 1, "7d": 7, "30d": 30, "90d": 90, "365d": 365}

RESPONSE_BUCKETS = [
    (60, "under 1 min"),
    (300, "1-5 min"),
    (900, "5-15 min"),
    (3600, "15-60 min"),
    (None, "over 1 hour"),
]

QUEUE_SCOPES = {
    "waiting": [("state", "=", "agent"), ("needs_reply", "=", True)],
    "mine": [("state", "!=", "closed")],          # + user filter, added below
    "unassigned": [("state", "=", "agent"), ("user_id", "=", False)],
    "bot": [("state", "=", "bot")],
    "all": [("state", "!=", "closed")],
    "closed": [("state", "=", "closed")],
}


def _as_date(value):
    """Normalise whatever _read_group hands back for a date bucket."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return fields.Datetime.to_datetime(value)


class WhatsappDashboard(models.TransientModel):
    _name = "whatsapp.dashboard"
    _description = "WhatsApp Dashboard"

    # ==================================================================
    # Identity, rights and scoping
    # ==================================================================
    @api.model
    def _me(self):
        return self.env["whatsapp.agent"]._current()

    @api.model
    def _rights(self):
        """The right names the caller holds, as a set."""
        agent = self._me()
        if agent:
            return set(ROLE_RIGHTS.get(agent.role, ()))
        # A system administrator who never joined the roster still needs a way
        # in, or a fresh install has nobody who can configure anything.
        if self.env.user.has_group("base.group_system"):
            return set(ROLE_RIGHTS["admin"])
        return set()

    @api.model
    def _assert(self, right):
        return self.env["whatsapp.agent"]._assert_right(right)

    @api.model
    def _scope_domain(self):
        """Restrict conversations to what the caller is allowed to see.

        Supervisors are scoped to their own departments when they list any;
        an agent sees their own chats plus whatever is unassigned, so they can
        still pick work off the queue.
        """
        agent = self._me()
        rights = self._rights()
        if "view_all_chats" not in rights:
            return ["|", ("user_id", "=", self.env.uid), ("user_id", "=", False)]
        if agent and agent.role == "supervisor" and agent.team_ids:
            return ["|", ("team_id", "in", agent.team_ids.ids),
                    ("team_id", "=", False)]
        return []

    @api.model
    def _report_domain(self):
        """Same idea for reporting, which agents only see for themselves."""
        if "view_all_reports" not in self._rights():
            return [("user_id", "=", self.env.uid)]
        return self._scope_domain()

    # ==================================================================
    # Filters
    # ==================================================================
    @api.model
    def _period_bounds(self, filters):
        """Return (start, end, previous_start) from a filter dict."""
        filters = filters or {}
        if filters.get("date_from") and filters.get("date_to"):
            start = fields.Datetime.to_datetime(filters["date_from"])
            end = fields.Datetime.to_datetime(filters["date_to"])
        else:
            days = PERIODS.get(filters.get("period", "7d"), 7)
            # A second past now, because fields.Datetime.now() drops
            # microseconds: a record written this very second has a create_date
            # *greater* than a truncated "now" and would fall outside its own
            # period.
            end = fields.Datetime.now() + timedelta(seconds=1)
            start = end - timedelta(days=days)
        span = end - start
        return start, end, start - span

    @api.model
    def _prefix_domain(self, domain, prefix):
        """Re-point a conversation domain at a model that relates to one.

        Operator strings ('|', '&', '!') pass through untouched; only the
        field name of each leaf is prefixed, so a scope like
        ``['|', ('team_id', 'in', ids), ('team_id', '=', False)]`` still means
        the same thing when applied to messages or tickets.
        """
        prefixed = []
        for leaf in domain:
            if isinstance(leaf, (list, tuple)) and len(leaf) == 3:
                prefixed.append((prefix + leaf[0], leaf[1], leaf[2]))
            else:
                prefixed.append(leaf)
        return prefixed

    @api.model
    def _filter_domain(self, filters, prefix=""):
        """Translate the dashboard's filter bar into a domain.

        ``prefix`` lets the same filters apply to messages and ratings, whose
        team/agent fields sit behind a relation.
        """
        filters = filters or {}
        domain = []
        for key, field in (("team_id", "team_id"), ("user_id", "user_id"),
                           ("account_id", "account_id")):
            if filters.get(key):
                domain.append((prefix + field, "=", filters[key]))
        if filters.get("tag_ids"):
            domain.append((prefix + "tag_ids", "in", filters["tag_ids"]))
        return domain

    # ==================================================================
    # Bootstrap
    # ==================================================================
    @api.model
    def get_bootstrap(self):
        """One call at load: identity, rights and every reference list."""
        agent = self._me()
        return {
            "me": agent._dashboard_payload() if agent else {
                "id": False,
                "name": self.env.user.name,
                "role": "admin" if self.env.user.has_group("base.group_system") else "agent",
                "role_label": _("Not on the roster"),
                "status": "offline",
                "avatar": "/web/image/res.users/%s/avatar_128" % self.env.uid,
                "is_me": True,
                "on_roster": False,
            },
            "on_roster": bool(agent),
            "rights": sorted(self._rights()),
            "agents": self.get_agents(),
            "departments": self.get_teams(),
            "tags": self.get_tags(),
            "accounts": [
                {"id": a.id, "name": a.name, "number": a.phone_number or "",
                 "state": a.state}
                for a in self.env["whatsapp.account"].search([])
            ],
            "periods": [
                {"key": "today", "label": _("Today")},
                {"key": "7d", "label": _("7 days")},
                {"key": "30d", "label": _("30 days")},
                {"key": "90d", "label": _("90 days")},
                {"key": "365d", "label": _("12 months")},
            ],
        }

    # ==================================================================
    # Overview
    # ==================================================================
    @api.model
    def get_dashboard_data(self, filters=None):
        start, end, prev_start = self._period_bounds(filters)
        scope = self._report_domain()
        extra = self._filter_domain(filters)
        conversations = self.env["whatsapp.conversation"]
        messages = self.env["whatsapp.message"]

        window = scope + extra + [("create_date", ">=", start), ("create_date", "<", end)]
        prev = scope + extra + [("create_date", ">=", prev_start), ("create_date", "<", start)]
        # Messages and tickets must carry the same role scope as conversations,
        # or an agent's own dashboard would quietly report the whole team's
        # volume.
        msg_scope = self._prefix_domain(scope, "conversation_id.") +             self._filter_domain(filters, prefix="conversation_id.")
        msg_window = msg_scope + [("create_date", ">=", start), ("create_date", "<", end)]
        msg_prev = msg_scope + [("create_date", ">=", prev_start), ("create_date", "<", start)]

        started = conversations.search_count(window)
        started_prev = conversations.search_count(prev)
        inbound = messages.search_count(msg_window + [("direction", "=", "inbound")])
        inbound_prev = messages.search_count(msg_prev + [("direction", "=", "inbound")])
        outbound = messages.search_count(msg_window + [("direction", "=", "outbound")])
        outbound_prev = messages.search_count(msg_prev + [("direction", "=", "outbound")])
        failed = messages.search_count(msg_window + [("state", "=", "failed")])

        ticket_scope = self._prefix_domain(scope, "whatsapp_conversation_id.")
        ticket_window = ticket_scope + [
            ("create_date", ">=", start), ("create_date", "<", end),
            ("whatsapp_conversation_id", "!=", False),
        ]
        # sudo on a bare count: the KPI is a number, and agents without
        # helpdesk rights would otherwise see the whole dashboard fail.
        helpdesk = self.env["helpdesk.ticket"].sudo()
        tickets = helpdesk.search_count(ticket_window)
        tickets_prev = helpdesk.search_count(
            ticket_scope + [
                ("create_date", ">=", prev_start), ("create_date", "<", start),
                ("whatsapp_conversation_id", "!=", False),
            ]
        )

        closed = conversations.search(
            scope + extra + [("closed_date", ">=", start), ("closed_date", "<", end)]
        )
        bot_rate = round(
            len(closed.filtered("bot_resolved")) * 100.0 / len(closed)
        ) if closed else 0

        avg_response, avg_response_prev = (
            self._average_response(scope + extra, start, end),
            self._average_response(scope + extra, prev_start, start),
        )
        resolved = closed.filtered("resolution_seconds")
        avg_resolution = round(
            sum(resolved.mapped("resolution_seconds")) / len(resolved)
        ) if resolved else 0

        csat, csat_count = self._average_csat(filters, start, end)
        csat_prev = self._average_csat(filters, prev_start, start)[0]

        live_scope = self._scope_domain() + extra
        return {
            "generated_at": fields.Datetime.to_string(end.replace(microsecond=0)),
            "kpi": {
                "conversations": {"value": started, "delta": self._delta(started, started_prev)},
                "inbound": {"value": inbound, "delta": self._delta(inbound, inbound_prev)},
                "outbound": {"value": outbound, "delta": self._delta(outbound, outbound_prev)},
                "tickets": {"value": tickets, "delta": self._delta(tickets, tickets_prev)},
                "avg_response": {"value": avg_response,
                                 "delta": self._delta(avg_response, avg_response_prev),
                                 "lower_is_better": True},
                "avg_resolution": {"value": avg_resolution, "delta": None},
                "bot_rate": {"value": bot_rate, "delta": None},
                "csat": {"value": csat, "delta": self._delta(csat, csat_prev),
                         "count": csat_count},
                "failed": {"value": failed, "delta": None},
            },
            "live": self._live_counts(live_scope),
            "series": self._get_series(start, end, scope + extra, msg_scope),
        }

    @api.model
    def _delta(self, current, previous):
        if not previous:
            return None
        return round((current - previous) * 100.0 / previous)

    @api.model
    def _average_response(self, domain, start, end):
        answered = self.env["whatsapp.conversation"].search(
            domain + [("first_response_seconds", ">", 0),
                      ("handoff_date", ">=", start), ("handoff_date", "<", end)]
        )
        if not answered:
            return 0
        return round(sum(answered.mapped("first_response_seconds")) / len(answered))

    @api.model
    def _average_csat(self, filters, start, end):
        domain = [("create_date", ">=", start), ("create_date", "<", end)]
        domain += self._filter_domain(filters)
        if "view_all_reports" not in self._rights():
            domain.append(("user_id", "=", self.env.uid))
        ratings = self.env["whatsapp.rating"].search(domain)
        if not ratings:
            return 0, 0
        average = sum(ratings.mapped("score_value")) / len(ratings)
        return round(average * 20), len(ratings)   # as a percentage of 5 stars

    @api.model
    def _live_counts(self, scope):
        conversations = self.env["whatsapp.conversation"]
        return {
            "waiting": conversations.search_count(
                scope + [("state", "=", "agent"), ("needs_reply", "=", True)]
            ),
            "unassigned": conversations.search_count(
                scope + [("state", "=", "agent"), ("user_id", "=", False)]
            ),
            "with_bot": conversations.search_count(scope + [("state", "=", "bot")]),
            "open": conversations.search_count(scope + [("state", "!=", "closed")]),
            "agents_online": self.env["whatsapp.agent"].search_count(
                [("status", "=", "available")]
            ),
        }

    @api.model
    def _get_series(self, start, end, scope, msg_scope):
        """Daily volume, the current state mix, and the priority split."""
        messages = self.env["whatsapp.message"]
        window = [("create_date", ">=", start), ("create_date", "<", end)]

        def bucket(direction):
            groups = messages._read_group(
                msg_scope + window + [("direction", "=", direction)],
                ["create_date:day"], ["__count"],
            )
            return {_as_date(key).date(): count for key, count in groups}

        inbound_map, outbound_map = bucket("inbound"), bucket("outbound")
        volume, cursor = [], start.date()
        while cursor <= end.date():
            volume.append({
                "date": fields.Date.to_string(cursor),
                "label": cursor.strftime("%d %b"),
                "short": cursor.strftime("%a"),
                "inbound": inbound_map.get(cursor, 0),
                "outbound": outbound_map.get(cursor, 0),
            })
            cursor += timedelta(days=1)
        # A 12-month window would render 365 unreadable bars; keep the tail.
        volume = volume[-31:]

        conversations = self.env["whatsapp.conversation"]
        mix = [
            {"key": "bot", "label": _("With bot"),
             "value": conversations.search_count(scope + [("state", "=", "bot")])},
            {"key": "agent", "label": _("With agent"),
             "value": conversations.search_count(scope + [("state", "=", "agent")])},
            {"key": "closed", "label": _("Closed"),
             "value": conversations.search_count(scope + [("state", "=", "closed")])},
        ]
        return {"volume": volume, "mix": mix}

    # ==================================================================
    # Monitoring board
    # ==================================================================
    @api.model
    def get_monitoring(self):
        """A live board: three columns of chats plus who is on shift."""
        scope = self._scope_domain()
        conversations = self.env["whatsapp.conversation"]
        columns = []
        for key, label, domain, order in (
            ("waiting", _("Waiting"),
             [("state", "=", "agent"), ("needs_reply", "=", True)],
             "priority desc, last_message_date asc"),
            ("active", _("In progress"),
             [("state", "=", "agent"), ("needs_reply", "=", False)],
             "last_message_date desc"),
            ("bot", _("With the bot"), [("state", "=", "bot")],
             "last_message_date desc"),
        ):
            records = conversations.search(scope + domain, limit=25, order=order)
            columns.append({
                "key": key,
                "label": label,
                "count": conversations.search_count(scope + domain),
                "conversations": [c._queue_payload() for c in records],
            })
        return {
            "columns": columns,
            "agents": self.get_agents(),
            "generated_at": fields.Datetime.to_string(fields.Datetime.now()),
        }

    # ==================================================================
    # Queue and chat console
    # ==================================================================
    @api.model
    def get_conversations(self, scope="waiting", search=None, filters=None, limit=60):
        base = self._scope_domain() + self._filter_domain(filters)
        conversations = self.env["whatsapp.conversation"]

        def scope_domain(key):
            domain = list(QUEUE_SCOPES.get(key, QUEUE_SCOPES["waiting"]))
            if key == "mine":
                domain.append(("user_id", "=", self.env.uid))
            return base + domain

        domain = scope_domain(scope)
        if search:
            domain += ["|", "|", "|",
                       ("number", "ilike", search),
                       ("partner_id.name", "ilike", search),
                       ("profile_name", "ilike", search),
                       ("whatsapp_message_ids.body", "ilike", search)]
        records = conversations.search(
            domain, limit=limit,
            order="needs_reply desc, priority desc, last_message_date desc",
        )
        return {
            "scope": scope,
            "conversations": [c._queue_payload() for c in records],
            "counts": {
                key: conversations.search_count(scope_domain(key))
                for key in QUEUE_SCOPES
            },
        }

    @api.model
    def _get_conversation(self, conversation_id):
        """Fetch one conversation, refusing anything outside the caller's scope."""
        conversation = self.env["whatsapp.conversation"].search(
            self._scope_domain() + [("id", "=", conversation_id)], limit=1
        )
        if not conversation:
            raise AccessError(_("That conversation is not yours to open."))
        return conversation

    @api.model
    def get_conversation(self, conversation_id, message_limit=50):
        return self._get_conversation(conversation_id)._chat_payload(message_limit)

    @api.model
    def send_message(self, conversation_id, body, canned_id=None):
        """Send an agent reply from the dashboard.

        Deliberately raises rather than swallowing: an expired session window
        or a Twilio rejection has to reach the person who just hit Send.
        """
        conversation = self._get_conversation(conversation_id)
        if not (body or "").strip():
            raise UserError(_("The message is empty."))
        if conversation.state == "closed":
            raise UserError(_("This conversation is closed. Reopen it first."))
        taking_over = conversation.state == "bot"
        if taking_over:
            conversation._handoff(
                reason=_("Answered directly from the dashboard by %s.")
                       % self.env.user.name,
                announce=False,
            )
        # Whoever answers owns the chat. Routing may have assigned it during
        # the hand-off, but the person who actually typed the reply wins.
        if taking_over or not conversation.user_id:
            conversation.assign_to(self.env.user, source=_("the dashboard"))

        agent = self._me()
        if agent.signature:
            body = "%s\n%s" % (body, agent.signature)
        conversation.send_text(body)
        if canned_id:
            self.env["whatsapp.canned.response"].browse(canned_id)._register_use()
        return conversation._chat_payload()

    @api.model
    def assign_conversation(self, conversation_id, user_id=None):
        conversation = self._get_conversation(conversation_id)
        conversation.assign_to(
            self.env["res.users"].browse(user_id) if user_id else None,
            source=_("the dashboard"),
        )
        return conversation._queue_payload()

    @api.model
    def transfer_conversation(self, conversation_id, user_id=None, team_id=None, note=None):
        self._assert("transfer")
        conversation = self._get_conversation(conversation_id)
        conversation.transfer_to(
            user=self.env["res.users"].browse(user_id) if user_id else None,
            team=self.env["helpdesk.team"].browse(team_id) if team_id else None,
            note=note,
        )
        return conversation._chat_payload()

    @api.model
    def act_on_conversation(self, conversation_id, action):
        conversation = self._get_conversation(conversation_id)
        if action == "take_over":
            if conversation.state == "bot":
                conversation._handoff(
                    reason=_("Taken over from the dashboard by %s.")
                           % self.env.user.name,
                    announce=False,
                )
            conversation.assign_to(self.env.user, source=_("the dashboard"))
        elif action == "close":
            conversation._close(notify=True)
        elif action == "reopen":
            conversation.action_reopen()
        elif action == "mark_handled":
            conversation.needs_reply = False
        elif action == "ask_rating":
            if not conversation._ask_for_rating():
                raise UserError(_(
                    "A rating cannot be requested right now: the chat was never "
                    "handled by an agent, is already rated, or the 24h session "
                    "has expired."
                ))
        else:
            raise UserError(_("Unknown action '%s'.") % action)
        conversation._notify_dashboard(action)
        return conversation._queue_payload()

    @api.model
    def set_conversation_tags(self, conversation_id, tag_ids):
        conversation = self._get_conversation(conversation_id)
        conversation.tag_ids = [(6, 0, tag_ids or [])]
        return conversation._chat_payload()

    @api.model
    def set_conversation_priority(self, conversation_id, priority):
        conversation = self._get_conversation(conversation_id)
        conversation.priority = priority
        return conversation._queue_payload()

    @api.model
    def add_note(self, conversation_id, body):
        conversation = self._get_conversation(conversation_id)
        conversation.add_note(body)
        return conversation._chat_payload()

    # ==================================================================
    # Canned responses
    # ==================================================================
    @api.model
    def get_canned_responses(self, search=None, conversation_id=None):
        conversation = self.env["whatsapp.conversation"]
        if conversation_id:
            conversation = self._get_conversation(conversation_id)
        records = self.env["whatsapp.canned.response"]._search_for_agent(
            search, conversation.team_id
        )
        return [r._payload(conversation) for r in records]

    @api.model
    def save_canned_response(self, values, canned_id=None):
        """Create or update a saved reply.

        Anyone may keep a private one; sharing it with the team needs the
        canned-response right.
        """
        values = dict(values or {})
        is_private = values.get("is_private")
        values.pop("is_private", None)
        values["owner_id"] = self.env.uid if is_private else False
        if not is_private:
            self._assert("manage_canned")
        model = self.env["whatsapp.canned.response"]
        if canned_id:
            record = model.browse(canned_id)
            if record.owner_id and record.owner_id != self.env.user:
                raise AccessError(_("That saved reply belongs to someone else."))
            if not record.owner_id:
                self._assert("manage_canned")
            record.write(values)
        else:
            model.create(values)
        return self.get_canned_responses()

    @api.model
    def delete_canned_response(self, canned_id):
        record = self.env["whatsapp.canned.response"].browse(canned_id)
        if record.owner_id and record.owner_id != self.env.user:
            raise AccessError(_("That saved reply belongs to someone else."))
        if not record.owner_id:
            self._assert("manage_canned")
        record.unlink()
        return self.get_canned_responses()

    # ==================================================================
    # Tags
    # ==================================================================
    @api.model
    def get_tags(self):
        return [t._payload() for t in self.env["whatsapp.tag"].search([])]

    @api.model
    def save_tag(self, values, tag_id=None):
        self._assert("manage_tags")
        model = self.env["whatsapp.tag"]
        if tag_id:
            model.browse(tag_id).write(values)
        else:
            model.create(values)
        return self.get_tags()

    @api.model
    def delete_tag(self, tag_id):
        self._assert("manage_tags")
        self.env["whatsapp.tag"].browse(tag_id).unlink()
        return self.get_tags()

    # ==================================================================
    # Reports
    # ==================================================================
    @api.model
    def get_reports(self, filters=None):
        start, end, _prev = self._period_bounds(filters)
        scope = self._report_domain() + self._filter_domain(filters)
        window = scope + [("create_date", ">=", start), ("create_date", "<", end)]
        return {
            "leaderboard": self._report_leaderboard(scope, start, end),
            "heatmap": self._report_heatmap(filters, start, end),
            "tags": self._report_tags(window),
            "departments": self._report_departments(window),
            "csat": self._report_csat(filters, start, end),
            "issues": self._report_issues(window),
            "response_buckets": self._report_response_buckets(scope, start, end),
            "period": {"from": fields.Datetime.to_string(start),
                       "to": fields.Datetime.to_string(end)},
        }

    @api.model
    def _report_leaderboard(self, scope, start, end):
        """Per-agent throughput and quality over the window."""
        conversations = self.env["whatsapp.conversation"]
        handled = conversations._read_group(
            scope + [("handoff_date", ">=", start), ("handoff_date", "<", end),
                     ("user_id", "!=", False)],
            ["user_id"],
            ["__count", "first_response_seconds:avg", "resolution_seconds:avg"],
        )
        ratings = self.env["whatsapp.rating"]._read_group(
            [("create_date", ">=", start), ("create_date", "<", end),
             ("user_id", "!=", False)],
            ["user_id"], ["__count", "score_value:avg"],
        )
        rating_map = {
            user.id: {"count": count, "avg": average or 0}
            for user, count, average in ratings
        }
        agents = {a.user_id.id: a for a in self.env["whatsapp.agent"].search([])}

        rows = []
        for user, count, avg_response, avg_resolution in handled:
            rating = rating_map.get(user.id, {})
            agent = agents.get(user.id)
            rows.append({
                "user_id": user.id,
                "name": user.name,
                "avatar": "/web/image/res.users/%s/avatar_128" % user.id,
                "role": agent.role if agent else "",
                "status": agent.status if agent else "offline",
                "handled": count,
                "avg_response": round(avg_response or 0),
                "avg_resolution": round(avg_resolution or 0),
                "rating_count": rating.get("count", 0),
                "csat": round((rating.get("avg") or 0) * 20),
                "open_now": agent.active_chat_count if agent else 0,
            })
        rows.sort(key=lambda r: (-r["handled"], r["avg_response"]))
        return rows

    @api.model
    def _report_heatmap(self, filters, start, end):
        """Inbound volume by weekday and hour: when does the queue get busy?"""
        domain = self._prefix_domain(self._report_domain(), "conversation_id.")
        domain += self._filter_domain(filters, prefix="conversation_id.")
        domain += [("direction", "=", "inbound"),
                   ("create_date", ">=", start), ("create_date", "<", end)]
        groups = self.env["whatsapp.message"]._read_group(
            domain, ["create_date:hour"], ["__count"]
        )
        # Python's weekday(): Monday is 0, which is the order we render.
        grid = [[0] * 24 for _day in range(7)]
        peak = 0
        for key, count in groups:
            moment = _as_date(key)
            grid[moment.weekday()][moment.hour] += count
            peak = max(peak, grid[moment.weekday()][moment.hour])
        days = [_("Mon"), _("Tue"), _("Wed"), _("Thu"),
                _("Fri"), _("Sat"), _("Sun")]
        return {
            "days": days,
            "grid": grid,
            "peak": peak,
            "total": sum(sum(row) for row in grid),
        }

    @api.model
    def _report_tags(self, window):
        groups = self.env["whatsapp.conversation"]._read_group(
            window + [("tag_ids", "!=", False)], ["tag_ids"], ["__count"]
        )
        rows = [
            {"id": tag.id, "label": tag.name, "color": tag.color, "value": count}
            for tag, count in groups
        ]
        rows.sort(key=lambda r: -r["value"])
        untagged = self.env["whatsapp.conversation"].search_count(
            window + [("tag_ids", "=", False)]
        )
        return {"rows": rows[:12], "untagged": untagged}

    @api.model
    def _report_departments(self, window):
        groups = self.env["whatsapp.conversation"]._read_group(
            window, ["team_id"], ["__count", "first_response_seconds:avg"]
        )
        return sorted(
            [
                {
                    "id": team.id if team else False,
                    "label": team.name if team else _("No department"),
                    "value": count,
                    "avg_response": round(avg or 0),
                }
                for team, count, avg in groups
            ],
            key=lambda r: -r["value"],
        )

    @api.model
    def _report_issues(self, window):
        """What customers contacted about, and how much of it repeats.

        The headline a support lead wants is not "how many tickets" but "how
        many *different problems*, and which ones keep coming back".
        """
        conversations = self.env["whatsapp.conversation"].search(
            window + [("issue_id", "!=", False)]
        )
        by_issue = {}
        for conversation in conversations:
            entry = by_issue.setdefault(conversation.issue_id, {
                "conversations": self.env["whatsapp.conversation"],
                "numbers": set(),
            })
            entry["conversations"] |= conversation
            entry["numbers"].add(conversation.number)

        rows = []
        for issue, entry in by_issue.items():
            found = entry["conversations"]
            resolved = found.filtered("resolution_seconds")
            rated = found.filtered("rating_score")
            rows.append({
                "id": issue.id,
                "name": issue.name,
                "count": len(found),
                "contacts": len(entry["numbers"]),
                "kind": "repeated" if len(found) > 1 else "unique",
                "avg_resolution": round(
                    sum(resolved.mapped("resolution_seconds")) / len(resolved)
                ) if resolved else 0,
                "rating": round(
                    sum(rated.mapped("rating_score")) / len(rated), 1
                ) if rated else 0,
                "open": len(found.filtered(lambda c: c.state != "closed")),
                "tags": issue.tag_ids.mapped("name")[:3],
                "last_seen": fields.Datetime.to_string(
                    max(found.mapped("create_date"))
                ),
            })
        rows.sort(key=lambda r: (-r["count"], r["name"]))

        repeated = [r for r in rows if r["kind"] == "repeated"]
        unique = [r for r in rows if r["kind"] == "unique"]
        classified = sum(r["count"] for r in rows)
        unclassified = self.env["whatsapp.conversation"].search_count(
            window + [("issue_id", "=", False)]
        )

        # Customers who came back, which is a different question from whether
        # the issue repeats across different people.
        seen = {}
        for conversation in self.env["whatsapp.conversation"].search(window):
            seen[conversation.number] = seen.get(conversation.number, 0) + 1
        repeat_contacts = sum(1 for count in seen.values() if count > 1)

        return {
            "rows": rows[:20],
            "repeated": repeated[:10],
            "unique_count": len(unique),
            "repeated_count": len(repeated),
            "distinct_count": len(rows),
            "classified": classified,
            "unclassified": unclassified,
            "repeat_share": round(
                sum(r["count"] for r in repeated) * 100.0 / classified
            ) if classified else 0,
            "contacts": len(seen),
            "repeat_contacts": repeat_contacts,
        }

    @api.model
    def _report_csat(self, filters, start, end):
        domain = [("create_date", ">=", start), ("create_date", "<", end)]
        domain += self._filter_domain(filters)
        if "view_all_reports" not in self._rights():
            domain.append(("user_id", "=", self.env.uid))
        groups = self.env["whatsapp.rating"]._read_group(domain, ["score"], ["__count"])
        counts = {score: count for score, count in groups}
        total = sum(counts.values())
        distribution = [
            {"score": score, "count": counts.get(str(score), 0),
             "percent": round(counts.get(str(score), 0) * 100.0 / total) if total else 0}
            for score in range(5, 0, -1)
        ]
        happy = sum(counts.get(str(s), 0) for s in (4, 5))
        return {
            "distribution": distribution,
            "total": total,
            "average": round(
                sum(int(s) * c for s, c in counts.items()) / total, 2
            ) if total else 0,
            "happy_percent": round(happy * 100.0 / total) if total else 0,
            "comments": [
                {"score": r.score_value, "comment": r.comment,
                 "partner": r.partner_id.display_name or "",
                 "date": fields.Datetime.to_string(r.create_date)}
                for r in self.env["whatsapp.rating"].search(
                    domain + [("comment", "!=", False)], limit=8
                )
            ],
        }

    @api.model
    def _report_response_buckets(self, scope, start, end):
        """How long customers actually wait, in readable bands."""
        records = self.env["whatsapp.conversation"].search_read(
            scope + [("first_response_seconds", ">", 0),
                     ("handoff_date", ">=", start), ("handoff_date", "<", end)],
            ["first_response_seconds"],
        )
        rows = [{"label": label, "value": 0} for _limit, label in RESPONSE_BUCKETS]
        for record in records:
            seconds = record["first_response_seconds"]
            for index, (limit, _label) in enumerate(RESPONSE_BUCKETS):
                if limit is None or seconds < limit:
                    rows[index]["value"] += 1
                    break
        total = len(records)
        for row in rows:
            row["percent"] = round(row["value"] * 100.0 / total) if total else 0
        return {"rows": rows, "total": total}

    # ==================================================================
    # Export
    # ==================================================================
    @api.model
    def export_report(self, kind, filters=None):
        """Render a report as CSV and return a download URL.

        The file is stored as an attachment so the browser fetches it through
        Odoo's normal content route, rather than shipping the whole dataset
        back inside a JSON response.
        """
        self._assert("export")
        builders = {
            "leaderboard": self._csv_leaderboard,
            "tags": self._csv_tags,
            "departments": self._csv_departments,
            "csat": self._csv_csat,
            "issues": self._csv_issues,
        }
        if kind == "conversations":
            header, rows = self._csv_conversations(filters)
        elif kind in builders:
            header, rows = builders[kind](self.get_reports(filters))
        else:
            raise UserError(_("Nothing to export for '%s'.") % kind)

        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
        stamp = fields.Datetime.now().strftime("%Y%m%d-%H%M")
        name = "whatsapp-%s-%s.csv" % (kind, stamp)
        attachment = self.env["ir.attachment"].create({
            "name": name,
            "type": "binary",
            "mimetype": "text/csv",
            "datas": base64.b64encode(buffer.getvalue().encode("utf-8-sig")),
            "res_model": self._name,
        })
        return {
            "name": name,
            "url": "/web/content/%s?download=true" % attachment.id,
            "rows": len(rows),
        }

    def _csv_leaderboard(self, reports):
        header = ["Agent", "Role", "Chats handled", "Avg first response (s)",
                  "Avg resolution (s)", "Ratings", "CSAT %", "Open now"]
        rows = [
            [r["name"], r["role"], r["handled"], r["avg_response"],
             r["avg_resolution"], r["rating_count"], r["csat"], r["open_now"]]
            for r in reports["leaderboard"]
        ]
        return header, rows

    def _csv_tags(self, reports):
        header = ["Tag", "Conversations"]
        rows = [[r["label"], r["value"]] for r in reports["tags"]["rows"]]
        rows.append(["(untagged)", reports["tags"]["untagged"]])
        return header, rows

    def _csv_departments(self, reports):
        header = ["Department", "Conversations", "Avg first response (s)"]
        rows = [[r["label"], r["value"], r["avg_response"]]
                for r in reports["departments"]]
        return header, rows

    def _csv_issues(self, reports):
        header = ["Issue", "Kind", "Occurrences", "Customers", "Still open",
                  "Avg time to close (s)", "Avg rating", "Tags", "Last seen"]
        rows = [
            [r["name"], r["kind"], r["count"], r["contacts"], r["open"],
             r["avg_resolution"], r["rating"], ", ".join(r["tags"]),
             r["last_seen"]]
            for r in reports["issues"]["rows"]
        ]
        return header, rows

    def _csv_csat(self, reports):
        header = ["Score", "Responses", "Share %"]
        rows = [[r["score"], r["count"], r["percent"]]
                for r in reports["csat"]["distribution"]]
        return header, rows

    def _csv_conversations(self, filters):
        start, end, _prev = self._period_bounds(filters)
        domain = self._report_domain() + self._filter_domain(filters)
        domain += [("create_date", ">=", start), ("create_date", "<", end)]
        records = self.env["whatsapp.conversation"].search(domain, limit=5000)
        header = ["Opened", "Number", "Contact", "Department", "Agent", "State",
                  "Priority", "Tags", "Ticket", "First response (s)",
                  "Resolution (s)", "Bot resolved", "Rating"]
        rows = [
            [
                fields.Datetime.to_string(c.create_date), c.number,
                c.partner_id.display_name or "", c.team_id.name or "",
                c.user_id.name or "", c.state, c.priority,
                ", ".join(c.tag_ids.mapped("name")),
                c.ticket_id.id or "", c.first_response_seconds,
                c.resolution_seconds, "yes" if c.bot_resolved else "no",
                c.rating_score or "",
            ]
            for c in records
        ]
        return header, rows

    # ==================================================================
    # Contacts
    # ==================================================================
    @api.model
    def get_contacts(self, search=None, limit=40, offset=0):
        """Customers who have ever written in, with their chat history."""
        domain = self._scope_domain()
        if search:
            domain += ["|", "|",
                       ("number", "ilike", search),
                       ("partner_id.name", "ilike", search),
                       ("profile_name", "ilike", search)]
        conversations = self.env["whatsapp.conversation"].search(
            domain, order="last_message_date desc"
        )
        # Group by number rather than partner: an unknown number has no partner
        # yet but is still a contact worth showing.
        seen, contacts = {}, []
        for conversation in conversations:
            key = conversation.number
            if key in seen:
                seen[key]["chats"] += 1
                continue
            entry = {
                "number": conversation.number,
                "partner_id": conversation.partner_id.id or False,
                "name": conversation.partner_id.display_name
                        or conversation.profile_name or conversation.number,
                "email": conversation.partner_id.email or "",
                "avatar": conversation.partner_id.id and
                          "/web/image/res.partner/%s/avatar_128" % conversation.partner_id.id
                          or False,
                "chats": 1,
                "last_seen": fields.Datetime.to_string(conversation.last_message_date),
                "state": conversation.state,
                "conversation_id": conversation.id,
                "tags": [t._payload() for t in conversation.tag_ids],
                "rating": conversation.rating_score or 0,
                "blocked": bool(
                    self.env["whatsapp.blocklist"]._entry_for(conversation.number)
                ),
            }
            seen[key] = entry
            contacts.append(entry)
        total = len(contacts)
        return {"contacts": contacts[offset:offset + limit], "total": total}

    # ==================================================================
    # Blocklist
    # ==================================================================
    @api.model
    def get_blocklist(self):
        return [b._payload() for b in self.env["whatsapp.blocklist"].search([])]

    @api.model
    def block_number(self, number, reason="spam", note=None):
        self._assert("block")
        entry = self.env["whatsapp.blocklist"]._block(number, reason, note)
        conversation = self.env["whatsapp.conversation"].sudo().search(
            [("number", "=", entry.number)], limit=1
        )
        if conversation and conversation.state != "closed":
            conversation._close(notify=False)
        return self.get_blocklist()

    @api.model
    def unblock_number(self, blocklist_id):
        self._assert("block")
        self.env["whatsapp.blocklist"].browse(blocklist_id).unlink()
        return self.get_blocklist()

    # ==================================================================
    # Roster
    # ==================================================================
    @api.model
    def get_agents(self):
        agents = self.env["whatsapp.agent"].sudo().search([])
        return [agent._dashboard_payload() for agent in agents]

    @api.model
    def get_teams(self):
        # sudo: an agent needs the department names to filter by, but does not
        # necessarily hold helpdesk read rights. Names only, nothing sensitive.
        return [{"id": t.id, "name": t.name}
                for t in self.env["helpdesk.team"].sudo().search([])]

    @api.model
    def get_candidate_users(self, search=None, limit=20):
        """Internal users who are not on the roster yet."""
        self._assert("manage_roster")
        existing = self.env["whatsapp.agent"].sudo().search([]).user_id.ids
        domain = [("share", "=", False), ("active", "=", True),
                  ("id", "not in", existing)]
        if search:
            domain.append(("name", "ilike", search))
        users = self.env["res.users"].sudo().search(domain, limit=limit, order="name")
        return [
            {"id": u.id, "name": u.name, "email": u.email or "",
             "avatar": "/web/image/res.users/%s/avatar_128" % u.id}
            for u in users
        ]

    @api.model
    def add_agents(self, user_ids, team_ids=None, role="agent"):
        me = self._assert("manage_roster")
        if role in ("owner", "admin") and me and me.role != "owner":
            raise AccessError(_("Only the Owner can create another Administrator."))
        if role == "owner":
            raise UserError(_(
                "Ownership is transferred, not granted: add the person first, "
                "then promote them to Owner."
            ))
        agents = self.env["whatsapp.agent"].sudo()
        for user_id in user_ids or []:
            if agents.search_count([("user_id", "=", user_id)]):
                continue
            agents.create({
                "user_id": user_id,
                "role": role,
                "team_ids": [(6, 0, team_ids or [])],
            })
        return self.get_agents()

    @api.model
    def remove_agent(self, agent_id):
        me = self._assert("manage_roster")
        agent = self.env["whatsapp.agent"].sudo().browse(agent_id)
        if agent.role == "owner":
            raise UserError(_("The Owner cannot be removed. Transfer ownership first."))
        if me and not me.can_act_on(agent):
            raise AccessError(_("Your role does not allow removing %s.") % agent.user_id.name)
        open_chats = self.env["whatsapp.conversation"].sudo().search_count([
            ("user_id", "=", agent.user_id.id), ("state", "!=", "closed"),
        ])
        if open_chats:
            raise UserError(_(
                "%(name)s still has %(count)s open chat(s). Reassign them first.",
                name=agent.user_id.name, count=open_chats,
            ))
        agent.unlink()
        return self.get_agents()

    @api.model
    def update_agent(self, agent_id, values):
        """Change one roster entry.

        Agents may flip their own presence and personal signature; everything
        else needs the roster right, and role changes need seniority over the
        person being changed.
        """
        agent = self.env["whatsapp.agent"].sudo().browse(agent_id)
        me = self._me()
        touched = set(values)
        own_settings = touched <= {"status", "signature", "display_alias"}

        if agent.user_id == self.env.user and own_settings:
            pass
        else:
            self._assert("manage_roster")
            if "role" in touched:
                self._assert("manage_roles")
                if me and not me.can_act_on(agent):
                    raise AccessError(
                        _("Your role does not allow changing %s.") % agent.user_id.name
                    )
                if values["role"] == "owner" and me and me.role != "owner":
                    raise AccessError(_("Only the Owner can transfer ownership."))

        writable = {"status", "auto_assign", "max_active_chats", "team_ids",
                    "account_ids", "sequence", "active", "role", "signature",
                    "display_alias"}
        agent.write({k: v for k, v in values.items() if k in writable})
        return self.get_agents()

    @api.model
    def set_my_status(self, status):
        agent = self.env["whatsapp.agent"]._current()
        if not agent:
            raise UserError(_(
                "You are not on the WhatsApp agent roster yet. Ask an "
                "administrator to add you from the dashboard."
            ))
        agent.status = status
        return agent._dashboard_payload()
