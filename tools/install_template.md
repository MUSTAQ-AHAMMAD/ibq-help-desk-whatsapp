# Installing IBQ WhatsApp Helpdesk on Odoo {{SERIES}}

This package is for **Odoo {{SERIES}} only**. Installing the wrong one fails at
install time with a view error — it will not corrupt anything, but it will not
work either. Check `addons/ibq_whatsapp_helpdesk/__manifest__.py`; its `version`
starts with `{{SERIES}}`.

Assumes you already have Odoo running on a server. There is no Docker here.

---

## What is in this package

```
{{NAME}}/
├── INSTALL.md                            this file
├── README.md                             what the addon does, in full
├── LICENSE.txt                           LGPL-3
├── requirements.txt                      the one Python package needed
├── addons/
│   └── ibq_whatsapp_helpdesk/            ← the addon. Point Odoo here.
└── optional-community-helpdesk-stub/
    └── helpdesk/                         ← ONLY if you have no Enterprise Helpdesk
```

### Dependencies

| Dependency | Where it comes from |
|---|---|
| `base`, `web`, `bus`, `mail`, `contacts` | Odoo core — already on your server |
| `helpdesk` | **Odoo Enterprise.** See below if you are on Community |
| `requests` (Python) | `requirements.txt` |

Nothing else. No JavaScript build step, no npm, no compiled extension.

---

## 1. Put the addon on the server

Copy `addons/ibq_whatsapp_helpdesk` into a directory Odoo scans. If you already
have a custom addons directory, use it:

```bash
sudo cp -r addons/ibq_whatsapp_helpdesk /opt/odoo/custom-addons/
sudo chown -R odoo:odoo /opt/odoo/custom-addons/ibq_whatsapp_helpdesk
```

If you do not have one yet, create it and add it to `addons_path` in your Odoo
config (usually `/etc/odoo/odoo.conf`), comma-separated, **before** restarting:

```ini
addons_path = /opt/odoo/odoo/addons,/opt/odoo/enterprise,/opt/odoo/custom-addons
```

The `odoo` service account must be able to read the files. Getting ownership
wrong is the most common reason a module never appears in the Apps list.

## 2. Install the Python dependency

Into the **same interpreter that runs Odoo**. If Odoo runs in a virtualenv:

```bash
sudo -u odoo /opt/odoo/venv/bin/pip install -r requirements.txt
```

System-wide install instead:

```bash
sudo pip3 install -r requirements.txt
```

Check it landed in the right place:

```bash
sudo -u odoo python3 -c "import requests; print(requests.__version__)"
```

## 3. If you are on Odoo Community, not Enterprise

`helpdesk` is an Enterprise app. Without it this addon cannot install.

- **You have Enterprise:** skip this step entirely. Do **not** copy the stub —
  it would collide with the real app.
- **You are on Community:** the stub in `optional-community-helpdesk-stub/`
  provides the three models and the fields this addon reads, and nothing more.
  It is a stand-in so the addon can run, not a helpdesk. Copy it the same way:

  ```bash
  sudo cp -r optional-community-helpdesk-stub/helpdesk /opt/odoo/custom-addons/
  sudo chown -R odoo:odoo /opt/odoo/custom-addons/helpdesk
  ```

  If you would rather use a real Community helpdesk, OCA's `helpdesk_mgmt` is
  the usual choice — porting notes are in README.md.

## 4. Install the module

Stop Odoo first. A running instance holds locks on `ir_cron`, and the install
fails on them:

```bash
sudo systemctl stop odoo
```

```bash
sudo -u odoo /opt/odoo/odoo-bin -c /etc/odoo/odoo.conf \
    -d YOUR_DATABASE -i ibq_whatsapp_helpdesk --stop-after-init
```

```bash
sudo systemctl start odoo
```

A clean run prints no `ERROR` or `CRITICAL` lines. If it does, nothing was
committed — fix and re-run.

Prefer the web UI? Start Odoo, go to **Apps**, *Update Apps List*, search
`WhatsApp`, click Install. The command line is better on a production box
because you see the errors.

## 5. Server settings that matter

In `/etc/odoo/odoo.conf`:

```ini
proxy_mode = True
workers = 4
list_db = False
admin_passwd = <something long>
db_filter = ^YOUR_DATABASE$
```

Three things people get wrong, all of which look like "it just does not work":

**HTTPS is not optional.** Twilio refuses to post to plain HTTP, and the webhook
signature is computed over the exact URL. Behind a reverse proxy set
`proxy_mode = True`, or fill **Public Base URL** on the account record if the
external URL differs from what Odoo thinks it is.

**`workers` must be greater than 0**, or scheduled actions never run — and the
outbound send queue and the idle-close both depend on cron.

**Proxy the gevent port for websockets.** The dashboard's live updates ride
Odoo's bus. Without this they silently fall back to slow polling:

