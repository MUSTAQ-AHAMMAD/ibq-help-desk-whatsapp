# -*- coding: utf-8 -*-
"""Fill the demo database with enough traffic that the dashboard says something.

Run inside the container:

    docker compose -f demo/docker-compose.yml exec odoo \\
        odoo shell -d demo --db_host=db --db_user=odoo --db_password=odoo \\
        --addons-path=/usr/lib/python3/dist-packages/odoo/addons,/mnt/demo-addons,/mnt/project \\
        < /mnt/project/demo/seed.py

Everything here is invented: fake names, fake numbers, no Twilio call is made.
Outbound sends are monkey-patched to a stub so the seed never touches the
network, and the timestamps are back-dated so the charts have a shape.
"""
import random
from datetime import datetime, timedelta

from odoo import fields

random.seed(20260825)          # reproducible demo, same picture every run

NOW = fields.Datetime.now()
DAYS = 21

FIRST_NAMES = [
    "Mustaq", "Layla", "Omar", "Fatima", "Rashid", "Noor", "Hassan", "Amina",
    "Yusuf", "Sara", "Khalid", "Mariam", "Tariq", "Huda", "Zaid", "Salma",
    "Bilal", "Rania", "Faisal", "Dana", "Nabil", "Iman", "Adel", "Lina",
]
LAST_NAMES = [
    "Tapal", "Haddad", "Nasser", "Rahman", "Al Balushi", "Farouk", "Idrissi",
    "Suleiman", "Mansour", "Aziz", "Qureshi", "Darwish",
]

SUBJECTS = [
    ("Technical issue", "printer",
     ["The office printer is jammed", "Wi-Fi keeps dropping on the 3rd floor",
      "My laptop will not connect to the VPN", "Screen flickers after the update",
      "Cannot log in to the portal", "The scanner shows error E-042"]),
    ("Billing or invoice", "billing",
     ["Invoice 4471 shows the wrong VAT", "I was charged twice this month",
      "Can I get a copy of last quarter's invoices?",
      "The payment link expired", "Refund for the cancelled order"]),
    ("Delivery", "delivery",
     ["Order SO4471 has not arrived", "The courier left without ringing",
      "Wrong item delivered", "Can I change the delivery address?"]),
    ("Account", "account",
     ["Please add a user to our account", "Reset the admin password",
      "How do I change the billing contact?"]),
]

AGENT_REPLIES = [
    "Thanks for flagging that — looking into it now.",
    "I have escalated this to the on-site team, someone will be up shortly.",
    "Good news: that is fixed on our side. Could you try again?",
    "Sorry about that. I have issued the correction, you will see it within a day.",
    "I can see the order in our system — it is out for delivery today.",
    "Done. Anything else I can help with?",
]

CUSTOMER_FOLLOWUPS = [
    "Thanks, that worked",
    "Still not working I am afraid",
    "Perfect, appreciated",
    "Any update on this?",
    "Yes that fixed it, thank you",
]


def stub_send(self, to_number, **kwargs):
    """Stand in for the Twilio REST call. Nothing leaves the machine."""
    return {"sid": "SM%030d" % random.randrange(10 ** 20),
            "status": "delivered", "_ok": True, "_http_status": 201}


def back_date(records, when):
    """Move create_date, which the ORM will not let us write normally."""
    if not records:
        return
    env.cr.execute(
        "UPDATE %s SET create_date = %%s WHERE id IN %%s" % records._table,
        (when, tuple(records.ids)),
    )


def run(env):
    from odoo.addons.ibq_whatsapp_helpdesk.models import whatsapp_account

    original_send = whatsapp_account.WhatsappAccount._send_raw
    whatsapp_account.WhatsappAccount._send_raw = stub_send
    try:
        _build(env)
    finally:
        whatsapp_account.WhatsappAccount._send_raw = original_send


