# -*- coding: utf-8 -*-
{
    "name": "IBQ WhatsApp Helpdesk (Twilio)",
    "summary": "Run a helpdesk over WhatsApp with Twilio: scripted bot, ticket "
               "creation, a role-based agent console and full reporting.",
    "description": """
IBQ WhatsApp Helpdesk
=====================

Run a helpdesk over WhatsApp, using the Twilio Programmable Messaging API.

The channel
-----------

* Multi-account Twilio configuration: Account SID, Auth Token, and either a
  WhatsApp sender number or a Messaging Service.
* Signed inbound webhook, validating the ``X-Twilio-Signature`` HMAC, with
  duplicate ``MessageSid`` values ignored so Twilio retries never replay the bot.
* Conversations threaded per phone number, linked to a contact and a ticket.
* A no-code bot flow: menus, free-text questions with validation, ticket
  creation, agent hand-off, closing.
* Outbound queue with retries, delivery-status callbacks, and the 24h
  free-form session window enforced, falling back to approved templates.
* Inbound media fetched from Twilio server-side and attached.

The console
-----------

* A dashboard with six tabs: Overview, Monitoring, Inbox, Reports, Contacts
  and Team, over a shared period and department filter.
* Live chat for agents, with saved replies typed as ``/shortcut``, tags,
  priority, internal notes and transfers.
* Reports: agent leaderboard, a weekday-by-hour volume heatmap, first-response
  wait bands, satisfaction, tags and departments, each exportable to CSV.

The team
--------

* Four roles: Owner, Administrator, Supervisor and Agent. A role decides what
  the dashboard offers, what the server accepts, and which Odoo security group
  the person holds.
* Load-based routing to the least loaded available agent covering the chat's
  department and sender.
* Customer satisfaction collected automatically when a handled chat closes.
* A blocklist that drops inbound messages before any record is created.
""",
    "version": "17.0.3.0.0",
    "category": "Services/Helpdesk",
    "author": "IBQ",
    "website": "https://github.com/ibq/ibq-help-desk-whatsapp",
    "license": "LGPL-3",
    "depends": [
        "base",
        "web",
        "bus",
        "mail",
        "contacts",
        "helpdesk",
    ],
    "external_dependencies": {
        "python": ["requests"],
    },
    "data": [
        "security/whatsapp_security.xml",
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "data/whatsapp_bot_data.xml",
        "views/whatsapp_account_views.xml",
        "views/whatsapp_agent_views.xml",
        "views/whatsapp_crm_views.xml",
        "views/whatsapp_issue_views.xml",
        "views/whatsapp_template_views.xml",
        "views/whatsapp_bot_views.xml",
        "views/whatsapp_message_views.xml",
        "views/whatsapp_conversation_views.xml",
        "views/helpdesk_ticket_views.xml",
        "views/res_partner_views.xml",
        "wizard/whatsapp_compose_message_views.xml",
        "wizard/whatsapp_invite_member_views.xml",
        "views/res_config_settings_views.xml",
        "views/whatsapp_menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "ibq_whatsapp_helpdesk/static/src/dashboard/**/*.scss",
            "ibq_whatsapp_helpdesk/static/src/dashboard/**/*.js",
            "ibq_whatsapp_helpdesk/static/src/dashboard/**/*.xml",
        ],
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}
