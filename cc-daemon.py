#!/usr/bin/env python3
"""Email -> Claude Code daemon.

Supports two mail providers:

- AgentMail (default): subscribes over WebSocket and dispatches unread messages.
- Primitive: polls stored inbound mail over REST and dispatches unseen messages.

On every authenticated, allowlisted message the daemon opens a new Claude Code
session in your terminal (standalone Ghostty or cmux), writes the email to a
prompt file, and tells Claude to read and act on it.

Configuration is entirely via environment variables (see cc.env.example):
  CC_MAIL_PROVIDER          "agentmail" (default) or "primitive"
  AGENTMAIL_API_KEY         AgentMail inbox-scoped API key
  AGENTMAIL_INBOX           AgentMail inbox to watch, e.g. you@agentmail.to
  PRIMITIVE_AUTH_TOKEN      Primitive API key or OAuth access token
  PRIMITIVE_API_KEY         accepted alias for PRIMITIVE_AUTH_TOKEN
  PRIMITIVE_INBOX           Primitive inbox to watch, e.g. you@org.primitive.email
  CC_TERMINAL               "cmux" (default) or "ghostty"
  CC_ALLOWED_FROM           comma-separated sender allowlist
  CMUX_BIN                  absolute path to the cmux CLI (cmux backend only)
  CC_HOME                   runtime dir for prompts/attachments/logs/state
"""

from __future__ import annotations

import base64
import email.utils
import io
import json
import logging
import os
import pathlib
import re
import subprocess
import tarfile
import time
import urllib.parse

import httpx

# --- configuration -----------------------------------------------------------

MAIL_PROVIDER = os.environ.get("CC_MAIL_PROVIDER", "agentmail").strip().lower()
if MAIL_PROVIDER == "primitive":
    INBOX = os.environ["PRIMITIVE_INBOX"]
elif MAIL_PROVIDER == "agentmail":
    INBOX = os.environ["AGENTMAIL_INBOX"]
else:
    raise RuntimeError("CC_MAIL_PROVIDER must be 'agentmail' or 'primitive'")

# Which terminal to drive a new Claude Code session in.
TERMINAL = os.environ.get("CC_TERMINAL", "cmux").strip().lower()

# Comma-separated sender allowlist, e.g. "you@example.com,agent@example.com".
# Messages from any other sender (or that fail provider auth checks) are dropped.
ALLOWED_FROM = {
    addr.strip().lower()
    for addr in os.environ.get("CC_ALLOWED_FROM", "").split(",")
    if addr.strip()
}

CC_HOME = pathlib.Path(
    os.environ.get("CC_HOME", pathlib.Path.home() / ".agentmail-cc")
)
ATTACHMENT_ROOT = CC_HOME / "attachments"
PROMPT_ROOT = CC_HOME / "prompts"
LOG_PATH = CC_HOME / "dispatch.log"
PRIMITIVE_STATE_PATH = CC_HOME / "primitive-state.json"
CC_HOME.mkdir(parents=True, exist_ok=True)

# Primitive REST polling. Stored email has no AgentMail-style unread marker, so
# we keep a local processed-id state file and retry failed dispatches with backoff.
PRIMITIVE_API_BASE = os.environ.get(
    "PRIMITIVE_API_BASE", "https://api.primitive.dev/v1"
).rstrip("/")
PRIMITIVE_POLL_INTERVAL_S = int(os.environ.get("PRIMITIVE_POLL_INTERVAL", "15"))
PRIMITIVE_POLL_LIMIT = int(os.environ.get("PRIMITIVE_POLL_LIMIT", "25"))
PRIMITIVE_POLL_MAX_PAGES = int(os.environ.get("PRIMITIVE_POLL_MAX_PAGES", "4"))
PRIMITIVE_FAILED_BACKOFF_S = int(os.environ.get("PRIMITIVE_FAILED_BACKOFF", "300"))
PRIMITIVE_EMAIL_STATUSES = [
    s.strip()
    for s in os.environ.get("PRIMITIVE_EMAIL_STATUSES", "completed,accepted").split(",")
    if s.strip()
]

