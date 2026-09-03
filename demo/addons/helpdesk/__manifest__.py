# -*- coding: utf-8 -*-
# One module, two Odoo versions.
#
# Odoo 18 renamed <tree> to <list>, the kanban card template, and the chatter,
# and XML cannot branch on a version the way Python can. So the views/ files
# are the Odoo 17 sources and v18/ holds the generated equivalents; this picks
# between them at load time. Regenerate v18/ with tools/build_v18.py after
# editing anything under views/ or wizard/.
from odoo import release

_V18 = release.version_info[0] >= 18


def _views(paths):
    """Point view and wizard paths at v18/ when running on Odoo 18+."""
    if not _V18:
        return paths
    return [
        "v18/" + path
        if path.endswith(".xml") and path.split("/")[0] in ("views", "wizard")
        else path
        for path in paths
    ]


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
    "version": "17.0.1.1.0",
    "category": "Services/Helpdesk",
    "license": "LGPL-3",
    "depends": ["base", "mail"],
    "data": _views([
        "security/helpdesk_security.xml",
        "security/ir.model.access.csv",
        "views/helpdesk_views.xml",
    ]),
    "installable": True,
    "application": False,
}
