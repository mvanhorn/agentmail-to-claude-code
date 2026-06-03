#!/usr/bin/env python3
"""Send a task email into the watched inbox via AgentMail or Primitive.

The daemon reacts to inbound mail, so anything that can send a message to the
watched inbox from an allowlisted address can hand Claude Code a task. This is
the companion "sender" side: an agent, cron job, or script triggers a session
by sending mail.

An optional image is attached as base64 that is read and encoded in-process,
so when an agent shells out to this script the raw image bytes never have to
pass through its LLM context.

Usage:
  send_task.py <to_inbox> <subject> <body> [image_path]

Environment:
  CC_SEND_PROVIDER      "agentmail" (default) or "primitive"
  AGENTMAIL_API_KEY     API key for the SENDING inbox
  AGENTMAIL_SEND_INBOX  the inbox to send FROM (must be in the daemon's
                        CC_ALLOWED_FROM, e.g. your-agent@agentmail.to)
  PRIMITIVE_AUTH_TOKEN  Primitive bearer token, or set PRIMITIVE_API_KEY
  PRIMITIVE_SEND_FROM   Primitive sender address, e.g. agent@org.primitive.email
"""

import base64
import json
import mimetypes
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

PROVIDER = os.environ.get("CC_SEND_PROVIDER", "agentmail").strip().lower()
AGENTMAIL_API_BASE = os.environ.get("AGENTMAIL_API_BASE", "https://api.agentmail.to/v0")
PRIMITIVE_API_BASE = os.environ.get("PRIMITIVE_API_BASE", "https://api.primitive.dev/v1")


def _post_json(url: str, token: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST"
    )
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def send_agentmail(to: str, subject: str, body: str, image_path: str | None = None) -> dict:
    api_key = os.environ.get("AGENTMAIL_API_KEY")
    from_inbox = os.environ.get("AGENTMAIL_SEND_INBOX")
    if not api_key:
        raise RuntimeError("set AGENTMAIL_API_KEY")
    if not from_inbox:
        raise RuntimeError("set AGENTMAIL_SEND_INBOX")
    payload = {"to": [to], "subject": subject, "text": body}

    if image_path and os.path.exists(image_path):
        ext = image_path.rsplit(".", 1)[-1].lower()
        with open(image_path, "rb") as f:
            payload["attachments"] = [{
                "filename": f"image.{ext}",
                "content": base64.b64encode(f.read()).decode(),
            }]

    encoded_inbox = urllib.parse.quote(from_inbox, safe="")
    return _post_json(
        f"{AGENTMAIL_API_BASE}/inboxes/{encoded_inbox}/messages/send",
        api_key,
        payload,
    )


def send_primitive(to: str, subject: str, body: str, image_path: str | None = None) -> dict:
    api_key = os.environ.get("PRIMITIVE_AUTH_TOKEN") or os.environ.get("PRIMITIVE_API_KEY")
    from_email = os.environ.get("PRIMITIVE_SEND_FROM")
    if not api_key:
        raise RuntimeError("set PRIMITIVE_AUTH_TOKEN or PRIMITIVE_API_KEY")
    if not from_email:
        raise RuntimeError("set PRIMITIVE_SEND_FROM")
    payload = {
        "from": from_email,
        "to": to,
        "subject": subject,
        "body_text": body,
        "wait": True,
    }

    if image_path and os.path.exists(image_path):
        path = pathlib.Path(image_path)
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        payload["attachments"] = [{
            "filename": path.name,
            "content_type": ctype,
            "content_base64": base64.b64encode(path.read_bytes()).decode(),
        }]

    return _post_json(f"{PRIMITIVE_API_BASE}/send-mail", api_key, payload)


def send(to: str, subject: str, body: str, image_path: str | None = None) -> None:
    try:
        if PROVIDER == "agentmail":
            result = send_agentmail(to, subject, body, image_path)
            out = {"status": "sent", "messageId": result.get("message_id", "")}
        elif PROVIDER == "primitive":
            result = send_primitive(to, subject, body, image_path)
            data = result.get("data") or {}
            out = {
                "status": "sent",
                "id": data.get("id"),
                "delivery_status": data.get("delivery_status"),
                "accepted": data.get("accepted"),
                "rejected": data.get("rejected"),
            }
        else:
            raise RuntimeError("CC_SEND_PROVIDER must be 'agentmail' or 'primitive'")
        print(json.dumps(out, indent=2))
    except urllib.error.HTTPError as e:
        print(json.dumps({"status": "error", "code": e.code, "body": e.read().decode()}, indent=2))
        sys.exit(1)
    except RuntimeError as e:
        print(json.dumps({"status": "error", "message": str(e)}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: send_task.py <to_inbox> <subject> <body> [image_path]")
        sys.exit(1)
    send(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None)
