"""Tests for webhook_server.

The module is imported with no config.json present (it is gitignored), so the
routing tables start empty and import has no side effects beyond defining
defaults. Tests monkeypatch the module globals they need.
"""

import json
import threading
import types
import urllib.request
from http.server import HTTPServer

import pytest

import webhook_server as ws


# ---------------------------------------------------------------------------
# build_bots — config parsing / routing tables
# ---------------------------------------------------------------------------


def test_build_bots_basic_mapping():
    config = {"agents": [{"name": "asst", "display_name": "Assistant", "token": "111:AAA"}]}
    bots, names, t2a = ws.build_bots(config)
    assert bots == {"asst": "111:AAA"}
    assert names == {"asst": "Assistant"}
    assert t2a == {"111": "asst"}


def test_build_bots_display_name_defaults_to_name():
    bots, names, _ = ws.build_bots({"agents": [{"name": "coder", "token": "222:BBB"}]})
    assert names == {"coder": "coder"}


def test_build_bots_empty_config():
    assert ws.build_bots({}) == ({}, {}, {})


def test_build_bots_missing_token_raises():
    with pytest.raises(ValueError, match="missing required key"):
        ws.build_bots({"agents": [{"name": "x"}]})


def test_build_bots_missing_name_raises():
    with pytest.raises(ValueError, match="missing required key"):
        ws.build_bots({"agents": [{"token": "9:Z"}]})


def test_build_bots_duplicate_prefix_raises():
    config = {"agents": [
        {"name": "a", "token": "100:AAA"},
        {"name": "b", "token": "100:BBB"},
    ]}
    with pytest.raises(ValueError, match="duplicate bot token prefix"):
        ws.build_bots(config)


# ---------------------------------------------------------------------------
# route_agent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path,expected", [
    ("/webhook/111", "asst"),
    ("webhook/111", "asst"),
    ("/webhook/999", None),     # unknown prefix
    ("/webhook", None),         # too short
    ("/health", None),          # not a webhook path
    ("/", None),
])
def test_route_agent(path, expected):
    assert ws.route_agent(path, {"111": "asst"}) == expected


# ---------------------------------------------------------------------------
# extract_message
# ---------------------------------------------------------------------------


def test_extract_message_plain_text():
    update = {"message": {"chat": {"id": 42}, "text": "hello"}}
    assert ws.extract_message(update) == ("42", "hello")


def test_extract_message_edited_message_fallback():
    update = {"edited_message": {"chat": {"id": 7}, "text": "edited"}}
    assert ws.extract_message(update) == ("7", "edited")


def test_extract_message_caption_fallback():
    update = {"message": {"chat": {"id": 7}, "caption": "a photo"}}
    assert ws.extract_message(update) == ("7", "a photo")


def test_extract_message_no_message():
    assert ws.extract_message({"channel_post": {}}) == (None, None)


def test_extract_message_empty_text():
    update = {"message": {"chat": {"id": 7}}}
    assert ws.extract_message(update) == ("7", "")


# ---------------------------------------------------------------------------
# is_authorized — the security boundary (fail-closed)
# ---------------------------------------------------------------------------


def test_is_authorized_allows_listed_chat():
    assert ws.is_authorized("5", {"5", "6"}) is True


def test_is_authorized_rejects_unlisted_chat():
    assert ws.is_authorized("9", {"5", "6"}) is False


def test_is_authorized_empty_allowlist_denies_everyone():
    # Regression guard: an empty allowlist must NOT mean "allow all".
    assert ws.is_authorized("5", set()) is False
    assert ws.is_authorized("", set()) is False


# ---------------------------------------------------------------------------
# chunk_response — boundary behavior
# ---------------------------------------------------------------------------


def test_chunk_response_short():
    assert ws.chunk_response("hi") == ["hi"]


def test_chunk_response_exactly_one_chunk():
    text = "x" * 4000
    assert ws.chunk_response(text) == [text]


def test_chunk_response_just_over_splits():
    text = "x" * 4001
    chunks = ws.chunk_response(text)
    assert len(chunks) == 2
    assert chunks[0] == "x" * 4000
    assert chunks[1] == "x"


def test_chunk_response_multiple():
    text = "y" * 9000
    chunks = ws.chunk_response(text)
    assert [len(c) for c in chunks] == [4000, 4000, 1000]
    assert "".join(chunks) == text


def test_chunk_response_empty_yields_nothing():
    # Documents the boundary: empty input produces no chunks. process_message
    # never reaches here with an empty response (run_claude always returns a
    # non-empty fallback), which is what guarantees the user always gets a reply.
    assert ws.chunk_response("") == []


# ---------------------------------------------------------------------------
# save_history
# ---------------------------------------------------------------------------


def test_save_history_writes_jsonl(tmp_path):
    ws.save_history("asst", "user", "hello", history_dir=tmp_path)
    lines = (tmp_path / "asst.jsonl").read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["role"] == "user"
    assert entry["text"] == "hello"
    assert "ts" in entry


def test_save_history_appends(tmp_path):
    ws.save_history("asst", "user", "one", history_dir=tmp_path)
    ws.save_history("asst", "assistant", "two", history_dir=tmp_path)
    lines = (tmp_path / "asst.jsonl").read_text().splitlines()
    assert [json.loads(x)["text"] for x in lines] == ["one", "two"]


def test_save_history_preserves_unicode(tmp_path):
    ws.save_history("asst", "user", "привет 👋", history_dir=tmp_path)
    raw = (tmp_path / "asst.jsonl").read_text()
    assert "привет 👋" in raw  # ensure_ascii=False


