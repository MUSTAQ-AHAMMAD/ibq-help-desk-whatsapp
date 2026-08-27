# -*- coding: utf-8 -*-
"""Commands an agent can text to the support number.

An agent with their own WhatsApp number on the roster can run the queue from
their phone: ``#take 1042``, ``#assign 1042 sue``, ``#close 1042``. The command
is recognised before any customer handling, so it never creates a conversation
and never reaches a customer.

Everything here runs **as the agent's own user**, not as sudo, so the same
roles, record rules and rights that govern the dashboard govern the commands.
A supervisor can reassign anyone's chat by text; an agent cannot.
"""
import logging
import re

from odoo import _, api, models
from odoo.exceptions import AccessError, UserError

from .whatsapp_account import normalize_number

_logger = logging.getLogger(__name__)

PREFIX = "#"
# "#assign 1042 sue" -> verb "assign", rest "1042 sue"
COMMAND_RE = re.compile(r"^\s*#\s*([a-zA-Z]+)\s*(.*)$", re.DOTALL)


class WhatsappCommand(models.AbstractModel):
    _name = "whatsapp.command"
    _description = "WhatsApp Agent Command"

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    @api.model
    def looks_like_command(self, body):
        return bool(COMMAND_RE.match(body or ""))

    @api.model
    def execute(self, body):
        """Run one command and return the reply to text back to the agent.

        Never raises: an agent who typos a command should get a sentence back,
        not silence, and the webhook must always answer Twilio.
        """
        match = COMMAND_RE.match(body or "")
        if not match:
            return self._help()
        verb = match.group(1).lower()
        rest = (match.group(2) or "").strip()

        handler = getattr(self, "_cmd_%s" % verb, None)
        if handler is None:
            return _("Unknown command %(verb)s. Send #help for the list.",
                     verb=PREFIX + verb)
        try:
            return handler(rest)
        except (UserError, AccessError) as exc:
            return str(exc)
        except Exception:  # noqa: BLE001 - the agent still deserves an answer
            _logger.exception("WhatsApp command failed: %s", body)
            return _("Something went wrong running that command. "
                     "It has been logged for the administrators.")

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------
    @api.model
    def _me(self):
        agent = self.env["whatsapp.agent"]._current()
        if not agent:
            raise UserError(_("You are not on the WhatsApp roster."))
        return agent

    @api.model
    def _resolve_conversation(self, ref):
        """Find a chat from a ticket number or a customer's phone number.

        Searched as the calling agent, so a chat they are not allowed to see
        simply does not resolve.
        """
        ref = (ref or "").strip().lstrip("#")
        if not ref:
            raise UserError(_("Which chat? Give a ticket number, e.g. #take 1042."))
        conversations = self.env["whatsapp.conversation"]

        if ref.isdigit():
            found = conversations.search([("ticket_id", "=", int(ref))], limit=1)
            if found:
                return found
        number = normalize_number(ref)
        if number:
            found = conversations.search([("number", "=", number)], limit=1)
            if found:
                return found
        raise UserError(_(
            "No chat found for %(ref)s. Use the ticket number or the "
            "customer's number.", ref=ref,
        ))

    @api.model
    def _resolve_agent(self, text):
        """Match 'sue', 'Sue Iqbal' or a phone number to a roster entry."""
        needle = (text or "").strip()
        if not needle:
            raise UserError(_("Assign to whom? e.g. #assign 1042 sue"))
        agents = self.env["whatsapp.agent"].sudo().search([])

        number = normalize_number(needle)
        if number:
            by_phone = agents.filtered(lambda a: a.phone == number)
            if by_phone:
                return by_phone[0]

        lowered = needle.lower()
        exact = agents.filtered(
            lambda a: lowered in (a.user_id.login.lower(), a.user_id.name.lower())
        )
        if len(exact) == 1:
            return exact[0]
        partial = exact or agents.filtered(lambda a: lowered in a.user_id.name.lower())
        if len(partial) == 1:
            return partial[0]
        if not partial:
            raise UserError(_("No agent called '%s'. Send #team for the list.") % needle)
        names = ", ".join(partial.mapped("user_id.name"))
        raise UserError(_(
            "'%(needle)s' matches several people: %(names)s. Be more specific.",
            needle=needle, names=names,
        ))

    @api.model
    def _label(self, conversation):
        who = conversation.partner_id.display_name or conversation.number
        if conversation.ticket_id:
            return _("#%(ticket)s %(who)s", ticket=conversation.ticket_id.id, who=who)
        return who

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    def _help(self):
        return _(
            "*WhatsApp commands*\n"
            "#queue - chats waiting for an answer\n"
            "#mine - chats assigned to you\n"
            "#who 1042 - who owns a chat\n"
            "#take 1042 - assign it to yourself\n"
            "#assign 1042 sue - assign it to someone else\n"
            "#note 1042 called them back - internal note\n"
            "#close 1042 - close the chat\n"
            "#tag 1042 billing - add a tag\n"
            "#status available|busy|away|offline\n"
            "#team - who is on shift"
        )

    def _cmd_help(self, rest):
        return self._help()

    def _cmd_status(self, rest):
        wanted = (rest or "").strip().lower()
        valid = ("available", "busy", "away", "offline")
        if wanted not in valid:
            raise UserError(_("Status must be one of: %s.") % ", ".join(valid))
        agent = self._me()
        agent.sudo().status = wanted
        return _("You are now *%s*.") % wanted

    def _cmd_team(self, rest):
        agents = self.env["whatsapp.agent"].sudo().search([], order="role, id")
        if not agents:
            return _("Nobody is on the roster.")
        lines = [_("*On shift*")]
        for agent in agents:
            lines.append("%s - %s (%s/%s)" % (
                agent.user_id.name, agent.status,
                agent.active_chat_count, agent.max_active_chats,
            ))
        return "\n".join(lines)

    def _cmd_queue(self, rest):
        conversations = self.env["whatsapp.conversation"].search(
            [("state", "=", "agent"), ("needs_reply", "=", True)],
            order="priority desc, last_message_date asc", limit=10,
        )
        if not conversations:
            return _("Nothing is waiting. ")
        lines = [_("*Waiting for a reply*")]
        for conversation in conversations:
            lines.append("%s - %s" % (
                self._label(conversation),
                conversation.user_id.name or _("unassigned"),
            ))
        return "\n".join(lines)

    def _cmd_mine(self, rest):
        conversations = self.env["whatsapp.conversation"].search(
            [("user_id", "=", self.env.uid), ("state", "!=", "closed")],
            order="needs_reply desc, last_message_date asc", limit=10,
        )
        if not conversations:
            return _("You have no open chats.")
        lines = [_("*Your chats*")]
        for conversation in conversations:
            flag = _(" - waiting") if conversation.needs_reply else ""
            lines.append("%s%s" % (self._label(conversation), flag))
        return "\n".join(lines)

    def _cmd_who(self, rest):
        conversation = self._resolve_conversation(rest)
        return _(
            "%(label)s\nAgent: %(agent)s\nDepartment: %(team)s\nStatus: %(state)s",
            label=self._label(conversation),
            agent=conversation.user_id.name or _("unassigned"),
            team=conversation.team_id.name or _("none"),
            state=conversation.state,
        )

    def _cmd_take(self, rest):
        conversation = self._resolve_conversation(rest)
        conversation.assign_to(self.env.user, source=_("a WhatsApp command"))
        return _("%s is yours.") % self._label(conversation)

    def _cmd_assign(self, rest):
        parts = (rest or "").split(None, 1)
        if len(parts) < 2:
            raise UserError(_("Use: #assign 1042 sue"))
        conversation = self._resolve_conversation(parts[0])
        agent = self._resolve_agent(parts[1])
        conversation.assign_to(agent.user_id, source=_("a WhatsApp command"))
        return _("%(label)s assigned to %(who)s.",
                 label=self._label(conversation), who=agent.user_id.name)

    def _cmd_close(self, rest):
        conversation = self._resolve_conversation(rest)
        conversation._close(notify=True)
        return _("%s closed.") % self._label(conversation)

    def _cmd_note(self, rest):
        parts = (rest or "").split(None, 1)
        if len(parts) < 2:
            raise UserError(_("Use: #note 1042 what you want to record"))
        conversation = self._resolve_conversation(parts[0])
        conversation.add_note(parts[1])
        return _("Note added to %s.") % self._label(conversation)

    def _cmd_tag(self, rest):
        parts = (rest or "").split(None, 1)
        if len(parts) < 2:
            raise UserError(_("Use: #tag 1042 billing"))
        conversation = self._resolve_conversation(parts[0])
        needle = parts[1].strip().lower()
        tag = self.env["whatsapp.tag"].search(
            [("name", "=ilike", needle)], limit=1
        ) or self.env["whatsapp.tag"].search(
            [("name", "ilike", needle)], limit=1
        )
        if not tag:
            names = ", ".join(self.env["whatsapp.tag"].search([]).mapped("name"))
            raise UserError(_("No tag called '%(needle)s'. Available: %(names)s",
                              needle=parts[1], names=names or _("none")))
        conversation.tag_ids = [(4, tag.id)]
        return _("%(label)s tagged %(tag)s.",
                 label=self._label(conversation), tag=tag.name)