# cmux backend: the daemon shells out to the cmux CLI by absolute path because
# launchd's sanitized PATH does not include the app bundle's bin directory.
CMUX_BIN = os.environ.get(
    "CMUX_BIN", "/Applications/cmux.app/Contents/Resources/bin/cmux"
)
# Substring printed in a fresh Claude Code surface once it is ready for input.
CLAUDE_READY_MARKER = "bypass permissions on"
CLAUDE_READY_TIMEOUT_S = int(os.environ.get("CC_READY_TIMEOUT", "25"))
# surface.create can fail with a transient "Broken pipe" when cmux's control
# plane is briefly unresponsive (mid-restart, recovering from a crash). Retry a
# few times with backoff before giving up so a flaky cmux self-heals.
SURFACE_CREATE_ATTEMPTS = int(os.environ.get("CC_SURFACE_CREATE_ATTEMPTS", "4"))
SURFACE_CREATE_BACKOFF_S = int(os.environ.get("CC_SURFACE_CREATE_BACKOFF", "3"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
)
log = logging.getLogger("cc-mail")

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def parse_from(raw: str) -> str:
    _name, addr = email.utils.parseaddr(raw or "")
    return (addr or raw or "").strip().lower()


def is_authenticated(labels) -> bool:
    """AgentMail auth check: unauthenticated mail gets a provider label."""
    return "unauthenticated" not in (labels or [])


def primitive_is_authenticated(detail: dict) -> bool:
    """Conservative sender auth check for Primitive REST/webhook shapes."""
    analysis = detail.get("analysis") or {}
    sender_analysis = analysis.get("sender") or {}
    if sender_analysis.get("authenticated") is True:
        return True
    if sender_analysis.get("authenticated") is False:
        return False

    auth = detail.get("auth") or {}
    dmarc = str(auth.get("dmarc") or "").lower()
    if dmarc == "pass":
        return True
    if auth.get("dmarcSpfAligned") or auth.get("dmarcDkimAligned"):
        return True
    for sig in auth.get("dkimSignatures") or []:
        if str(sig.get("result") or "").lower() == "pass" and sig.get("aligned"):
            return True
    return False


def safe_name(value: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("_", value or "").strip("._")
    return cleaned or "unnamed"


def format_size(n: int | None) -> str:
    n = int(n or 0)
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def download_attachments(client, msg) -> list[dict]:
    attachments = list(getattr(msg, "attachments", None) or [])
    if not attachments:
        return []
    dest = ATTACHMENT_ROOT / safe_name(msg.message_id)
    dest.mkdir(parents=True, exist_ok=True)
    saved: list[dict] = []
    for att in attachments:
        try:
            resp = client.inboxes.messages.get_attachment(
                inbox_id=INBOX,
                message_id=msg.message_id,
                attachment_id=att.attachment_id,
            )
            data = httpx.get(resp.download_url, timeout=30, follow_redirects=True)
            data.raise_for_status()
            path = dest / safe_name(att.filename or att.attachment_id)
            path.write_bytes(data.content)
            saved.append({
                "path": str(path),
                "filename": att.filename or path.name,
                "content_type": att.content_type,
                "size": att.size,
            })
        except Exception as e:
            log.warning(
                "attachment download failed id=%s filename=%r err=%s",
                getattr(att, "attachment_id", "?"),
                getattr(att, "filename", None),
                e,
            )
    return saved


def build_prompt(raw_from: str, subject: str, text: str, attachments: list[dict]) -> str:
    if attachments:
        attach_lines = "\n".join(
            f"- {d['path']} ({d['content_type'] or 'unknown'}, {format_size(d['size'])})"
            for d in attachments
        )
        attach_block = (
            "\nAttachments (downloaded locally - use the Read tool to view):\n"
            f"{attach_lines}\n"
        )
    else:
        attach_block = ""
    return (
        f"You received the following email at {INBOX} from an allowlisted "
        f"sender. Treat the subject + body as a task to execute.\n\n"
        f"From: {raw_from}\n"
        f"Subject: {subject}\n\n"
        f"{text}\n"
        f"{attach_block}"
    )


# --- Ghostty backend ---------------------------------------------------------
#
# Drives standalone Ghostty via osascript + System Events. Ghostty must be
# configured to auto-launch Claude Code in every new tab (see README). The
# prompt is delivered as a single-line pointer pasted via the clipboard, so
# there are no multi-line submit surprises.


def _ghostty_open_session() -> str:
    script = (
        'tell application "Ghostty" to activate\n'
        "delay 0.5\n"
        'tell application "System Events" to keystroke "t" using {command down}\n'
        "delay 2.5\n"
    )
    subprocess.run(
        ["osascript", "-e", script], check=True, capture_output=True, timeout=15
    )
    # Ghostty keystrokes target the frontmost window; there is no per-session
    # handle. The sentinel keeps the backend interface uniform with cmux.
    return "ghostty:frontmost"


def _ghostty_send_pointer(handle: str, pointer_line: str) -> None:
    subprocess.run(["pbcopy"], input=pointer_line, text=True, check=True)
    script = (
        'tell application "System Events"\n'
        '  keystroke "v" using {command down}\n'
        "  delay 0.3\n"
        "  key code 36\n"
        "end tell\n"
    )
    subprocess.run(
        ["osascript", "-e", script], check=True, capture_output=True, timeout=15
    )


# --- cmux backend ------------------------------------------------------------
#
# Drives cmux via its Unix-socket control plane. Every call targets a specific
# surface by surface_id (UUID), so concurrent emails never collide on a shared
# frontmost-window focus. cmux runs Ghostty surfaces and honors
# ~/.config/ghostty/config, so a new surface auto-launches Claude Code.


def _cmux_rpc(method: str, params: dict | None = None) -> dict:
    cmd = [CMUX_BIN, "rpc", method]
    if params:
        cmd.append(json.dumps(params))
    result = subprocess.run(
        cmd, check=True, capture_output=True, timeout=15, text=True
    )
    out = (result.stdout or "").strip()
    return json.loads(out) if out else {}


def _surface_text(surface_id: str) -> str:
    data = _cmux_rpc("surface.read_text", {"surface_id": surface_id})
    b64 = data.get("base64")
    if b64:
        try:
            return base64.b64decode(b64).decode("utf-8", "replace")
        except Exception:
            return data.get("text", "") or ""
    return data.get("text", "") or ""


def _cmux_open_session() -> str:
    resp = None
    for attempt in range(1, SURFACE_CREATE_ATTEMPTS + 1):
        try:
            resp = _cmux_rpc("surface.create")
            break
        except subprocess.CalledProcessError as e:
            stderr = getattr(e, "stderr", "") or ""
            if isinstance(stderr, bytes):
                stderr = stderr.decode().strip()
            if attempt == SURFACE_CREATE_ATTEMPTS:
                raise
            log.warning(
                "surface.create failed (attempt %d/%d), retrying in %ds: %s %s",
                attempt, SURFACE_CREATE_ATTEMPTS, SURFACE_CREATE_BACKOFF_S, e, stderr,
            )
            time.sleep(SURFACE_CREATE_BACKOFF_S)
    surface_id = resp.get("surface_id")
    if not surface_id:
        raise RuntimeError(f"surface.create returned no surface_id: {resp!r}")
    deadline = time.monotonic() + CLAUDE_READY_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            if CLAUDE_READY_MARKER in _surface_text(surface_id):
                return surface_id
        except subprocess.CalledProcessError:
            pass  # transient read failure; retry until deadline
        time.sleep(1)
    log.warning(
        "claude ready marker not seen in surface=%s within %ds; sending anyway",
        surface_id,
        CLAUDE_READY_TIMEOUT_S,
    )
    return surface_id


def _cmux_send_pointer(surface_id: str, pointer_line: str) -> None:
    # cmux's send_text writes straight to the PTY and the Claude Code TUI treats
    # every newline as Enter, so the pointer MUST be single-line. cmux also
    # swallows clipboard-paste chords, so send_text is the only path in.
    if "\n" in pointer_line:
        raise ValueError("pointer_line must be single-line; newlines submit early")
    _cmux_rpc("surface.send_text", {"surface_id": surface_id, "text": pointer_line})
    time.sleep(0.3)
    _cmux_rpc("surface.send_key", {"surface_id": surface_id, "key": "enter"})


# --- backend dispatch --------------------------------------------------------

BACKENDS = {
    "ghostty": (_ghostty_open_session, _ghostty_send_pointer),
    "cmux": (_cmux_open_session, _cmux_send_pointer),
}


def dispatch_to_claude_code(prompt: str, sender: str, msg_id: str) -> bool:
    PROMPT_ROOT.mkdir(parents=True, exist_ok=True)
    prompt_path = PROMPT_ROOT / f"{safe_name(msg_id)}.md"
    prompt_path.write_text(prompt)

    backend = BACKENDS.get(TERMINAL)
    if backend is None:
        log.error("unknown CC_TERMINAL=%r (expected 'ghostty' or 'cmux')", TERMINAL)
        return False
    open_session, send_pointer = backend

    try:
        handle = open_session()
    except Exception as e:
        log.error("open session failed (terminal=%s): %s", TERMINAL, e)
        return False

    pointer = (
        f"An email task arrived at {INBOX} from {sender}. "
        f"Read and act on the task described in this file: {prompt_path}"
    )
    try:
        send_pointer(handle, pointer)
    except Exception as e:
        log.error("send pointer failed (terminal=%s): %s", TERMINAL, e)
        return False
    log.info(
        "dispatched via %s for %s (prompt_file=%s)", TERMINAL, sender, prompt_path
    )
    return True


# --- AgentMail provider ------------------------------------------------------


def handle_message(client, ev) -> None:
    msg = ev.message
    raw_from = getattr(msg, "from_", None) or getattr(msg, "from", "")
    sender = parse_from(raw_from)
    subject = (msg.subject or "(no subject)").strip()
    text = (msg.text or "").strip()
    labels = list(msg.labels or [])
    log.info("recv from=%s subj=%r labels=%s", sender, subject, labels)

    if sender not in ALLOWED_FROM:
        log.info("DROP sender-not-allowed sender=%s", sender)
        return
    if not is_authenticated(labels):
        log.info("DROP not-authenticated sender=%s labels=%s", sender, labels)
        return

    saved = download_attachments(client, msg)
    if saved:
        log.info("attachments saved=%d for %s", len(saved), sender)
    prompt = build_prompt(raw_from, subject, text, saved)
    ok = dispatch_to_claude_code(prompt, sender, msg.message_id)

    if not ok:
        # Dispatch failed (e.g. terminal unreachable). Leave the message UNREAD
        # so the task is not silently lost, and bounce a reply back so the
        # sender knows to resend or fix their terminal. A common cause on cmux:
        # socketControlMode is "cmuxOnly", which blocks this launchd daemon
        # (not a cmux child) -- see the README cmux setup section.
        log.error(
            "dispatch FAILED; leaving unread for retry sender=%s subj=%r msg=%s",
            sender, subject, msg.message_id,
        )
        try:
            client.inboxes.messages.reply(
                inbox_id=INBOX,
                message_id=msg.message_id,
                text=(
                    f"Could not dispatch '{subject}' to your terminal "
                    f"(CC_TERMINAL={TERMINAL}). The task was left unread; resend "
                    f"once the terminal is reachable. If you use cmux, confirm "
                    f"automation.socketControlMode is not 'cmuxOnly' and that "
                    f"cmux was restarted after changing it."
                ),
            )
        except Exception as e:
            log.warning("failure-reply skipped: %s", e)
        return

    try:
        client.inboxes.messages.update(
            inbox_id=INBOX,
            message_id=msg.message_id,
            remove_labels=["unread"],
        )
    except Exception as e:
        log.warning("mark-read skipped: %s", e)


def run_agentmail_loop() -> None:
    from agentmail import AgentMail, MessageReceivedEvent, Subscribe, Subscribed

    while True:
        try:
            client = AgentMail()
            with client.websockets.connect() as socket:
                socket.send_subscribe(Subscribe(inbox_ids=[INBOX]))
                for event in socket:
                    if isinstance(event, Subscribed):
                        log.info("subscribed: %s", event.inbox_ids)
                    elif isinstance(event, MessageReceivedEvent):
                        try:
                            handle_message(client, event)
                        except Exception:
                            log.exception("handler crashed")
        except Exception:
            log.exception("WS loop crashed, reconnecting in 5s")
            time.sleep(5)


# --- Primitive provider ------------------------------------------------------


def primitive_token() -> str:
    token = os.environ.get("PRIMITIVE_AUTH_TOKEN") or os.environ.get("PRIMITIVE_API_KEY")
    if not token:
        raise RuntimeError("set PRIMITIVE_AUTH_TOKEN or PRIMITIVE_API_KEY")
    return token


def primitive_request_json(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    payload: dict | None = None,
) -> dict:
    resp = httpx.request(
        method,
        f"{PRIMITIVE_API_BASE}{path}",
        params={k: v for k, v in (params or {}).items() if v is not None and v != ""},
        json=payload,
        headers={
            "Authorization": f"Bearer {primitive_token()}",
            "Accept": "application/json",
        },
        timeout=30,
        follow_redirects=True,
    )
    resp.raise_for_status()
    result = resp.json()
    if result.get("success") is False:
        raise RuntimeError(f"Primitive API {method} {path} failed: {result.get('error')}")
    return result


def primitive_request_bytes(path: str) -> bytes:
    resp = httpx.get(
        f"{PRIMITIVE_API_BASE}{path}",
        headers={"Authorization": f"Bearer {primitive_token()}"},
        timeout=60,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.content


def primitive_load_state() -> dict:
    if not PRIMITIVE_STATE_PATH.exists():
        return {"processed": [], "failures": {}}
    try:
        data = json.loads(PRIMITIVE_STATE_PATH.read_text())
        data.setdefault("processed", [])
        data.setdefault("failures", {})
        return data
    except Exception:
        log.exception("state load failed; starting with empty state")
        return {"processed": [], "failures": {}}


def primitive_save_state(state: dict) -> None:
    PRIMITIVE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PRIMITIVE_STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(PRIMITIVE_STATE_PATH)


def primitive_mark_processed(state: dict, email_id: str) -> None:
    processed = [x for x in state.get("processed", []) if x != email_id]
    processed.append(email_id)
    state["processed"] = processed[-1000:]
    state.get("failures", {}).pop(email_id, None)


def primitive_should_attempt(state: dict, email_id: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    failure = state.get("failures", {}).get(email_id)
    if not failure:
        return True
    return now - float(failure.get("last_attempt", 0)) >= PRIMITIVE_FAILED_BACKOFF_S


def primitive_mark_failure(state: dict, email_id: str, *, notified: bool) -> None:
    failures = state.setdefault("failures", {})
    previous = failures.get(email_id, {})
    failures[email_id] = {
        "last_attempt": time.time(),
        "notified": bool(notified or previous.get("notified")),
    }


def primitive_list_candidate_summaries() -> list[dict]:
    """Return recent Primitive inbox summaries from newest to oldest."""
    by_id: dict[str, dict] = {}
    for status in PRIMITIVE_EMAIL_STATUSES:
        cursor = None
        for _page in range(PRIMITIVE_POLL_MAX_PAGES):
            result = primitive_request_json(
                "GET",
                "/emails/search",
                params={
                    "to": INBOX,
                    "status": status,
                    "sort": "received_at_desc",
                    "limit": PRIMITIVE_POLL_LIMIT,
                    "cursor": cursor,
                    "snippet": "false",
                    "include_facets": "false",
                },
            )
            for item in result.get("data") or []:
                if item.get("id"):
                    by_id[item["id"]] = item
            cursor = (result.get("meta") or {}).get("cursor")
            if not cursor:
                break
    return sorted(
        by_id.values(),
        key=lambda item: item.get("received_at") or item.get("created_at") or "",
        reverse=True,
    )


def primitive_get_email_detail(email_id: str) -> dict:
    result = primitive_request_json("GET", f"/emails/{urllib.parse.quote(email_id, safe='')}")
    return result.get("data") or {}


def _safe_tar_target(root: pathlib.Path, member_name: str) -> pathlib.Path | None:
    target = root / member_name
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return target


def primitive_download_attachments(detail: dict) -> list[dict]:
    parsed = detail.get("parsed") or {}
    attachments = list(parsed.get("attachments") or [])
    if not attachments:
        return []

    email_id = detail["id"]
    dest = ATTACHMENT_ROOT / safe_name(email_id)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        blob = primitive_request_bytes(
            f"/emails/{urllib.parse.quote(email_id, safe='')}/attachments.tar.gz"
        )
    except Exception as e:
        log.warning("attachment archive download failed id=%s err=%s", email_id, e)
        return []

    saved: list[dict] = []
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            target = _safe_tar_target(dest, member.name)
            if target is None:
                log.warning("skipping unsafe attachment path id=%s path=%r", email_id, member.name)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                continue
            target.write_bytes(source.read())
            meta = attachments[len(saved)] if len(saved) < len(attachments) else {}
            saved.append({
                "path": str(target),
                "filename": meta.get("filename") or pathlib.Path(member.name).name,
                "content_type": meta.get("content_type"),
                "size": meta.get("size_bytes") or member.size,
            })
    return saved


def primitive_sender_from_detail(detail: dict) -> str:
    return (
        detail.get("from_email")
        or parse_from(detail.get("from_header") or "")
        or detail.get("sender")
        or ""
    ).lower()


def primitive_failure_reply_once(detail: dict, state: dict, subject: str) -> bool:
    email_id = detail["id"]
    if state.get("failures", {}).get(email_id, {}).get("notified"):
        return True
    payload = {
        "body_text": (
            f"Could not dispatch '{subject}' to your terminal "
            f"(CC_TERMINAL={TERMINAL}). The task will be retried locally after "
            f"{PRIMITIVE_FAILED_BACKOFF_S} seconds once the terminal is reachable. "
            f"If you use cmux, confirm automation.socketControlMode is not "
            f"'cmuxOnly' and that cmux was restarted after changing it."
        )
    }
    if os.environ.get("PRIMITIVE_REPLY_FROM"):
        payload["from"] = os.environ["PRIMITIVE_REPLY_FROM"]
    try:
        primitive_request_json(
            "POST",
            f"/emails/{urllib.parse.quote(email_id, safe='')}/reply",
            payload=payload,
        )
        return True
    except Exception as e:
        log.warning("failure-reply skipped id=%s err=%s", email_id, e)
        return False


def primitive_handle_email(detail: dict, state: dict) -> None:
    email_id = detail["id"]
    raw_from = detail.get("from_header") or detail.get("from_email") or detail.get("sender") or ""
    sender = primitive_sender_from_detail(detail)
    subject = (detail.get("subject") or "(no subject)").strip()
    text = (detail.get("body_text") or "").strip()
    if not text and detail.get("body_html"):
        text = "[No text/plain body was available. HTML body follows.]\n\n" + detail["body_html"].strip()
    log.info("recv provider=primitive id=%s from=%s subj=%r", email_id, sender, subject)

    if sender not in ALLOWED_FROM:
        log.info("DROP sender-not-allowed sender=%s", sender)
        primitive_mark_processed(state, email_id)
        return
    if not primitive_is_authenticated(detail):
        log.info("DROP not-authenticated sender=%s auth=%s", sender, detail.get("auth"))
        primitive_mark_processed(state, email_id)
        return

    saved = primitive_download_attachments(detail)
    if saved:
        log.info("attachments saved=%d for %s", len(saved), sender)
    prompt = build_prompt(raw_from, subject, text, saved)
    ok = dispatch_to_claude_code(prompt, sender, email_id)
    if ok:
        primitive_mark_processed(state, email_id)
        return

    notified = primitive_failure_reply_once(detail, state, subject)
    primitive_mark_failure(state, email_id, notified=notified)


def primitive_poll_once(state: dict) -> int:
    processed = set(state.get("processed", []))
    summaries = primitive_list_candidate_summaries()
    handled = 0
    for summary in reversed(summaries):
        email_id = summary.get("id")
        if not email_id or email_id in processed:
            continue
        if not primitive_should_attempt(state, email_id):
            continue
        try:
            detail = primitive_get_email_detail(email_id)
            if not detail:
                continue
            primitive_handle_email(detail, state)
            handled += 1
        except Exception:
            log.exception("handler crashed id=%s", email_id)
            primitive_mark_failure(state, email_id, notified=False)
        finally:
            primitive_save_state(state)
    return handled


def run_primitive_loop() -> None:
    primitive_token()
    state = primitive_load_state()
    while True:
        try:
            count = primitive_poll_once(state)
            if count:
                log.info("primitive poll handled=%d", count)
        except Exception:
            log.exception("primitive poll loop crashed")
        time.sleep(PRIMITIVE_POLL_INTERVAL_S)


def main() -> None:
    if not ALLOWED_FROM:
        log.warning("CC_ALLOWED_FROM is empty - every message will be dropped. Set it in cc.env.")
    log.info(
        "starting cc-mail daemon provider=%s inbox=%s terminal=%s allowed=%s",
        MAIL_PROVIDER, INBOX, TERMINAL, sorted(ALLOWED_FROM),
    )
    if MAIL_PROVIDER == "agentmail":
        run_agentmail_loop()
    elif MAIL_PROVIDER == "primitive":
        run_primitive_loop()
    else:
        raise RuntimeError("unreachable provider branch")


if __name__ == "__main__":
    main()