# ---------------------------------------------------------------------------
# run_claude — subprocess outcomes mapped to user-facing text
# ---------------------------------------------------------------------------


def test_run_claude_success(monkeypatch):
    monkeypatch.setattr(ws.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout="  the answer  ", stderr=""))
    assert ws.run_claude("hi", "Agent") == "the answer"


def test_run_claude_empty_stdout_fallback(monkeypatch):
    monkeypatch.setattr(ws.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout="   ", stderr=""))
    assert ws.run_claude("hi", "Agent") == "Could not process. Try again."


def test_run_claude_timeout(monkeypatch):
    def raise_timeout(*a, **k):
        raise ws.subprocess.TimeoutExpired(cmd="claude", timeout=120)
    monkeypatch.setattr(ws.subprocess, "run", raise_timeout)
    assert ws.run_claude("hi", "Agent") == "Request timed out."


def test_run_claude_generic_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(ws.subprocess, "run", boom)
    assert ws.run_claude("hi", "Agent") == "Error: boom"


# ---------------------------------------------------------------------------
# process_message — orchestration
# ---------------------------------------------------------------------------


def _patch_process_message(monkeypatch, tmp_path, stdout="hello"):
    calls = []
    monkeypatch.setattr(ws, "BOTS", {"a": "111:tok"})
    monkeypatch.setattr(ws, "BOT_NAMES", {"a": "Agent A"})
    monkeypatch.setattr(ws, "HISTORY_DIR", tmp_path)
    monkeypatch.setattr(ws, "tg_call",
                        lambda token, method, data: calls.append((method, data)))
    monkeypatch.setattr(ws.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout=stdout, stderr=""))
    return calls


def test_process_message_sends_response(monkeypatch, tmp_path):
    calls = _patch_process_message(monkeypatch, tmp_path, stdout="the answer")
    ws.process_message("a", "question", "5")
    sends = [data["text"] for method, data in calls if method == "sendMessage"]
    assert sends == ["the answer"]


def test_process_message_chunks_long_response(monkeypatch, tmp_path):
    calls = _patch_process_message(monkeypatch, tmp_path, stdout="z" * 9000)
    ws.process_message("a", "question", "5")
    sends = [data["text"] for method, data in calls if method == "sendMessage"]
    assert [len(s) for s in sends] == [4000, 4000, 1000]


def test_process_message_records_history(monkeypatch, tmp_path):
    _patch_process_message(monkeypatch, tmp_path, stdout="reply")
    ws.process_message("a", "ask", "5")
    lines = (tmp_path / "a.jsonl").read_text().splitlines()
    roles = [json.loads(x)["role"] for x in lines]
    assert roles == ["user", "assistant"]


# ---------------------------------------------------------------------------
# tg_call — URL construction and error swallowing
# ---------------------------------------------------------------------------


def test_tg_call_builds_request(monkeypatch):
    captured = {}

    class FakeResp:
        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = req.data
        return FakeResp()

    monkeypatch.setattr(ws.urllib.request, "urlopen", fake_urlopen)
    ws.tg_call("111:tok", "sendMessage", {"chat_id": "5", "text": "hi"})
    assert captured["url"] == "https://api.telegram.org/bot111:tok/sendMessage"
    assert b"chat_id=5" in captured["data"]


def test_tg_call_swallows_network_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(ws.urllib.request, "urlopen", boom)
    ws.tg_call("t", "m", {})  # must not raise


# ---------------------------------------------------------------------------
# Integration — drive WebhookHandler over a real socket
# ---------------------------------------------------------------------------


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(ws, "TOKEN_TO_AGENT", {"111": "a"})
    monkeypatch.setattr(ws, "BOTS", {"a": "111:tok"})
    monkeypatch.setattr(ws, "ALLOWED_CHATS", {"5"})
    monkeypatch.setattr(ws, "tg_call", lambda *a, **k: None)

    httpd = HTTPServer(("127.0.0.1", 0), ws.WebhookHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    thread.join(timeout=5)


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read()


def test_post_authorized_message_is_processed(server, monkeypatch):
    httpd, base = server
    done = threading.Event()
    recorded = []

    def fake_process(agent, text, chat_id):
        recorded.append((agent, text, chat_id))
        done.set()
    monkeypatch.setattr(ws, "process_message", fake_process)

    status, body = _post(f"{base}/webhook/111",
                         {"message": {"chat": {"id": 5}, "text": "hi"}})
    assert status == 200
    assert json.loads(body) == {"ok": True}
    assert done.wait(5)
    assert recorded == [("a", "hi", "5")]


def test_post_unauthorized_chat_is_not_processed(server, monkeypatch):
    httpd, base = server
    recorded = []
    monkeypatch.setattr(ws, "process_message",
                        lambda *a: recorded.append(a))

    status, _ = _post(f"{base}/webhook/111",
                     {"message": {"chat": {"id": 999}, "text": "hi"}})
    assert status == 200      # still 200 so Telegram doesn't retry
    assert recorded == []     # but not processed


def test_post_unknown_token_prefix_is_not_processed(server, monkeypatch):
    httpd, base = server
    recorded = []
    monkeypatch.setattr(ws, "process_message",
                        lambda *a: recorded.append(a))

    status, _ = _post(f"{base}/webhook/000",
                     {"message": {"chat": {"id": 5}, "text": "hi"}})
    assert status == 200
    assert recorded == []


def test_get_health(server):
    httpd, base = server
    with urllib.request.urlopen(f"{base}/", timeout=5) as resp:
        payload = json.loads(resp.read())
    assert payload["status"] == "ok"
    assert payload["agents"] == ["a"]
