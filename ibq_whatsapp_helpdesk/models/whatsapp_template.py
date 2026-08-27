# -*- coding: utf-8 -*-
import json
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

PLACEHOLDER_RE = re.compile(r"\{\{(\d+)\}\}")


class WhatsappTemplate(models.Model):
    """An approved WhatsApp message template.

    Outside the 24h session window WhatsApp refuses free-form text, so every
    proactive notification (ticket acknowledgement, stage change, CSAT ask)
    has to travel through a template approved in the Twilio Content Template
    Builder. This model mirrors that template so Odoo can render the same
    body locally for the chatter and map placeholders to record fields.
    """

    _name = "whatsapp.template"
    _description = "WhatsApp Template"
    _order = "name"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    code = fields.Char(
        required=True,
        help="Technical key used from code and automation, e.g. 'ticket_created'.",
    )
    content_sid = fields.Char(
        "Content SID",
        help="Starts with 'HX'. Copy it from Twilio > Content Template Builder. "
             "Required to send outside the 24h session window.",
    )
    account_id = fields.Many2one("whatsapp.account", string="Account")
    body = fields.Text(
        required=True,
        help="Template body using Twilio numbered placeholders: {{1}}, {{2}}, ...",
    )
    variable_ids = fields.One2many(
        "whatsapp.template.variable", "template_id", string="Variables"
    )
    model_id = fields.Many2one(
        "ir.model", string="Applies to",
        domain=[("transient", "=", False)],
        help="Model whose fields the variables are read from, usually Helpdesk Ticket.",
    )
    model = fields.Char(related="model_id.model", store=True, readonly=True)
    lang_code = fields.Char("Language", default="en", required=True)
    status = fields.Selection(
        [("draft", "Draft"), ("pending", "Pending Approval"),
         ("approved", "Approved"), ("rejected", "Rejected")],
        default="draft", required=True,
        help="Mirrors the approval status shown in the Twilio console.",
    )

    _sql_constraints = [
        ("code_uniq", "unique(code)", "A template with this code already exists."),
    ]

    @api.constrains("body", "variable_ids")
    def _check_placeholders(self):
        for record in self:
            used = {int(index) for index in PLACEHOLDER_RE.findall(record.body or "")}
            if not used:
                continue
            expected = set(range(1, max(used) + 1))
            if used != expected:
                raise ValidationError(_(
                    "Placeholders in '%(name)s' must run 1..N without gaps; "
                    "found %(found)s.",
                    name=record.name,
                    found=", ".join("{{%s}}" % i for i in sorted(used)),
                ))
            declared = {v.index for v in record.variable_ids}
            missing = used - declared
            if declared and missing:
                raise ValidationError(_(
                    "Template '%(name)s' uses %(missing)s but does not declare them.",
                    name=record.name,
                    missing=", ".join("{{%s}}" % i for i in sorted(missing)),
                ))

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def _resolve_variables(self, record=None):
        """Return {index: value} for a source record."""
        self.ensure_one()
        values = {}
        for variable in self.variable_ids.sorted("index"):
            values[variable.index] = variable._render(record)
        return values

    def render(self, record=None, values=None):
        """Return (body_text, content_variables_json).

        ``values`` overrides anything resolved from ``record`` and is keyed by
        placeholder index.
        """
        self.ensure_one()
        resolved = self._resolve_variables(record)
        resolved.update({int(k): v for k, v in (values or {}).items()})

        def substitute(match):
            return str(resolved.get(int(match.group(1)), ""))

        body = PLACEHOLDER_RE.sub(substitute, self.body or "")
        content_variables = json.dumps(
            {str(index): str(value) for index, value in sorted(resolved.items())}
        ) if resolved else False
        return body, content_variables

    @api.model
    def _get_by_code(self, code, account=None):
        domain = [("code", "=", code)]
        if account:
            domain += ["|", ("account_id", "=", account.id), ("account_id", "=", False)]
        return self.search(domain, limit=1, order="account_id desc")

    def action_send_test(self):
        """Send this template to the current user's mobile, to eyeball formatting."""
        self.ensure_one()
        number = self.env.user.partner_id.mobile or self.env.user.partner_id.phone
        if not number:
            raise UserError(_("Set a mobile number on your own contact first."))
        account = self.account_id or self.env["whatsapp.account"]._get_default_account()
        if not account:
            raise UserError(_("No WhatsApp account is configured."))
        body, content_variables = self.render()
        self.env["whatsapp.message"].create({
            "account_id": account.id,
            "direction": "outbound",
            "number": number,
            "body": body,
            "message_type": "template",
            "template_id": self.id,
            "content_variables": content_variables,
        }).action_send()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "info",
                "title": _("Test queued"),
                "message": _("A test message was queued to %s.") % number,
                "sticky": False,
            },
        }


class WhatsappTemplateVariable(models.Model):
    _name = "whatsapp.template.variable"
    _description = "WhatsApp Template Variable"
    _order = "index"

    template_id = fields.Many2one(
        "whatsapp.template", required=True, ondelete="cascade"
    )
    index = fields.Integer(required=True, help="The N in {{N}}.")
    name = fields.Char(required=True, help="What this placeholder stands for.")
    source_type = fields.Selection(
        [("field", "Record Field"), ("static", "Static Text"),
         ("context", "Conversation Answer")],
        default="field", required=True,
    )
    field_path = fields.Char(
        help="Dotted path read from the source record, e.g. 'partner_id.name' "
             "or 'stage_id.name'. For conversation answers, the answer key.",
    )
    static_value = fields.Char()

    _sql_constraints = [
        ("index_uniq", "unique(template_id, index)",
         "Each placeholder index can only be declared once per template."),
    ]

    def _render(self, record=None):
        self.ensure_one()
        if self.source_type == "static":
            return self.static_value or ""
        if not record or not self.field_path:
            return ""
        if self.source_type == "context":
            answers = record._get_answers() if hasattr(record, "_get_answers") else {}
            return answers.get(self.field_path, "")
        value = record
        for part in self.field_path.split("."):
            if not value:
                return ""
            value = value[part] if part in value._fields else False
        if hasattr(value, "display_name"):
            return value.display_name or ""
        return "" if value in (False, None) else str(value)