```nginx
location /websocket {
    proxy_pass http://127.0.0.1:8072;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host $host;
}

location / {
    proxy_pass http://127.0.0.1:8069;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

## 6. Configure it

**WhatsApp ▸ Configuration ▸ WhatsApp Accounts ▸ New**

1. Paste the Account SID and Auth Token from the Twilio console.
2. Set the WhatsApp sender in E.164 (`+14155238886`).
3. Press **Test Connection** — you want a green *Connected*.
4. Copy the two webhook URLs it shows into Twilio, both as **POST**:
   - `https://your-odoo/whatsapp/twilio/inbound` → *When a message comes in*
   - `https://your-odoo/whatsapp/twilio/status` → *Status callback*

Then **WhatsApp ▸ Team ▸ Add Team Members** to build the roster, and paste each
approved template's `HX…` Content SID into its record under
**Configuration ▸ Templates**.

> The Twilio auth token is stored unencrypted in the database and is readable by
> the WhatsApp Administrator group. Treat that group as privileged, and rotate
> the token in Twilio if a database dump ever leaves your control.

## 7. Verify, in this order

1. `curl https://your-odoo/whatsapp/twilio/health` → `ok: 1 WhatsApp account(s) configured`
2. **Test Connection** on the account → *Connected*
3. Message the number from a real phone → a conversation appears within a second
4. Reply from **WhatsApp ▸ Dashboard ▸ Inbox** → it arrives on the phone
5. Move the ticket into a stage that has a template → the customer gets it

Step 3 is the one that matters. **No real Twilio message has ever gone through
this addon** — every automated test stubs the transport — so treat step 3 as the
real acceptance test, not a formality.

---

## Upgrading

Replace the folder and upgrade the module:

```bash
sudo systemctl stop odoo
sudo rm -rf /opt/odoo/custom-addons/ibq_whatsapp_helpdesk
sudo cp -r addons/ibq_whatsapp_helpdesk /opt/odoo/custom-addons/
sudo chown -R odoo:odoo /opt/odoo/custom-addons/ibq_whatsapp_helpdesk
sudo -u odoo /opt/odoo/odoo-bin -c /etc/odoo/odoo.conf \
    -d YOUR_DATABASE -u ibq_whatsapp_helpdesk --stop-after-init
sudo systemctl start odoo
```

Take a database dump first.

## Uninstalling

**Apps ▸ IBQ WhatsApp Helpdesk ▸ Uninstall** drops this addon's tables and the
columns it added to `helpdesk.ticket`. The entire conversation history lives in
`whatsapp_message` and **does not survive**. Dump the database first if you may
want it back.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Module not in the Apps list | Wrong `addons_path`, or the `odoo` user cannot read the files. Check ownership, then *Update Apps List*. |
| `Invalid view type: 'tree'` at install | You are installing the Odoo 17 package on Odoo 18. Use the 18 package. |
| Unknown tag `<list>` at install | The reverse: the 18 package on 17. |
| `ModuleNotFoundError: requests` | Installed into a different interpreter than the one running Odoo. |
| `Module helpdesk not found` | Odoo Community without the stub, or Enterprise addons not in `addons_path`. |
| Messages sent but nothing arrives inbound | Webhook not reachable, not HTTPS, or not POST. Check `/whatsapp/twilio/health` from outside. |
| Log shows `invalid X-Twilio-Signature` | `proxy_mode` not set, or **Public Base URL** does not match the URL Twilio actually calls. |
| Dashboard loads but never updates live | The gevent port is not proxied for `/websocket`. |
| Scheduled sends never leave | `workers = 0`, so cron does not run. |
| `KeyError: 'mail_message_id'` during install | **Not this addon.** See below. |

### `KeyError: 'mail_message_id'` (or any core field) during install

`mail_message_id` is a core field the `mail` module defines on `mail.mail`,
`mail.notification`, `mail.tracking.value` and `mail.message.schedule`. This
addon never references it.

The error means another module on the server extends a `mail.*` model without
declaring `mail` in its manifest `depends`. Odoo loads modules in dependency
order; such a module can load *after* `mail` by luck of the graph, and adding
any new module reshuffles that order and exposes the bug. The database is not
corrupt — the site keeps running, because the failure only happens during the
install's registry rebuild.

To find the offending module, run `tools/find-missing-mail-dep.sh` — shipped in
this package — from your custom addons directory. It is read-only and safe on
production:

```bash
cd /home/odoo/src/user          # or wherever your custom addons live
bash /path/to/{{NAME}}/tools/find-missing-mail-dep.sh
```

It prints one line per module that touches a `mail.*` model:

```
crm_extension                    extends: mail.thread     depends: base,mail    ok
some_custom_module               extends: mail.activity   depends: base         <<< MISSING mail -- THIS IS THE CULPRIT
```

Add `'mail'` to the flagged module's `depends`, upgrade just that module
(`-u THAT_MODULE`), then install this addon. The same applies to any other core
field name in the same error — the cause is always a missing dependency.

Run the addon's own test suite against a scratch database if you want to confirm
the install is sound — it needs no Twilio credentials:

```bash
sudo -u odoo /opt/odoo/odoo-bin -c /etc/odoo/odoo.conf \
    -d SCRATCH_DB -i ibq_whatsapp_helpdesk --test-enable \
    --test-tags=/ibq_whatsapp_helpdesk --stop-after-init
```

Expect `0 failed, 0 error(s) of 157 tests`.
