# -*- coding: utf-8 -*-
"""Grouping what customers actually contact about.

Tags say what a chat was *filed under*; an issue says what it was *about*.
Two customers writing "the office printer is jammed" and "printer jammed again,
3rd floor" have the same problem, and a support lead needs to see that as one
recurring issue rather than two unrelated tickets.

The matching is deliberately plain: normalise the subject to a set of
significant words, then compare that set against existing issues with a
Jaccard overlap. No model, no service, nothing to train — which means the
result is inspectable and a human can correct it by merging two issues.
"""
import re

from odoo import _, api, fields, models

# Words that carry no signal about *what* the problem is.
STOPWORDS = {
    "a", "about", "after", "again", "all", "am", "an", "and", "any", "are",
    "as", "at", "be", "been", "but", "by", "can", "cannot", "cant", "could",
    "did", "do", "does", "doing", "dont", "for", "from", "get", "getting",
    "had", "has", "have", "he", "her", "here", "hi", "hello", "how", "i",
    "if", "in", "is", "it", "its", "just", "me", "my", "need", "no", "not",
    "of", "on", "one", "or", "our", "out", "over", "please", "pls", "she",
    "so", "some", "still", "thanks", "that", "the", "their", "them", "then",
    "there", "these", "they", "this", "to", "too", "up", "us", "very", "want",
    "was", "we", "were", "what", "when", "where", "which", "who", "why",
    "will", "with", "would", "you", "your",
    # Attention-getters, not descriptions of a problem.
    "anybody", "anyone", "afternoon", "evening", "hey", "hiya", "morning",
    "okay", "somebody", "someone", "thank", "thanks", "yeah",
}
TOKEN_RE = re.compile(r"[a-z]+")
MIN_TOKEN = 3
# One significant word is almost never a description of a problem -- it is
# "anyone?" or "printer". Below this, the chat is left unclassified rather
# than inventing an issue out of filler.
MIN_TOKENS_FOR_ISSUE = 2
# Two subjects are the same issue when this much of their vocabulary overlaps.
SIMILARITY = 0.55


def tokenise(text):
    """Significant, lowercase, de-duplicated words from a subject line.

    Digits go entirely: "invoice 4471" and "invoice 5120" are the same issue,
    and keeping the reference would split every occurrence into its own.
    """
    words = TOKEN_RE.findall((text or "").lower())
    return {w for w in words if len(w) >= MIN_TOKEN and w not in STOPWORDS}


def similarity(left, right):
    """Jaccard overlap of two token sets, 0.0 to 1.0."""
    if not left or not right:
        return 0.0
    return len(left & right) / float(len(left | right))


