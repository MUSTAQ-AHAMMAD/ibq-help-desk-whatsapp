# IBQ WhatsApp Helpdesk (Twilio)

A two-way WhatsApp channel for the Odoo Helpdesk, powered by the Twilio
Programmable Messaging API. Customers message your WhatsApp number, a
configurable bot triages them, a ticket is opened, and agents answer straight
from the ticket chatter.

- **Module:** `ibq_whatsapp_helpdesk`
- **Odoo:** 17.0 (see [Other Odoo versions](#other-odoo-versions))
- **Version:** 17.0.2.0.0
- **Depends:** `base`, `web`, `bus`, `mail`, `contacts`, `helpdesk`
- **Python:** `requests`

---

## What it does

| Capability | Where |
|---|---|
| Dashboard: six tabs — overview, monitoring, inbox, reports, contacts, team | **WhatsApp ▸ Dashboard** |
| Four roles (Owner / Administrator / Supervisor / Agent) driving Odoo groups | **WhatsApp ▸ Team ▸ Team Members** |
| Saved replies inserted with `/shortcut` | **Configuration ▸ Saved Replies** |
| Conversation tags, priority and internal notes | Dashboard inbox |
| The whole chat recorded on the ticket, transcript and chatter | Helpdesk ticket form |
| Repeated vs one-off issue analysis | **WhatsApp ▸ Reporting ▸ Issues** |
| Assign a chat by texting `#assign 1042 sue` from your own phone | Any agent's WhatsApp |
| Customer satisfaction, asked automatically at close | **Reporting ▸ Satisfaction** |
| Blocklist — inbound dropped before anything is created | **Configuration ▸ Blocked Numbers** |
| Multi-account Twilio config, credential test button | **WhatsApp ▸ Configuration ▸ WhatsApp Accounts** |
| Signed inbound webhook (`X-Twilio-Signature`, HMAC-SHA1) | `/whatsapp/twilio/inbound` |
| Delivery receipts (sent → delivered → read → failed) | `/whatsapp/twilio/status` |
| Conversations threaded per number, linked to contact + ticket | **WhatsApp ▸ Inbox ▸ Conversations** |
| No-code bot: menus, free-text questions, ticket creation, hand-off | **WhatsApp ▸ Configuration ▸ Bot Flows** |
| Approved templates with `{{1}}`-style variables mapped to record fields | **WhatsApp ▸ Configuration ▸ Templates** |
| Agent replies relayed from the ticket chatter | Helpdesk ticket form |
| Stage-change notifications | Helpdesk stage form |
| Outbound queue with retries, failure inbox | **WhatsApp ▸ Inbox ▸ Failed Messages** |
| Inbound media pulled from Twilio and attached | On each message |

### How a chat becomes a ticket

```
Customer  ──▶  Twilio  ──▶  POST /whatsapp/twilio/inbound
                                   │
                          signature verified
                                   │
                    whatsapp.conversation (per number)
                                   │
                        ┌──────────┴──────────┐
                     bot flow            "AGENT" keyword
                        │                     │
              menu ▸ questions ▸ ticket ──────┴──▶ helpdesk.ticket
                                                        │
                        agent replies in chatter ───────┘
                                   │
                        Twilio REST ──▶ Customer
```

The bot is a small state machine. The conversation stores a pointer to the
current step and a JSON map of answers; each inbound message advances the
pointer. Customers can escape at any point by typing one of the flow's
keywords (`agent`, `stop`, `menu` by default).

---

## Install

1. Drop `ibq_whatsapp_helpdesk/` into your Odoo addons path.
2. `pip install requests` if it is not already present.
3. Update the apps list, then install **IBQ WhatsApp Helpdesk (Twilio)**.

```bash
python odoo-bin -c odoo.conf -u ibq_whatsapp_helpdesk -d YOUR_DB --stop-after-init
```

## Configure

### 1. Twilio credentials

**WhatsApp ▸ Configuration ▸ WhatsApp Accounts ▸ New**

| Field | Where to find it |
|---|---|
| Account SID | Twilio Console ▸ Account Info (`AC…`) |
| Auth Token | Twilio Console ▸ Account Info |
| WhatsApp Sender | The WhatsApp-enabled number in E.164, e.g. `+14155238886` (the sandbox number while testing) |
| Messaging Service SID | Use instead of a fixed sender (`MG…`) |

Hit **Test Connection**. A green *Connected* badge means the credentials work.

> The Auth Token is stored in `whatsapp.account.auth_token` and readable by the
> WhatsApp Administrator group. Treat that group as privileged. Odoo does not
> encrypt the column, so a database dump exposes it — rotate the token in
> Twilio if a dump leaves your control.

### 2. Webhooks

The account form shows the two URLs to paste into Twilio:

```
https://your-odoo.example.com/whatsapp/twilio/inbound     ← "When a message comes in"
https://your-odoo.example.com/whatsapp/twilio/status      ← "Status callback URL"
```

Both must be **POST**. Set them on the WhatsApp sender (Messaging ▸ Senders) or
on the Messaging Service, whichever you configured.

Signature verification compares against the URL Twilio actually called. Behind
a reverse proxy, either enable Odoo's `proxy_mode = True` or fill **Public Base
URL** on the account. While developing over ngrok, put the tunnel URL there.

Check the tunnel reaches Odoo:

```bash
curl https://your-tunnel.ngrok-free.app/whatsapp/twilio/health
```

### 3. Bot flow

A working starter flow, **IBQ Support (default)**, ships with the module:
greeting → 4-option menu → describe the problem → extra detail → ticket →
agent. Edit the wording, or build your own from these step types:

| Step type | Behaviour |
|---|---|
| Send a message | Sends the body, moves straight on |
| Ask a multiple choice | Sends body + numbered options, waits for a pick (matches the digit, the label, or any keyword) |
| Ask and store an answer | Stores the reply under an answer key; optional email/digits validation |
| Create a helpdesk ticket | Opens the ticket from the answers collected so far |
| Hand over to an agent | Stops the bot, flags the chat for a human |
| Close the conversation | Sends a sign-off and closes |

Bodies support `{name}`, `{number}`, `{ticket_ref}` and any answer key you
collected earlier, e.g. `{subject}`.

Assign the flow on the account. Leave it empty to send every chat straight to
an agent.

### 4. Templates

WhatsApp only allows free-form text within **24 hours** of the customer's last
message. Anything proactive — acknowledgements, stage updates, CSAT — must be
an approved template.

1. Build it in Twilio ▸ Content Template Builder, submit for approval.
2. Create the matching record here with the **same body**, and paste the
   Content SID (`HX…`).
3. Map each `{{N}}` to a field path (`partner_id.name`, `stage_id.name`, `id`).

Four templates are pre-created as drafts: `ticket_created`, `agent_handoff`,
`ticket_stage_update`, `ticket_closed`. They need your Content SIDs before
they will deliver outside the session window.

The composer and the chatter relay both tell you when the window has closed
rather than silently dropping the message.

### 5. Helpdesk wiring

- **Team form ▸ WhatsApp** — pick the sending account, toggle chatter relay.
- **Stage form ▸ WhatsApp** — pick a template to fire when a ticket lands in
  that stage, and whether to close the chat.
- **Settings ▸ WhatsApp** — default account, idle auto-close, whether a
  customer reply raises an activity, raw webhook logging.

---

## Roles

Four roles, set on the roster. A role decides what the dashboard offers, what
the server accepts, **and** which Odoo security group the person holds — one
place to manage, no second thing to keep in sync.

| Role | Chats they see | Can also |
|---|---|---|
| **Owner** | Everything | Transfer ownership, promote Administrators, delete data. Exactly one, and the first person on an empty roster gets it automatically. |
| **Administrator** | Everything | The roster, roles below their own rank, Twilio accounts, bot flows, templates. |
| **Supervisor** | Every chat in their departments | Reassign and transfer, manage saved replies and tags, block numbers, export reports. |
| **Agent** | Their own chats plus anything unassigned | Reply, tag, take notes, set priority, change their own presence and signature. |

Changing someone's role moves them between `group_whatsapp_user`,
`group_whatsapp_supervisor` and `group_whatsapp_manager`. Removing or archiving
a roster entry revokes all three. Nothing else on the user is touched.

Two guardrails worth knowing:

- An Administrator can manage Supervisors and Agents but **not another
  Administrator or the Owner**, so admins cannot lock each other out.
- Promoting someone to Owner steps the previous Owner down to Administrator in
  the same write. There is never more than one.

The boundary is enforced in three places, not one:

1. The dashboard hides what you cannot do.
2. Every dashboard method that changes something re-checks the right.
3. Record rules scope `whatsapp.conversation` and `whatsapp.message` at the ORM
   level, so an agent reaching those models through a list view or an RPC
   client sees the same slice.

A system administrator who is not on the roster still gets Administrator
rights, or a fresh install would have nobody able to configure it.

## Dashboard

**WhatsApp ▸ Dashboard** is one OWL client action with six tabs and a shared
filter bar — period, department, agent, sender, tags — that follows you between
them.

### Overview

A live strip (waiting, unassigned, with the bot, agents available) where each
count is a button into the relevant tab. Then nine KPIs against the selected
window — today / 7 / 30 / 90 days / 12 months — each showing its movement
against the equal period before:

| KPI | Meaning |
|---|---|
| Conversations | Chats started in the period |
| Messages in / out | Volume either way |
| Tickets opened | Helpdesk tickets that came from WhatsApp |
| First response | Average from hand-off to the first agent reply |
| Satisfaction | Average rating as a percentage, with the response count |
| Handled by bot | Share of closed chats that never needed a human |
| Time to close | Average from first message to closed |
| Failed sends | Messages Twilio rejected |

Below that, a daily inbound/outbound bar chart and the current state mix.

The timing figures are stamped on the conversation as the events happen
(`handoff_date`, `first_agent_reply_date`, `first_response_seconds`,
`closed_date`, `resolution_seconds`, `bot_resolved`), so the dashboard
aggregates real columns rather than walking messages on every load.

### Monitoring

A wallboard for a second screen: three columns — Waiting, In progress, With the
bot — each card showing age, priority and tags, shifting from grey to amber to
red as it sits. Unassigned cards carry a **Claim** button. Beside them, an
on-shift panel with every agent's presence and load bar. Refreshes every 20
seconds and on every bus event.

### Inbox

The agent console: queue on the left, thread in the middle, context on the
right.

- Six scopes with live counts, plus a search that also matches message bodies.
- **Saved replies** — type `/` in the composer to filter by shortcut, or press
  the `/` button. Placeholders (`{name}`, `{order_ref}`, `{agent}`) are filled
  from the conversation before insertion, and usage is counted so the ones
  people actually use float to the top.
- **Tags** and **priority**, both reflected in the queue ordering.
- **Internal notes** in their own panel. They live on the conversation's Odoo
  chatter and can never become a WhatsApp message.
- **Transfer** to another agent, another department, or both, leaving a note
  saying who moved it and where.
- Enter sends, Shift+Enter breaks the line. Replying to a chat still with the
  bot takes it over and assigns it to you.
- Outside the 24h window the composer disables itself and says why.

### Reports

- **Agent performance** — chats handled, average first response, average time
  to close, ratings received, CSAT, currently open.
- **When the queue is busy** — a weekday × hour heatmap over inbound volume,
  with the busiest slot called out. The staffing question, answered.
- **How long customers wait** — first responses bucketed into under 1 min,
  1–5, 5–15, 15–60, and over an hour.
- **Satisfaction** — the 1–5 distribution, the average, the happy share, and
  any comments customers left.
- **What chats are about** — the tag breakdown, plus how many chats carry no
  tag at all.
- **By department** — volume and average first response per department.

Every table exports to CSV, plus a raw conversation export (one row per chat
with its timings, tags, agent and rating, capped at 5000 rows). Exports are
written as an attachment and fetched through Odoo's normal content route.

### Contacts

Everyone who has ever written in, grouped by number rather than by contact — an
unknown number has no partner record yet but is still someone the team talked
to. Chat count, tags, last rating, last seen, and jumps into the chat or the
Odoo contact. Supervisors and above get a **Block** action and a second tab
listing the blocklist with how many messages each entry has dropped.

### Team

The roster, with role counts across the top and a row per person: presence,
open and waiting chats, a load bar, capacity, auto-assign, and the departments
they cover. The role dropdown only offers what your own role may grant.
**Add team members** picks several users at once with a role and departments;
**Full form** opens the same thing as a wizard from the Team menu.

**Routing.** When a chat reaches a human, `whatsapp.agent._route()` picks the
least loaded agent who is *Available*, accepts routed chats, is under capacity,
and covers the chat's department and sender. If nobody qualifies the chat stays
unassigned rather than landing on someone already full.

## Agent tooling

### Saved replies

`whatsapp.canned.response`. A shortcut (`order-status`), a title, a body with
placeholders, and an optional department restriction. Leave **Private to** empty
to share it with the team: agents may keep private ones, but only a Supervisor
or above can create or edit a shared one — enforced by both the role check and
a record rule that splits read from write.

### Tags

`whatsapp.tag`. Colour, optional description, optional department restriction.
Tags are what make the reports say something; without them the only breakdown
available is by agent or department.

### Satisfaction

When a chat **an agent actually handled** is closed and the session is still
open, the customer is asked to reply 1–5. The next numeric reply becomes a
`whatsapp.rating` instead of reopening the chat; anything else means they want
to keep talking, so the chat reopens as normal. Bot-only chats are never
polled, and nobody is polled twice. Turn it off per sender with **Ask for a
Rating** on the account.

### Blocklist

`whatsapp.blocklist`. Checked before a conversation is created, so a blocked
number cannot open tickets, trigger the bot, or reach the queue. Nothing is
sent back — a reply would confirm the number is live. Each entry counts how
many messages it has dropped.

## Assigning a chat

Assignment happens in one place in the code — `whatsapp.conversation.assign_to()`
— so it behaves identically wherever you trigger it, and the ticket always
follows. Four ways in:

| From | How |
|---|---|
| The dashboard | The agent dropdown in the thread header, **Take over**, or **Transfer** |
| The chat list | **Assigned Agent** is multi-editable — select several chats, set the agent once |
| The chat form | The **Assign to Me** button, or edit the field |
| **A text message** | `#assign 1042 sue` from an agent's own WhatsApp number |

Three things happen on every assignment, whichever door you came through:

1. The linked **ticket** is reassigned to the same person. (Before, only the
   dashboard did this and the form view silently did not.)
2. A note lands on the ticket saying who assigned it, to whom, and from where.
3. The **customer is told**, by name — *"Sara is taking over from here."*

The customer message is skipped when the chat is still with the bot (the
hand-off has its own line), when the agent has not actually changed, and when
the 24-hour session has expired — a routing detail is not worth burning an
approved template on. If the agent has a **Shown to Customers** alias, that is
the name used.

You cannot assign a chat to someone with no WhatsApp access; a ticket sitting
with somebody who cannot open it helps nobody. Add them to the roster first.

### Commands by text

Put an agent's own WhatsApp number in **Their WhatsApp Number** on their roster
entry, and they can run the queue from their phone by texting the support
number:

```
#queue                     chats waiting for an answer
#mine                      chats assigned to you
#who 1042                  who owns a chat
#take 1042                 assign it to yourself
#assign 1042 sue           assign it to someone else
#note 1042 rang them back  internal note, never sent to the customer
#close 1042                close the chat
#tag 1042 billing          add a tag
#status available|busy|away|offline
#team                      who is on shift
#help                      this list
```

The chat can be named by **ticket number** or by the **customer's number**, so
`#assign +971501234567 sue` works too. Agents are matched on login or name;
an ambiguous name gets a "be more specific" reply rather than a guess.

Four properties this design holds to:

- **A command never becomes a conversation.** It is recognised before any
  customer handling, so it never enters the queue and never opens a ticket.
- **A command never reaches a customer.** Replies go back to the agent's own
  number only.
- **The role still decides.** Every command runs *as that agent's Odoo user*,
  so the same roles, record rules and rights apply. An Agent texting
  `#assign` to hand a chat to someone else gets *"Your WhatsApp role does not
  allow this (reassign)"* — the same refusal the dashboard gives them.
- **Only `#` means a command.** An agent who is also a customer can text
  normally; anything not starting with `#` follows the ordinary chat path.

Commands and their replies are logged as messages with no conversation, so they
are auditable under **Inbox ▸ Messages ▸ Agent Commands** without cluttering
the queue. Turn the whole thing off per sender with **Agent Commands** on the
account.

> Recognition is by phone number, so it is only as strong as the sender's
> control of their WhatsApp account. Combined with signature verification this
> is fine for queue operations; do not extend the command set to anything you
> would not let someone do from an unlocked phone.

## The conversation on the ticket

Every WhatsApp message is kept against the ticket in two places, because they
answer different questions.

**A transcript on the ticket form** — the whole exchange, customer, bot and
agent, in order, with delivery state on each line. This is the "what was
actually said" view, and it is always complete.

**The chatter** — the same exchange as ticket history, attributed to whoever
said it, so it sits alongside stage changes and assignments in one timeline.
How much of it lands here is a per-department setting, **Log WhatsApp in the
Chatter**:

| Setting | What goes in the chatter |
|---|---|
| Every message, including the bot *(default)* | The complete exchange |
| Customer and agent messages only | Skips the menus and canned bot lines |
| Do not log to the chatter | Nothing — the transcript on the ticket still has it all |

Mirroring is **idempotent**: each WhatsApp message remembers the chatter entry
it produced, so re-running it never duplicates history. When a ticket gets
linked to a chat after the fact — an import, a manual fix — the ticket shows a
**Sync to Chatter** button, and a green tick once everything is in.

The messages are escaped before they become chatter HTML, so a customer writing
`<script>` gets a customer writing `<script>`, not markup that runs.

## Reporting on issues

Tags say what a chat was *filed under*. An **issue** says what it was *about*.
Two people writing "the office printer is jammed" and "printer jammed again,
3rd floor" have one problem between them, and **Reporting ▸ Issues** is where
you see that.

The dashboard's Reports tab leads with four numbers:

| | |
|---|---|
| **Distinct issues** | How many different problems, not how many tickets |
| **Repeated** | Raised more than once, and what share of all chats they account for |
| **One-offs** | Raised once — the long tail |
| **Customers who came back** | People who contacted you more than once, which is a different question |

Then a table of every issue with how often it was raised, how many different
customers hit it, how many are still open, average time to close, and average
rating. Sort by *Raised* to find what is worth fixing at the source rather than
answering again. It exports to CSV like every other report.

### How chats are grouped

Deliberately plain, and deliberately inspectable:

1. Take the subject — the bot's `subject` answer, else the ticket title, else
   the first inbound message with something in it.
2. Normalise it: lowercase, drop punctuation, **drop all digits** (so "invoice
   4471" and "invoice 5120" are one issue, not two), drop stopwords and
   attention-getters like *anyone* and *hello*.
3. Compare the remaining word set against existing issues by Jaccard overlap.
   At **55%** or more it joins that issue; below, it starts a new one.

A chat with fewer than two significant words is left unclassified rather than
inventing an issue out of "anyone there?". The Reports panel says how many
those were.

There is no model to train and nothing to tune blindly — you can read the
matched words on any issue. Where it splits something a human can see is one
thing, select both rows and use **Merge issues**; the vocabularies are combined
so it stays merged.

Each issue has a **note** field. That is the point of grouping them: work out
the fix once, write it down, and the next person to pick up the same chat has
it.

Classification runs when a ticket is created and again on close, with a
half-hourly cron catching anything missed.

## Menus

Each entry is gated by the role that can use it, so an agent's menu is short
and a supervisor's is not.

```
WhatsApp
├── Dashboard                        everyone
├── Inbox
│   ├── Waiting for a Reply
│   ├── Conversations
│   ├── Messages
│   └── Failed Messages
├── Team
│   ├── Team Members                 the roster, list + kanban + form
│   ├── Add Team Members             wizard: users, role, departments, capacity
│   └── Departments                  supervisor and above
├── Reporting                        supervisor and above
│   ├── Conversation Analysis        graph and pivot over the real columns
│   └── Satisfaction
└── Configuration                    supervisor and above
    ├── WhatsApp Accounts            administrator
    ├── Saved Replies
    ├── Tags
    ├── Blocked Numbers
    ├── Bot Flows                    administrator
    ├── Bot Steps                    administrator
    └── Templates                    administrator
```

**Team ▸ Add Team Members** is the menu-driven version of the dashboard's Team
tab: pick several users, give them a role, restrict them to departments and
senders, set a capacity and a starting presence, and optionally notify them in
Odoo. Only the Owner can hand out Administrator, from either place.

## Running the demo

A throwaway stack that boots the module with realistic traffic, so you can
click the dashboard without wiring up Twilio.

```bash
docker compose -f demo/docker-compose.yml up -d
```

Then create the database and install:

```bash
docker compose -f demo/docker-compose.yml stop odoo
```

```bash
docker compose -f demo/docker-compose.yml run --rm --no-deps odoo odoo -d demo --db_host=db --db_user=odoo --db_password=odoo --addons-path=/usr/lib/python3/dist-packages/odoo/addons,/mnt/demo-addons,/mnt/project -i base,helpdesk,ibq_whatsapp_helpdesk --without-demo=all --stop-after-init
```

Seed it with 8 agents across the four roles, ~70 conversations, ~700 messages,
tickets, tags, ratings, saved replies and a blocklist:

```bash
docker compose -f demo/docker-compose.yml run --rm --no-deps -T odoo odoo shell -d demo --db_host=db --db_user=odoo --db_password=odoo --addons-path=/usr/lib/python3/dist-packages/odoo/addons,/mnt/demo-addons,/mnt/project < demo/seed.py
```

```bash
docker compose -f demo/docker-compose.yml start odoo
```

Open <http://localhost:8069> as **admin / admin** and go to **WhatsApp ▸
Dashboard**. To see the role boundary from the other side, log in as
`alex / alex` (Agent), `sue / sue` (Supervisor) or `olive / olive` (Owner) —
the tab strip, the menus and the roster controls all change.

### What the demo stack is, and is not

`helpdesk` is an Odoo **Enterprise** app and is not in the Community image, so
`demo/addons/helpdesk` is a deliberately minimal stub: three models, the fields
this module actually reads, and the three view IDs it inherits — named exactly
as Enterprise names them, so the same module installs against both. It is not a
helpdesk and must never be installed on a real database; on Enterprise the
genuine app provides all of this and much more.

Nothing in the demo touches the network. `demo/seed.py` monkey-patches the
Twilio call to a stub, so no message is ever sent and no credentials are
needed. Every name, number and conversation in it is invented.

Timestamps are back-dated so the charts have a shape. Chats that are still open
are placed inside the idle window on purpose — otherwise the *close idle
conversations* cron does its job on the next run and the queue empties.

## Day to day

- **WhatsApp ▸ Dashboard ▸ Inbox** is where agents actually work.
- **WhatsApp ▸ Dashboard ▸ Monitoring** is the supervisor's wallboard.
- **WhatsApp ▸ Inbox ▸ Waiting for a Reply** is the same queue as a list view.
- Replying in a ticket's chatter with a **public message** sends it to the
  customer. Internal notes stay internal.
- Every WhatsApp message is mirrored into the ticket chatter as a note, so the
  ticket remains the single history.
- **Failed Messages** lists whatever Twilio refused, with its error code, and a
  retry button.

## Scheduled actions

| Cron | Every | Does |
|---|---|---|
| WhatsApp: send queued messages | 2 min | Retries anything still queued or failed (max 3 attempts) |
| WhatsApp: close idle conversations | 1 h | Closes chats untouched for `idle_close_hours` (default 48) |
| WhatsApp: classify issues | 30 min | Groups any chat that has no issue yet |

The idle-close cron is more aggressive than it looks: any chat whose last
message is older than the window gets closed, including ones an agent still
considers open. Raise `idle_close_hours` in **Settings ▸ WhatsApp** if your
team works threads over several days.

## Security

Three nested groups under the **WhatsApp** category, driven by the role on the
roster (see [Roles](#roles)):

| Group | Held by | Grants |
|---|---|---|
| `group_whatsapp_user` | Agents | Their own chats and unassigned ones, saved replies they own |
| `group_whatsapp_supervisor` | Supervisors | Every chat, the shared reply library, tags, the blocklist |
| `group_whatsapp_manager` | Owner and Administrators | The roster, accounts, bot flows, templates |

Helpdesk User implies the agent group and Helpdesk Manager the administrator
group, so existing staff land somewhere sensible on install.

Record rules do the rest:

- Conversations and messages are scoped to own-and-unassigned for agents, and
  opened up for supervisors and above.
- Saved replies split read from write: an agent reads the shared library but
  can only edit the ones they own.
- Multi-company rules scope accounts, conversations and messages by the
  account's company.

Inbound requests are rejected unless the `X-Twilio-Signature` HMAC matches, and
duplicate `MessageSid` values are ignored so Twilio's retries never replay the
bot. Blocked numbers are dropped before a conversation exists. Leave **Verify
Webhook Signature** on outside of local debugging.

## Testing without a real WhatsApp number

Twilio's WhatsApp sandbox works end to end:

1. Twilio Console ▸ Messaging ▸ Try it out ▸ Send a WhatsApp message.
2. Join the sandbox from your phone (`join <two-words>` to the sandbox number).
3. Use the sandbox number as the account's **WhatsApp Sender**.
4. Point the sandbox's inbound webhook at your ngrok URL.
5. Message the sandbox number — a conversation should appear within a second.

Sandbox chats are session-only; templates and Content SIDs still need a real
sender.

### Unit tests

```bash
python odoo-bin -c odoo.conf -d YOUR_DB -i ibq_whatsapp_helpdesk --test-enable --stop-after-init
```

151 tests across five files, all stubbing the Twilio call. They pass against
Odoo 17 Community with the demo stack:

```
odoo.tests.result: 0 failed, 0 error(s) of 151 tests
```

`tests/test_whatsapp_helpdesk.py` (16) — number normalisation, signature
validation *and* tampering, the whole bot path from greeting to ticket, keyword
escapes, session-window enforcement, status callbacks, template rendering, and
the chatter relay including that internal notes are not leaked to the customer.

`tests/test_whatsapp_dashboard.py` (26) — agent routing under every skip
condition, the service-time metrics, the statistics API on both an empty and a
busy database, queue scoping and search, and sending from the console.

`tests/test_whatsapp_assign.py` (31) — assignment from every door: that the
ticket follows the chat in all of them, that the customer is told by name and
only when it is worth telling them, and the texted commands — including that a
command never becomes a conversation, never reaches a customer, and is refused
for an Agent trying to reassign someone else's chat.

`tests/test_whatsapp_issue.py` (23) — the ticket transcript and issue
grouping: that mirroring never duplicates, that the log mode is respected, that
a customer's `<script>` is escaped rather than rendered, that line breaks
survive into the chatter, that filler like "anyone there?" never becomes an
issue, and that a chat created in the current second still falls inside its own
reporting window.

`tests/test_whatsapp_roles.py` (55) — the permission boundary, which is the
part worth testing hardest. Role-to-group mapping including promotion,
demotion, removal and archiving; single-owner enforcement and ownership
transfer; what each role may and may not do; that an agent's chat list, KPIs
and message counts are all scoped to them; supervisors limited to their
departments; private versus shared saved replies; tag permissions; the CSAT
flow including that a non-numeric reply reopens instead of rating; the
blocklist dropping inbound before anything is created; notes staying internal;
transfers leaving a trail; report shapes and bucket boundaries; CSV export; and
the invite wizard.

---

## Other Odoo versions

Written against **Odoo 17.0**. Two things to check when porting:

**Odoo 18+** renamed `<tree>` to `<list>`. The old tag still loads with a
deprecation warning; rename them in `views/` for a clean run. Odoo 18 also
renamed `check_access_rights` / `check_access_rule` to `check_access` — the one
call site is `get_conversation()` in `models/whatsapp_dashboard.py`.

**Helpdesk view IDs.** Three inherited views are referenced by XML ID. They are
stable across 16–18, but verify before installing on anything else:

```bash
grep -rn "helpdesk_ticket_view_form\|helpdesk_team_view_form\|helpdesk_stage_view_form" /path/to/enterprise/helpdesk/views/
```

If a name differs, update `views/helpdesk_ticket_views.xml`.

**Odoo Community.** `helpdesk` is an Enterprise app. To run this on OCA's
`helpdesk_mgmt` instead, change three things:

1. `__manifest__.py` — swap the `helpdesk` dependency for `helpdesk_mgmt`.
2. `models/whatsapp_conversation.py` — `_ticket_values()` and `_ensure_ticket()`
   are the only places a ticket is created; retarget the model and field names
   there.
3. `views/helpdesk_ticket_views.xml` — repoint the three `inherit_id` refs.

Everything else (accounts, conversations, messages, bot, templates, webhook) is
independent of the helpdesk app.

---

## Layout

```
ibq_whatsapp_helpdesk/
├── controllers/twilio_webhook.py     inbound + status endpoints, signature gate
├── models/
│   ├── whatsapp_account.py           Twilio credentials, REST transport, HMAC, intake
│   ├── whatsapp_conversation.py      threading, session window, the bot engine
│   ├── whatsapp_message.py           outbound queue, retries, delivery status, media
│   ├── whatsapp_template.py          approved templates + variable mapping
│   ├── whatsapp_bot.py               flows, steps, menu options
│   ├── whatsapp_agent.py             roster, roles, group sync, routing
│   ├── whatsapp_command.py           #assign / #take / #close by text
│   ├── whatsapp_issue.py             grouping chats into repeated issues
│   ├── whatsapp_dashboard.py         RPC surface: stats, reports, exports
│   ├── whatsapp_tag.py               conversation labels
│   ├── whatsapp_canned_response.py   saved replies and /shortcuts
│   ├── whatsapp_rating.py            CSAT scores and sentiment
│   ├── whatsapp_blocklist.py         numbers dropped on arrival
│   ├── helpdesk_ticket.py            chatter relay, stage notifications
│   ├── res_partner.py                number ↔ contact matching
│   └── res_config_settings.py        settings panel
├── static/src/dashboard/
│   ├── whatsapp_dashboard.js         client action root
│   ├── whatsapp_dashboard.xml        OWL templates
│   ├── whatsapp_dashboard.scss       scoped tokens, light + dark
│   └── components/                   tiles, charts, heatmap, board,
│                                     console, reports, contacts, roster
├── wizard/whatsapp_compose_message.py  agent composer
├── wizard/whatsapp_invite_member.py    add team members from a menu
├── data/whatsapp_bot_data.xml        starter flow + draft templates
├── security/                         groups, ACLs, multi-company rules
└── tests/
```

## Known limits

- Outbound sends happen inline during the webhook request. Under heavy load,
  disable that by queueing only (set the messages to `outgoing` and let the
  2-minute cron drain them).
- Template approval status is recorded by hand; the module does not poll
  Twilio's Content API for it.
- One conversation per number per account. A customer messaging two of your
  senders gets two threads, by design.
- Numbers are matched to contacts by the last 9 digits, which can collide in
  small national numbering plans. Set **Unknown Numbers** to *Keep the number
  only* if that matters to you.

## License

LGPL-3.
