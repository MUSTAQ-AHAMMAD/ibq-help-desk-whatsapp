# -*- coding: utf-8 -*-
{
    "name": "WhatsApp (Enterprise contract probe)",
    "summary": "Reproduces the model namespace Odoo Enterprise's WhatsApp app owns.",
    "description": """
TEST FIXTURE ONLY -- never install on a real database.

Odoo 17/18 Enterprise ships an official `whatsapp` app that owns the
`whatsapp.*` model namespace. Community does not have it, so a module that
squats on those names looks fine on Community and breaks on Enterprise.

This declares the minimum of that contract -- `whatsapp.message` carrying
`mail_message_id`, and the `mail.message` One2many that points at it -- so the
collision can be reproduced without an Enterprise licence.
""",
    "version": "17.0.1.0.0",
    "license": "LGPL-3",
    "depends": ["base", "mail"],
    "installable": True,
}
