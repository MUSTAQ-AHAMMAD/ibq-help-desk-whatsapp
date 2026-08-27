# -*- coding: utf-8 -*-
from odoo import _, api, fields, models

# 1-2 unhappy, 3 neutral, 4-5 happy. Kept as a 5-point scale because that is
# what customers are asked for over WhatsApp ("reply 1 to 5").
SENTIMENT_BANDS = ((4, "happy"), (3, "neutral"), (0, "unhappy"))


class WhatsappRating(models.Model):
    """A customer satisfaction score collected at the end of a chat."""

    _name = "whatsapp.rating"
    _description = "WhatsApp Satisfaction Rating"
    _order = "create_date desc"
    _rec_name = "score"

    conversation_id = fields.Many2one(
        "whatsapp.conversation", required=True, ondelete="cascade", index=True
    )
    ticket_id = fields.Many2one(
        related="conversation_id.ticket_id", store=True, string="Ticket"
    )
    partner_id = fields.Many2one(
        related="conversation_id.partner_id", store=True, string="Contact"
    )
    user_id = fields.Many2one(
        "res.users", string="Agent", index=True,
        help="Who was handling the chat when it closed.",
    )
    team_id = fields.Many2one("helpdesk.team", string="Department", index=True)
    score = fields.Selection(
        [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5")],
        required=True, index=True,
    )
    score_value = fields.Integer(compute="_compute_score_value", store=True)
    sentiment = fields.Selection(
        [("happy", "Happy"), ("neutral", "Neutral"), ("unhappy", "Unhappy")],
        compute="_compute_score_value", store=True, index=True,
    )
    comment = fields.Text(help="Anything the customer wrote after the score.")

    _sql_constraints = [
        ("one_per_conversation", "unique(conversation_id)",
         "This conversation has already been rated."),
    ]

    @api.depends("score")
    def _compute_score_value(self):
        for rating in self:
            value = int(rating.score or 0)
            rating.score_value = value
            rating.sentiment = next(
                (label for threshold, label in SENTIMENT_BANDS if value >= threshold),
                "unhappy",
            )

    @api.model
    def _record_from_reply(self, conversation, text):
        """Turn a bare '1'..'5' reply into a rating.

        Returns the rating, or an empty recordset when the reply was not a
        score, so the caller can fall back to normal message handling.
        """
        value = (text or "").strip()
        if value not in ("1", "2", "3", "4", "5"):
            return self.browse()
        if conversation.rating_id:
            return conversation.rating_id
        return self.sudo().create({
            "conversation_id": conversation.id,
            "user_id": conversation.user_id.id or False,
            "team_id": conversation.team_id.id or False,
            "score": value,
        })