def _build(env):
    print("Seeding demo data...")

    # ---------------------------------------------------------------- teams
    teams = {}
    for name in ("Technical Support", "Billing", "Logistics"):
        teams[name] = env["helpdesk.team"].search([("name", "=", name)], limit=1) or \
            env["helpdesk.team"].create({"name": name})

    stages = {}
    for index, name in enumerate(("New", "In Progress", "Waiting", "Solved")):
        stages[name] = env["helpdesk.stage"].search([("name", "=", name)], limit=1) or \
            env["helpdesk.stage"].create({"name": name, "sequence": index * 10,
                                          "fold": name == "Solved"})

    # ---------------------------------------------------------------- people
    roster = [
        ("Olive Ahmed", "olive", "owner", "available", 6, []),
        ("Adam Kareem", "adam", "admin", "available", 5, []),
        ("Sue Iqbal", "sue", "supervisor", "available", 5, ["Technical Support"]),
        ("Alex Rahim", "alex", "agent", "available", 4, ["Technical Support"]),
        ("Nadia Salem", "nadia", "agent", "available", 4, ["Billing"]),
        ("Rami Dawood", "rami", "agent", "busy", 3, ["Logistics"]),
        ("Hana Youssef", "hana", "agent", "away", 4, []),
        ("Zain Aslam", "zain", "agent", "offline", 5, []),
    ]
    agents = {}
    for name, login, role, status, capacity, team_names in roster:
        user = env["res.users"].search([("login", "=", login)], limit=1)
        if not user:
            user = env["res.users"].create({
                "name": name, "login": login, "password": login,
                "email": "%s@ibq.example" % login,
            })
        agent = env["whatsapp.agent"].search([("user_id", "=", user.id)], limit=1)
        values = {
            "role": role, "status": status, "max_active_chats": capacity,
            "team_ids": [(6, 0, [teams[t].id for t in team_names])],
        }
        if agent:
            agent.write(values)
        else:
            agent = env["whatsapp.agent"].create(dict(values, user_id=user.id))
        agents[login] = agent
    print("  %s agents" % len(agents))

    # ---------------------------------------------------------------- tags
    tag_specs = [
        ("Bug", 1, "Something is broken"),
        ("How-to", 4, "The customer needs guidance"),
        ("Billing", 3, "Invoices, payments, refunds"),
        ("Delivery", 5, "Shipping and couriers"),
        ("Escalation", 2, "Needs a second pair of eyes"),
        ("Feature request", 10, "Not a fault, a wish"),
    ]
    tags = {}
    for name, color, description in tag_specs:
        tags[name] = env["whatsapp.tag"].search([("name", "=", name)], limit=1) or \
            env["whatsapp.tag"].create({"name": name, "color": color,
                                        "description": description})

    # ---------------------------------------------------------------- replies
    canned = [
        ("thanks", "Thanks for waiting",
         "Thanks for your patience {name}, I am on it."),
        ("toner", "Printer toner on the way",
         "A replacement toner is on its way to you now, {name}."),
        ("invoice", "Invoice correction",
         "I have corrected invoice {order_ref} and resent it to you."),
        ("hours", "Opening hours",
         "We are open Sunday to Thursday, 08:00 to 18:00 Gulf time."),
        ("escalate", "Escalated",
         "I have escalated this to our specialists. Someone will reply here shortly."),
    ]
    for shortcut, title, body in canned:
        if not env["whatsapp.canned.response"].search([("shortcut", "=", shortcut)]):
            env["whatsapp.canned.response"].create({
                "shortcut": shortcut, "name": title, "body": body,
                "usage_count": random.randint(2, 40),
            })

    # ---------------------------------------------------------------- account
    account = env["whatsapp.account"].search([], limit=1)
    if not account:
        flow = env.ref("ibq_whatsapp_helpdesk.bot_flow_support",
                       raise_if_not_found=False)
        account = env["whatsapp.account"].create({
            "name": "IBQ Support Line",
            "account_sid": "AC" + "0" * 32,
            "auth_token": "demo-token-not-real",
            "phone_number": "+14155238886",
            "team_id": teams["Technical Support"].id,
            "bot_flow_id": flow.id if flow else False,
            "verify_signature": False,
            "state": "connected",
        })
    env["ir.config_parameter"].sudo().set_param(
        "ibq_whatsapp.default_account_id", account.id
    )

    # ---------------------------------------------------------------- traffic
    agent_pool = [agents[k] for k in ("alex", "nadia", "rami", "hana", "sue", "zain")]
    conversations = []
    used_numbers = set()

    for index in range(64):
        first = random.choice(FIRST_NAMES)
        last = random.choice(LAST_NAMES)
        number = "+9715%08d" % random.randrange(10 ** 8)
        while number in used_numbers:
            number = "+9715%08d" % random.randrange(10 ** 8)
        used_numbers.add(number)

        category, tag_hint, subjects = random.choice(SUBJECTS)
        subject = random.choice(subjects)

        # A chat that is still open three weeks later is not realistic, and
        # the idle-close cron would close it on the next run anyway. So decide
        # up front: recent chats may stay open, older ones always end closed.
        recent = random.random() < 0.34
        if recent:
            started = NOW - timedelta(hours=random.randint(1, 30))
        else:
            started = NOW - timedelta(days=random.randint(2, DAYS - 1))
            # Weekday-daytime bias, so the heatmap has a believable shape.
            while started.weekday() in (4, 5) and random.random() < 0.7:
                started -= timedelta(days=1)
        started = started.replace(
            hour=random.choices(range(24),
                                weights=[1, 1, 1, 1, 1, 2, 4, 7, 12, 16, 18, 15,
                                         11, 13, 16, 14, 10, 7, 5, 4, 3, 2, 2, 1])[0],
            minute=random.randrange(60), second=random.randrange(60),
        )

        partner = env["res.partner"].create({
            "name": "%s %s" % (first, last),
            "mobile": number,
            "email": "%s.%s@example.com" % (first.lower(), last.split()[0].lower()),
        })
        conversation = env["whatsapp.conversation"].create({
            "account_id": account.id,
            "number": number,
            "profile_name": first,
            "partner_id": partner.id,
            "team_id": teams[
                {"printer": "Technical Support", "billing": "Billing",
                 "delivery": "Logistics", "account": "Technical Support"}[tag_hint]
            ].id,
            "priority": random.choices(["0", "1", "2", "3"],
                                       weights=[1, 6, 2, 1])[0],
            "answers": '{"category": "%s", "subject": "%s"}' % (category, subject),
        })
        back_date(conversation, started)

        messages = []

        def say(body, direction, when, is_bot=False, author=None, state=None):
            record = env["whatsapp.message"].create({
                "conversation_id": conversation.id,
                "account_id": account.id,
                "direction": direction,
                "number": number,
                "partner_id": partner.id,
                "body": body,
                "is_bot": is_bot,
                "author_id": author.id if author else False,
                "state": state or ("received" if direction == "inbound" else "delivered"),
            })
            back_date(record, when)
            messages.append(record)
            return record

        cursor = started
        say("Hi", "inbound", cursor)
        cursor += timedelta(seconds=2)
        say("Hello %s! You have reached IBQ Support." % first, "outbound",
            cursor, is_bot=True)
        cursor += timedelta(seconds=1)
        say("What can we help you with today?\n\n1. Technical issue\n"
            "2. Billing or invoice\n3. Check an existing ticket\n4. Talk to an agent",
            "outbound", cursor, is_bot=True)

        # A quarter of chats never need a person: the bot answered them.
        bot_only = random.random() < 0.26
        cursor += timedelta(seconds=random.randint(8, 90))
        say(str(random.randint(1, 4)), "inbound", cursor)
        cursor += timedelta(seconds=2)
        say("Please describe the problem in one line.", "outbound", cursor, is_bot=True)
        cursor += timedelta(seconds=random.randint(10, 180))
        say(subject, "inbound", cursor)

        if bot_only:
            cursor += timedelta(seconds=3)
            say("Here is our guide for that: https://ibq.example/help",
                "outbound", cursor, is_bot=True)
            cursor += timedelta(seconds=random.randint(20, 400))
            say("Great, that answers it. Thanks!", "inbound", cursor)
            closed = cursor + timedelta(seconds=30)
            conversation.write({
                "state": "closed",
                "bot_resolved": True,
                "closed_date": closed,
                "resolution_seconds": int((closed - started).total_seconds()),
                "last_inbound_date": cursor,
                "last_outbound_date": cursor,
                "needs_reply": False,
                "tag_ids": [(6, 0, [tags["How-to"].id])],
            })
            conversations.append(conversation)
            continue

        # Hand-off to a person.
        handoff = cursor + timedelta(seconds=4)
        say("All set, %s. An agent will reply right here." % first,
            "outbound", handoff, is_bot=True)
        agent = random.choice(agent_pool)
        ticket = env["helpdesk.ticket"].create({
            "name": subject,
            "team_id": conversation.team_id.id,
            "partner_id": partner.id,
            "partner_phone": number,
            "user_id": agent.user_id.id,
            "stage_id": stages["In Progress"].id,
            "description": "Opened from WhatsApp %s" % number,
        })
        # The module normally sets this in _ensure_ticket(); the seed builds
        # records directly, so it has to make the same link itself.
        ticket.write({
            "whatsapp_conversation_id": conversation.id,
            "whatsapp_number": number,
        })
        back_date(ticket, handoff)

        # First response: mostly fast, with a believable tail.
        wait = random.choices(
            [random.randint(20, 55), random.randint(60, 290),
             random.randint(300, 890), random.randint(900, 3500),
             random.randint(3600, 20000)],
            weights=[28, 34, 20, 12, 6],
        )[0]
        reply_at = handoff + timedelta(seconds=wait)
        say(random.choice(AGENT_REPLIES), "outbound", reply_at,
            author=agent.user_id)

        cursor = reply_at
        for _turn in range(random.randint(0, 3)):
            cursor += timedelta(minutes=random.randint(1, 45))
            say(random.choice(CUSTOMER_FOLLOWUPS), "inbound", cursor)
            cursor += timedelta(minutes=random.randint(1, 20))
            say(random.choice(AGENT_REPLIES), "outbound", cursor,
                author=agent.user_id)

        chat_tags = [tags[{"printer": "Bug", "billing": "Billing",
                           "delivery": "Delivery", "account": "How-to"}[tag_hint]].id]
        if random.random() < 0.18:
            chat_tags.append(tags["Escalation"].id)

        # One in eight stays unassigned, so the queue and the Monitoring
        # board both show the "nobody has picked this up" case.
        unassigned = random.random() < 0.12
        conversation.write({
            "state": "agent",
            "user_id": False if unassigned else agent.user_id.id,
            "ticket_id": ticket.id,
            "handoff_date": handoff,
            "first_agent_reply_date": reply_at,
            "first_response_seconds": wait,
            "last_inbound_date": cursor,
            "last_outbound_date": cursor,
            "tag_ids": [(6, 0, chat_tags)],
            "needs_reply": False,
        })

        # Older chats always end closed; only the recent ones are still live.
        roll = random.random()
        if not recent or roll < 0.32:
            closed = cursor + timedelta(minutes=random.randint(5, 300))
            conversation.write({
                "state": "closed",
                "closed_date": closed,
                "resolution_seconds": int((closed - started).total_seconds()),
                "bot_resolved": False,
            })
            ticket.stage_id = stages["Solved"]
            if random.random() < 0.62:
                score = random.choices(["5", "4", "3", "2", "1"],
                                       weights=[46, 28, 12, 8, 6])[0]
                # Comment matched to the score: a 5-star review saying "still
                # broken" makes the satisfaction panel read as nonsense.
                comments = {
                    "5": [False, False, "Quick and friendly, thank you",
                          "Sorted in minutes, great service"],
                    "4": [False, False, "Helpful, thanks",
                          "Good, though I had to explain it twice"],
                    "3": [False, "Took a while but sorted in the end",
                          "Fine I suppose"],
                    "2": [False, "Slow to reply",
                          "Had to chase this three times"],
                    "1": ["Still not really fixed", "Nobody got back to me"],
                }[score]
                rating = env["whatsapp.rating"].create({
                    "conversation_id": conversation.id,
                    "user_id": agent.user_id.id,
                    "team_id": conversation.team_id.id,
                    "score": score,
                    "comment": random.choice(comments),
                })
                back_date(rating, closed + timedelta(minutes=3))
                conversation.rating_id = rating
        elif roll < 0.72:
            # Waiting on us: the customer wrote last.
            cursor += timedelta(minutes=random.randint(2, 90))
            say(random.choice(CUSTOMER_FOLLOWUPS), "inbound", cursor)
            conversation.write({"needs_reply": True, "last_inbound_date": cursor})

        conversations.append(conversation)

    # A few chats still with the bot, right now, so Monitoring has a column.
    for index in range(5):
        number = "+9715%08d" % random.randrange(10 ** 8)
        conversation = env["whatsapp.conversation"].create({
            "account_id": account.id, "number": number,
            "profile_name": random.choice(FIRST_NAMES),
            "team_id": teams["Technical Support"].id,
        })
        when = NOW - timedelta(minutes=random.randint(2, 70))
        record = env["whatsapp.message"].create({
            "conversation_id": conversation.id, "account_id": account.id,
            "direction": "inbound", "number": number,
            "body": random.choice(["Hi", "Hello", "I need help", "Anyone there?"]),
            "state": "received",
        })
        back_date(record, when)
        back_date(conversation, when)
        conversation.write({"last_inbound_date": when, "needs_reply": True})

    # Two blocked numbers, with a history of being dropped.
    for number, reason, note in (
        ("+971500000911", "spam", "Bulk marketing, 40+ messages a day"),
        ("+971500000912", "abuse", "Abusive language to two agents"),
    ):
        if not env["whatsapp.blocklist"]._entry_for(number):
            entry = env["whatsapp.blocklist"]._block(number, reason, note)
            entry.sudo().write({"hit_count": random.randint(3, 27)})

    # A couple of failed sends, so the failure inbox is not theoretically empty.
    for conversation in random.sample(conversations, 3):
        record = env["whatsapp.message"].create({
            "conversation_id": conversation.id, "account_id": account.id,
            "direction": "outbound", "number": conversation.number,
            "body": "Just checking in on this one.",
            "state": "failed", "error_code": "63016",
            "error_message": "Failed to send freeform message because you are "
                             "outside the allowed window.",
        })
        back_date(record, NOW - timedelta(days=random.randint(0, 5)))

    # last_message_date is a stored compute over message create_date, and the
    # back-dating above was raw SQL the ORM never saw. Without this every chat
    # would claim its last message arrived the moment the seed ran.
    # invalidate first: the back-dating above was raw SQL, so the ORM cache
    # still holds the create_date values from when the rows were written.
    env.invalidate_all()
    conversations_all = env["whatsapp.conversation"].search([])
    env.add_to_compute(
        env["whatsapp.conversation"]._fields["last_message_date"], conversations_all
    )
    conversations_all.flush_recordset()

    env.cr.commit()
    print("  %s conversations" % env["whatsapp.conversation"].search_count([]))
    print("  %s messages" % env["whatsapp.message"].search_count([]))
    print("  %s tickets" % env["helpdesk.ticket"].search_count([]))
    print("  %s ratings" % env["whatsapp.rating"].search_count([]))
    print("Done. Log in as admin/admin and open WhatsApp > Dashboard.")


run(env)  # noqa: F821 - `env` is provided by `odoo shell`
