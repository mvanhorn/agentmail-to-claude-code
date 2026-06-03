"""Tests for cc-daemon.py helpers and both dispatch backends.

The daemon file has a hyphen in its name, so we load it via importlib.
"""

from __future__ import annotations

import importlib.util
import io
import os
import pathlib
import tarfile
import tempfile

import pytest


def _load_daemon():
    os.environ.setdefault("AGENTMAIL_INBOX", "bot@agentmail.to")
    os.environ.setdefault("AGENTMAIL_API_KEY", "x")
    os.environ.setdefault("CC_TERMINAL", "cmux")
    os.environ.setdefault("CC_ALLOWED_FROM", "alice@example.com, bob@example.com")
    os.environ.setdefault("CC_HOME", tempfile.mkdtemp(prefix="cc-home-"))
    spec = importlib.util.spec_from_file_location(
        "cc_daemon",
        pathlib.Path(__file__).parent / "cc-daemon.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


daemon = _load_daemon()


# --- config parsing ----------------------------------------------------------


def test_allowlist_parsed_and_lowercased():
    assert daemon.ALLOWED_FROM == {"alice@example.com", "bob@example.com"}


def test_safe_name_strips_message_id_angle_brackets():
    out = daemon.safe_name("<mp8w1w77.8dd87270@example.com>")
    assert out == "mp8w1w77.8dd87270_example.com"
    assert "/" not in out and ".." not in out


def test_safe_name_blocks_path_traversal():
    assert daemon.safe_name("../../etc/passwd") == "etc_passwd"


def test_format_size_units():
    assert daemon.format_size(512) == "512 B"
    assert daemon.format_size(2048) == "2.0 KB"
    assert daemon.format_size(5 * 1024 * 1024) == "5.0 MB"


def test_parse_from_extracts_angle_addr():
    assert daemon.parse_from("Alice <ALICE@Example.com>") == "alice@example.com"
    assert daemon.parse_from("bob@example.com") == "bob@example.com"


# --- build_prompt ------------------------------------------------------------


def test_build_prompt_includes_headers_and_body():
    p = daemon.build_prompt("Alice <alice@example.com>", "Do X", "body here", [])
    assert "From: Alice <alice@example.com>" in p
    assert "Subject: Do X" in p
    assert "body here" in p
    assert "Attachments" not in p


def test_build_prompt_lists_attachment_paths():
    atts = [{"path": "/tmp/a.png", "content_type": "image/png", "size": 1234}]
    p = daemon.build_prompt("a@example.com", "subj", "body", atts)
    assert "/tmp/a.png" in p
    assert "use the Read tool" in p


# --- Primitive helpers -------------------------------------------------------


def test_primitive_auth_uses_analysis_then_auth_fallback():
    assert daemon.primitive_is_authenticated({"analysis": {"sender": {"authenticated": True}}})
    assert not daemon.primitive_is_authenticated({"analysis": {"sender": {"authenticated": False}}, "auth": {"dmarc": "pass"}})
    assert daemon.primitive_is_authenticated({"auth": {"dmarc": "pass"}})
    assert daemon.primitive_is_authenticated({"auth": {"dkimSignatures": [{"result": "pass", "aligned": True}]}})
    assert not daemon.primitive_is_authenticated({"auth": {"dmarc": "fail", "dkimSignatures": []}})


def test_primitive_list_candidate_summaries_queries_each_status(monkeypatch):
    calls = []

    def fake_request(_method, _path, *, params=None, payload=None):
        calls.append(params)
        return {"data": [{"id": f"em_{params['status']}"}]}

    monkeypatch.setattr(daemon, "INBOX", "task@primitive.email")
    monkeypatch.setattr(daemon, "PRIMITIVE_EMAIL_STATUSES", ["completed", "accepted"])
    monkeypatch.setattr(daemon, "primitive_request_json", fake_request)
    assert [item["id"] for item in daemon.primitive_list_candidate_summaries()] == [
        "em_completed",
        "em_accepted",
    ]
    assert calls == [
        {"to": "task@primitive.email", "status": "completed", "limit": daemon.PRIMITIVE_POLL_LIMIT},
        {"to": "task@primitive.email", "status": "accepted", "limit": daemon.PRIMITIVE_POLL_LIMIT},
    ]


def test_primitive_download_attachments_extracts_safe_members(monkeypatch, tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        data = b"hello"
        good = tarfile.TarInfo("0_note.txt")
        good.size = len(data)
        archive.addfile(good, io.BytesIO(data))
        bad = tarfile.TarInfo("../escape.txt")
        bad.size = len(data)
        archive.addfile(bad, io.BytesIO(data))

    monkeypatch.setattr(daemon, "ATTACHMENT_ROOT", tmp_path)
    monkeypatch.setattr(daemon, "primitive_request_bytes", lambda _path: buf.getvalue())
    detail = {
        "id": "em_tar",
        "parsed": {
            "attachments": [{"filename": "note.txt", "content_type": "text/plain", "size_bytes": 5}]
        },
    }
    saved = daemon.primitive_download_attachments(detail)
    assert len(saved) == 1
    assert pathlib.Path(saved[0]["path"]).read_text() == "hello"


# --- cmux RPC helper ---------------------------------------------------------


def _fake_completed(stdout: str):
    class _R:
        returncode = 0

    r = _R()
    r.stdout = stdout
    r.stderr = ""
    return r


def test_cmux_rpc_no_params(monkeypatch):
    calls = []
    monkeypatch.setattr(daemon.subprocess, "run",
                        lambda cmd, **k: (calls.append(cmd), _fake_completed('{"surface_id":"U1"}'))[1])
    assert daemon._cmux_rpc("surface.create") == {"surface_id": "U1"}
    assert calls[0] == [daemon.CMUX_BIN, "rpc", "surface.create"]


def test_cmux_rpc_encodes_params(monkeypatch):
    calls = []
    monkeypatch.setattr(daemon.subprocess, "run",
                        lambda cmd, **k: (calls.append(cmd), _fake_completed("{}"))[1])
    daemon._cmux_rpc("surface.send_text", {"surface_id": "S", "text": "hi"})
    import json
    assert calls[0][:3] == [daemon.CMUX_BIN, "rpc", "surface.send_text"]
    assert json.loads(calls[0][3]) == {"surface_id": "S", "text": "hi"}


def test_cmux_rpc_empty_stdout(monkeypatch):
    monkeypatch.setattr(daemon.subprocess, "run", lambda *a, **k: _fake_completed("  "))
    assert daemon._cmux_rpc("surface.refresh") == {}


# --- cmux open / send --------------------------------------------------------


def test_cmux_open_session_returns_surface_id_when_ready(monkeypatch):
    monkeypatch.setattr(daemon, "_cmux_rpc", lambda m, p=None: {"surface_id": "SID"})
    monkeypatch.setattr(daemon, "_surface_text", lambda s: "... bypass permissions on ...")
    monkeypatch.setattr(daemon.time, "sleep", lambda *_: None)
    assert daemon._cmux_open_session() == "SID"


def test_cmux_open_session_raises_without_id(monkeypatch):
    monkeypatch.setattr(daemon, "_cmux_rpc", lambda m, p=None: {})
    with pytest.raises(RuntimeError):
        daemon._cmux_open_session()


def test_cmux_send_pointer_text_then_enter(monkeypatch):
    calls = []
    monkeypatch.setattr(daemon, "_cmux_rpc", lambda m, p=None: calls.append((m, p)))
    monkeypatch.setattr(daemon.time, "sleep", lambda *_: None)
    daemon._cmux_send_pointer("SID", "one line")
    assert calls == [
        ("surface.send_text", {"surface_id": "SID", "text": "one line"}),
        ("surface.send_key", {"surface_id": "SID", "key": "enter"}),
    ]


def test_cmux_send_pointer_rejects_multiline():
    with pytest.raises(ValueError):
        daemon._cmux_send_pointer("SID", "line1\nline2")


# --- ghostty backend ---------------------------------------------------------


def test_ghostty_open_session_runs_osascript(monkeypatch):
    calls = []
    monkeypatch.setattr(daemon.subprocess, "run",
                        lambda cmd, **k: (calls.append(cmd), _fake_completed(""))[1])
    handle = daemon._ghostty_open_session()
    assert handle == "ghostty:frontmost"
    assert calls[0][0] == "osascript"


def test_ghostty_send_pointer_pbcopy_then_paste(monkeypatch):
    cmds = []
    monkeypatch.setattr(daemon.subprocess, "run",
                        lambda cmd, **k: (cmds.append(cmd[0]), _fake_completed(""))[1])
    daemon._ghostty_send_pointer("ghostty:frontmost", "one line pointer")
    assert cmds == ["pbcopy", "osascript"]


# --- dispatch routing --------------------------------------------------------


def test_dispatch_routes_to_selected_backend_and_writes_file(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "PROMPT_ROOT", tmp_path / "prompts")
    monkeypatch.setattr(daemon, "TERMINAL", "cmux")
    sent = {}
    monkeypatch.setattr(daemon, "BACKENDS", {
        "cmux": (lambda: "SID-X", lambda h, p: sent.update(handle=h, pointer=p)),
    })
    daemon.dispatch_to_claude_code("From: a\nSubject: b\n\nbody", "a@example.com", "<m-1@x>")
    prompt_file = tmp_path / "prompts" / "m-1_x.md"
    assert prompt_file.read_text() == "From: a\nSubject: b\n\nbody"
    assert sent["handle"] == "SID-X"
    assert str(prompt_file) in sent["pointer"] and "\n" not in sent["pointer"]


def test_dispatch_unknown_terminal_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "PROMPT_ROOT", tmp_path / "prompts")
    monkeypatch.setattr(daemon, "TERMINAL", "wezterm")
    called = []
    monkeypatch.setattr(daemon, "BACKENDS", {"cmux": (lambda: called.append("open"), None)})
    daemon.dispatch_to_claude_code("p", "a@example.com", "m2")
    assert called == []  # unknown terminal: logged + returns, no backend invoked


def test_dispatch_aborts_when_open_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "PROMPT_ROOT", tmp_path / "prompts")
    monkeypatch.setattr(daemon, "TERMINAL", "cmux")
    sent = []

    def boom():
        raise RuntimeError("no socket")

    monkeypatch.setattr(daemon, "BACKENDS", {"cmux": (boom, lambda h, p: sent.append(p))})
    result = daemon.dispatch_to_claude_code("p", "a@example.com", "m3")
    assert sent == []
    assert result is False


