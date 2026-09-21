# Install failure: `KeyError: 'mail_message_id'`

**For whoever is doing the Odoo.sh deployment.** Please read this before
retrying the install — the fix is a one-line change, but it is not in the
WhatsApp addon.

---

## The error

Installing `ibq_whatsapp_helpdesk` on Odoo 17 fails with:

```
File "/home/odoo/src/odoo/odoo/fields.py", line 4458, in setup_nonrelated
    invf = comodel._fields[self.inverse_name]
KeyError: 'mail_message_id'
```

## What it means

`mail_message_id` is a **core Odoo field**, defined by the `mail` module on
`mail.mail`, `mail.notification`, `mail.tracking.value` and
`mail.message.schedule`. `mail.message` declares One2many fields pointing back
at those models.

The error says one of those comodels was built **without** its own
`mail_message_id`. That happens when a module extends a `mail.*` model but does
not declare `mail` in its manifest `depends`, so Odoo builds that model before
`mail` has defined its fields.

## Why it only appears now

The database is **not** corrupt — the site runs fine, so the registry builds
normally. Odoo loads modules in dependency order, and a module with a missing
dependency can load *after* `mail` purely by luck of the graph. Installing any
new module changes that graph, the mis-declared module now loads *first*, and
the latent bug surfaces.

Installing any other module would very likely trigger the same failure.

## It is not the WhatsApp addon

- `mail_message_id` does not appear anywhere in its source
  (`grep -rn "mail_message_id" ibq_whatsapp_helpdesk/` → no matches).
- None of its One2many fields use that inverse.
- It declares `mail` correctly in `depends`.
- It installs cleanly and passes its 157 tests on stock Odoo 17 and 18,
  including alongside `sms`, `im_livechat`, `mail_group` and `snailmail` — every
  core module that defines a `mail_message_id`.

---

## Step 1 — Find the module (read-only, safe on production)

In the Odoo.sh shell:

```bash
cd /home/odoo/src/user
bash find-missing-mail-dep.sh
```

(The script is attached, and also ships inside the release zip under `tools/`.)

One line per custom module that touches a `mail.*` model. Anything flagged
`<<< MISSING mail` is the culprit.

### If that prints nothing

The registry currently builds, so `odoo shell` works. Use it to see which
modules own the models involved:

```bash
odoo-bin shell -c /home/odoo/.config/odoo/odoo.conf -d YOUR_DB
```

```python
for m in ('mail.mail', 'mail.notification', 'mail.tracking.value', 'mail.message.schedule'):
    print(m, '->', env['ir.model'].search([('model', '=', m)]).modules)
```

Any module listed that is not an Odoo app is a candidate. `simplify_access_management`
appears in the traceback and is worth checking directly:

```bash
grep -rn "mail\.\(mail\|notification\|message\|tracking\)" /home/odoo/src/user/simplify_access_management/
```

## Step 2 — Fix it

Add `'mail'` to that module's `depends` in its `__manifest__.py`:

```python
"depends": ["base", "mail"],       # 'mail' was missing
```

This pins the load order permanently, so it cannot resurface the next time a
module is added.

## Step 3 — Apply and install

Test on an Odoo.sh **staging branch** first. Staging is a copy of the
production database, not a blank one, so it reproduces the problem exactly.

```bash
odoo-bin -c /home/odoo/.config/odoo/odoo.conf -d YOUR_DB -u THAT_MODULE --stop-after-init
```

Then install the WhatsApp addon as normal (see `INSTALL.md` in the release
package). Stop the Odoo service first — a running instance holds locks on
`ir_cron` and the install fails on them.

---

## If it still fails

Send back:

1. The full output of the Step 1 scanner.
2. The output of the `odoo shell` snippet above.
3. The complete server log for the failed install, not just the RPC traceback —
   the lines *before* the traceback usually name the module being loaded when it
   broke.

That is enough to identify it precisely.
