# -*- coding: utf-8 -*-
"""Translate Meta Cloud API webhook JSON into the flat shape intake expects.

Meta nests everything under ``entry[].changes[].value``; Twilio posts a flat
form. Rather than teach the whole intake path two dialects, Meta payloads are
flattened here into the same keys ``_process_inbound_payload`` already reads.

Pure functions on purpose: no ORM, no network, so the mapping can be tested
against captured payloads without a database.
"""

# Meta's delivery states, mapped onto the module's own message states. The
# names Twilio uses are already the module's vocabulary, so most pass straight
# through; Meta simply has fewer of them.
META_STATUS_MAP = {
    "accepted": "outgoing",
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
    "deleted": "cancelled",
}

# Message types that carry a media id rather than text.
MEDIA_TYPES = ("image", "video", "audio", "document", "sticker")


def _digits_to_e164(value):
    """Meta sends bare digits ("971501234567"); the module speaks E.164."""
    if not value:
        return ""
    value = str(value).strip()
    return value if value.startswith("+") else "+" + value


def iter_message_events(payload):
    """Yield one ``value`` dict per inbound-message change in the payload.

    A single webhook POST may batch several entries and changes, so callers
    must loop rather than assume one message per request.
    """
    for entry in (payload or {}).get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            if change.get("field") == "messages":
                yield value


def flatten_message(value, message):
    """Flatten one Meta message into Twilio-shaped intake parameters."""
    metadata = value.get("metadata") or {}
    contacts = value.get("contacts") or []
    profile_name = ""
    if contacts:
        profile_name = ((contacts[0] or {}).get("profile") or {}).get("name") or ""

    params = {
        "From": _digits_to_e164(message.get("from")),
        "To": _digits_to_e164(metadata.get("display_phone_number")),
        "ProfileName": profile_name,
        "MessageSid": message.get("id") or "",
        # The account is identified by the phone number id, which is the only
        # stable handle Meta puts in every payload.
        "AccountSid": metadata.get("phone_number_id") or "",
        "Body": "",
        "NumMedia": "0",
    }

    kind = message.get("type")
    if kind == "text":
        params["Body"] = (message.get("text") or {}).get("body") or ""
    elif kind == "interactive":
        interactive = message.get("interactive") or {}
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        # The id is what the bot matches on; the title is what the person saw.
        key = "ListId" if interactive.get("type") == "list_reply" else "ButtonPayload"
        params[key] = reply.get("id") or ""
        params["Body"] = reply.get("title") or reply.get("id") or ""
    elif kind == "button":
        # Quick-reply button attached to a template.
        button = message.get("button") or {}
        params["ButtonPayload"] = button.get("payload") or ""
        params["Body"] = button.get("text") or button.get("payload") or ""
    elif kind in MEDIA_TYPES:
        media = message.get(kind) or {}
        media_id = media.get("id")
        if media_id:
            # The id, not a URL: resolving it needs a Graph call plus the
            # access token, which happens server-side at download time.
            params["NumMedia"] = "1"
            params["MediaUrl0"] = media_id
            params["MediaContentType0"] = media.get("mime_type") or ""
        params["Body"] = media.get("caption") or ""
    elif kind == "location":
        location = message.get("location") or {}
        params["Body"] = location.get("name") or "%s,%s" % (
            location.get("latitude"), location.get("longitude"),
        )
    elif kind == "contacts":
        params["Body"] = "[contact card]"
    else:
        # Unknown or unsupported type: keep the envelope so the chat still
        # shows that something arrived.
        params["Body"] = ""
    return params


def inbound_params(payload):
    """Every inbound message in a Meta webhook, as flat intake dicts."""
    out = []
    for value in iter_message_events(payload):
        for message in value.get("messages") or []:
            out.append(flatten_message(value, message))
    return out


def status_params(payload):
    """Every delivery-status update in a Meta webhook, flattened.

    Meta posts statuses to the same webhook as messages, so one request can
    carry both.
    """
    out = []
    for value in iter_message_events(payload):
        for status in value.get("statuses") or []:
            errors = status.get("errors") or []
            error = errors[0] if errors else {}
            out.append({
                "MessageSid": status.get("id") or "",
                "MessageStatus": (status.get("status") or "").lower(),
                "ErrorCode": str(error.get("code")) if error.get("code") else "",
                "ErrorMessage": error.get("title") or error.get("message") or "",
            })
    return out


def build_send_payload(to_number, body=None, media_ids=None, template_name=None,
                       template_language=None, template_components=None):
    """Build the JSON body for POST /{phone_number_id}/messages."""
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": str(to_number).lstrip("+"),
    }
    if template_name:
        payload["type"] = "template"
        payload["template"] = {
            "name": template_name,
            "language": {"code": template_language or "en"},
        }
        if template_components:
            payload["template"]["components"] = template_components
    elif media_ids:
        # One media per message is Meta's limit; the caller sends the rest
        # as follow-up messages.
        payload["type"] = "image"
        payload["image"] = {"id": media_ids[0]}
        if body:
            payload["image"]["caption"] = body
    else:
        payload["type"] = "text"
        # preview_url off: a bare link in a support reply should not expand
        # into a card the agent did not intend to send.
        payload["text"] = {"preview_url": False, "body": body or ""}
    return payload


def build_template_components(variables):
    """Turn ``{1: "Ann", 2: "T-42"}`` into Meta's body-parameter components."""
    if not variables:
        return []
    ordered = sorted(variables.items(), key=lambda item: int(item[0]))
    return [{
        "type": "body",
        "parameters": [
            {"type": "text", "text": "" if value is None else str(value)}
            for _index, value in ordered
        ],
    }]