def test_dispatch_returns_true_on_success(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "PROMPT_ROOT", tmp_path / "prompts")
    monkeypatch.setattr(daemon, "TERMINAL", "cmux")
    monkeypatch.setattr(daemon, "BACKENDS", {"cmux": (lambda: "SID", lambda h, p: None)})
    assert daemon.dispatch_to_claude_code("p", "a@example.com", "m4") is True


def test_dispatch_unknown_terminal_returns_false(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "PROMPT_ROOT", tmp_path / "prompts")
    monkeypatch.setattr(daemon, "TERMINAL", "wezterm")
    monkeypatch.setattr(daemon, "BACKENDS", {"cmux": (lambda: "SID", lambda h, p: None)})
    assert daemon.dispatch_to_claude_code("p", "a@example.com", "m5") is False


class _FakeMessages:
    def __init__(self):
        self.updated = []
        self.replied = []

    def update(self, **kw):
        self.updated.append(kw)

    def reply(self, **kw):
        self.replied.append(kw)


class _FakeInboxes:
    def __init__(self):
        self.messages = _FakeMessages()


class _FakeClient:
    def __init__(self):
        self.inboxes = _FakeInboxes()


class _Msg:
    def __init__(self):
        self.message_id = "<m-h@x>"
        self.subject = "do a thing"
        self.text = "body"
        self.labels = ["received", "unread"]
        self.attachments = []
        self.from_ = "Allowed <a@example.com>"


