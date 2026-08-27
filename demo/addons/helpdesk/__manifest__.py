# -*- coding: utf-8 -*-
{
    "name": "Helpdesk (demo stub)",
    "summary": "Minimal stand-in for Odoo Enterprise Helpdesk, for the demo stack only.",
    "description": """
Helpdesk stub — DEMO ONLY
=========================

``ibq_whatsapp_helpdesk`` depends on Odoo Enterprise's ``helpdesk`` app, which
is not in the Community image. This stub provides just enough of it — the three
models, the fields the WhatsApp module reads, and the three view IDs it
inherits — to install and run the dashboard on Community.

It is deliberately NOT a helpdesk. Do not install it on a real database: on
Enterprise the genuine ``helpdesk`` module provides all of this and much more,
and having both would collide.
""",
    "version": "17.0.1.0.0",
    "category": "Services/Helpdesk",
    "license": "LGPL-3",
    "depends": ["base", "mail"],
    "data": [
        "security/helpdesk_security.xml",
        "security/ir.model.access.csv",
        "views/helpdesk_views.xml",
    ],
    "installable": True,
    "application": False,
}