class WhatsappIssue(models.Model):
    _name = "whatsapp.issue"
    _description = "WhatsApp Issue"
    _order = "occurrence_count desc, id desc"

    name = fields.Char(
        required=True,
        help="The subject line of the first chat that raised it. Rename it to "
             "something the team recognises.",
    )
    keywords = fields.Char(
        readonly=True,
        help="The significant words this issue is matched on.",
    )
    active = fields.Boolean(default=True)
    team_id = fields.Many2one("helpdesk.team", string="Department")
    tag_ids = fields.Many2many("whatsapp.tag", string="Tags")

    conversation_ids = fields.One2many(
        "whatsapp.conversation", "issue_id", string="Conversations"
    )
    occurrence_count = fields.Integer(
        "Occurrences", compute="_compute_stats", store=True
    )
    contact_count = fields.Integer(
        "Customers", compute="_compute_stats", store=True,
        help="How many different people raised it.",
    )
    first_seen = fields.Datetime(compute="_compute_stats", store=True)
    last_seen = fields.Datetime(compute="_compute_stats", store=True)
    kind = fields.Selection(
        [("unique", "One-off"), ("repeated", "Repeated")],
        compute="_compute_stats", store=True, index=True,
        help="Repeated once it has been raised more than once.",
    )
    avg_resolution_seconds = fields.Integer(
        "Avg Time to Close", compute="_compute_stats", store=True
    )
    rating_avg = fields.Float("Avg Rating", compute="_compute_stats", store=True)
    note = fields.Text(
        help="What the fix or workaround is. This is the point of grouping "
             "them: write it once.",
    )

    @api.depends("conversation_ids", "conversation_ids.create_date",
                 "conversation_ids.resolution_seconds",
                 "conversation_ids.rating_score")
    def _compute_stats(self):
        for issue in self:
            conversations = issue.conversation_ids
            issue.occurrence_count = len(conversations)
            issue.contact_count = len(set(conversations.mapped("number")))
            dates = conversations.mapped("create_date")
            issue.first_seen = min(dates) if dates else False
            issue.last_seen = max(dates) if dates else False
            issue.kind = "repeated" if len(conversations) > 1 else "unique"
            resolved = conversations.filtered("resolution_seconds")
            issue.avg_resolution_seconds = round(
                sum(resolved.mapped("resolution_seconds")) / len(resolved)
            ) if resolved else 0
            rated = conversations.filtered("rating_score")
            issue.rating_avg = round(
                sum(rated.mapped("rating_score")) / len(rated), 2
            ) if rated else 0.0

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------
    @api.model
    def _subject_of(self, conversation):
        """The best sentence available to describe what the chat is about."""
        answers = conversation._get_answers()
        for key in ("subject", "issue", "problem", "details"):
            if answers.get(key):
                return answers[key]
        if conversation.ticket_id:
            return conversation.ticket_id.name
        inbound = conversation.whatsapp_message_ids.filtered(
            lambda m: m.direction == "inbound" and (m.body or "").strip()
        ).sorted("id")
        # Skip the opening "hi": it describes nothing.
        for message in inbound:
            if len(tokenise(message.body)) >= MIN_TOKENS_FOR_ISSUE:
                return message.body
        return ""

    @api.model
    def _match_or_create(self, conversation):
        """Attach a conversation to the issue it belongs to.

        Returns an empty recordset when there is nothing to go on, which is
        normal for a chat that never got past "hello".
        """
        subject = self._subject_of(conversation)
        tokens = tokenise(subject)
        if len(tokens) < MIN_TOKENS_FOR_ISSUE:
            return self.browse()

        best, best_score = self.browse(), 0.0
        for issue in self.sudo().search([]):
            score = similarity(tokens, set((issue.keywords or "").split()))
            if score > best_score:
                best, best_score = issue, score

        if best and best_score >= SIMILARITY:
            # Widen the issue's vocabulary a little so near-misses match next
            # time, without letting it drift into a catch-all.
            merged = set((best.keywords or "").split()) | tokens
            if len(merged) <= len(tokens) * 3:
                best.sudo().keywords = " ".join(sorted(merged))
            issue = best
        else:
            issue = self.sudo().create({
                "name": (subject or "").strip()[:120],
                "keywords": " ".join(sorted(tokens)),
                "team_id": conversation.team_id.id or False,
            })

        conversation.sudo().issue_id = issue
        if conversation.tag_ids:
            issue.sudo().tag_ids = [(4, tag.id) for tag in conversation.tag_ids]
        return issue

    @api.model
    def _cron_classify(self, limit=500):
        """Catch up on chats that have no issue yet, and sweep up empties.

        An issue with nothing left under it is rot -- from a merge, a deleted
        chat, or a matching rule that was tightened after the fact. Leaving it
        in the catalogue means the reports count problems nobody has.
        """
        pending = self.env["whatsapp.conversation"].sudo().search(
            [("issue_id", "=", False)], limit=limit, order="id"
        )
        for conversation in pending:
            self._match_or_create(conversation)
        self.sudo().search([("occurrence_count", "=", 0)]).unlink()
        return len(pending)

    @api.model
    def action_reclassify_all(self):
        """Re-run matching over every chat, from an empty catalogue.

        The rules can be tuned -- a stopword added, the threshold moved -- and
        this is how you apply that to what is already there.
        """
        self.env["whatsapp.agent"]._assert_right("manage_tags")
        conversations = self.env["whatsapp.conversation"].sudo().search([])
        conversations.write({"issue_id": False})
        self.sudo().search([]).unlink()
        for conversation in conversations:
            self._match_or_create(conversation)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": _("Reclassified"),
                "message": _("%(chats)s chat(s) regrouped into %(issues)s issue(s).",
                             chats=len(conversations),
                             issues=self.sudo().search_count([])),
                "sticky": False,
            },
        }

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_view_conversations(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id(
            "ibq_whatsapp_helpdesk.action_whatsapp_conversation"
        )
        action["domain"] = [("issue_id", "=", self.id)]
        action["context"] = {}
        return action

    def action_merge(self):
        """Fold the selected issues into the one raised most often.

        Grouping by word overlap will always split a few things a human can
        see are the same; this is the correction.
        """
        if len(self) < 2:
            return True
        target = max(self, key=lambda i: i.occurrence_count)
        others = self - target
        keywords = set((target.keywords or "").split())
        for issue in others:
            keywords |= set((issue.keywords or "").split())
        others.conversation_ids.sudo().write({"issue_id": target.id})
        target.sudo().write({
            "keywords": " ".join(sorted(keywords)),
            "tag_ids": [(4, tag.id) for tag in others.tag_ids],
        })
        others.sudo().unlink()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": _("Merged"),
                "message": _("Folded into '%s'.") % target.name,
                "sticky": False,
            },
        }