class _Ev:
    def __init__(self):
        self.message = _Msg()


def _prep_handle(monkeypatch, tmp_path, ok):
    monkeypatch.setattr(daemon, "PROMPT_ROOT", tmp_path / "prompts")
    monkeypatch.setattr(daemon, "ALLOWED_FROM", {"a@example.com"})
    monkeypatch.setattr(daemon, "download_attachments", lambda *a: [])
    monkeypatch.setattr(daemon, "dispatch_to_claude_code", lambda *a: ok)


def test_handle_marks_read_only_on_success(monkeypatch, tmp_path):
    _prep_handle(monkeypatch, tmp_path, ok=True)
    client = _FakeClient()
    daemon.handle_message(client, _Ev())
    assert client.inboxes.messages.updated and not client.inboxes.messages.replied


def test_handle_leaves_unread_and_replies_on_failure(monkeypatch, tmp_path):
    _prep_handle(monkeypatch, tmp_path, ok=False)
    client = _FakeClient()
    daemon.handle_message(client, _Ev())
    # not marked read; a failure reply was bounced back instead.
    assert not client.inboxes.messages.updated
    assert client.inboxes.messages.replied


def _primitive_detail():
    return {
        "id": "em_primitive",
        "from_header": "Allowed <a@example.com>",
        "from_email": "a@example.com",
        "subject": "do a primitive thing",
        "body_text": "body",
        "status": "completed",
        "analysis": {"sender": {"authenticated": True}},
        "parsed": {"attachments": []},
    }


def test_handle_primitive_email_success_marks_processed(monkeypatch):
    state = {"processed": []}
    monkeypatch.setattr(daemon, "ALLOWED_FROM", {"a@example.com"})
    monkeypatch.setattr(daemon, "primitive_download_attachments", lambda _detail: [])
    monkeypatch.setattr(daemon, "dispatch_to_claude_code", lambda *a: True)
    daemon.handle_primitive_email(_primitive_detail(), state)
    assert state["processed"] == ["em_primitive"]


def test_handle_primitive_email_failure_replies_once_and_marks_processed(monkeypatch):
    state = {"processed": []}
    replies = []
    monkeypatch.setattr(daemon, "ALLOWED_FROM", {"a@example.com"})
    monkeypatch.setattr(daemon, "primitive_download_attachments", lambda _detail: [])
    monkeypatch.setattr(daemon, "dispatch_to_claude_code", lambda *a: False)
    monkeypatch.setattr(daemon, "primitive_reply_failure", lambda detail, subject: replies.append((detail["id"], subject)))
    daemon.handle_primitive_email(_primitive_detail(), state)
    assert replies == [("em_primitive", "do a primitive thing")]
    assert state["processed"] == ["em_primitive"]
