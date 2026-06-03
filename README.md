# agentmail-to-claude-code

Email gateway for [Claude Code](https://claude.com/claude-code). Subscribe an
[AgentMail](https://agentmail.to) inbox over WebSocket, or poll a
[Primitive](https://primitive.dev) inbox over REST; on every authenticated,
allowlisted message the daemon opens a fresh Claude Code session in your
terminal, writes the email to a prompt file, and tells Claude to read and act
on it.

Send an email to the watched inbox and Claude Code starts working on whatever's
in the subject and body, with any attachments available to it by path.

The point: because the trigger is just an email, you can put Claude Code to work
on your own computer from anywhere. I've wired it to a `cc` command in my agent
Hermes, so from my phone I run `cc <task>` and it lands as a working session on
my Mac, no VPN or SSH into the machine. See [How I use it with Hermes](#how-i-use-it-with-hermes).

It drives either terminal:

- **cmux** ([cmux.com](https://cmux.com)) via its Unix-socket control plane, or
- **standalone Ghostty** ([ghostty.org](https://ghostty.org)) via AppleScript.

Pick one with `CC_TERMINAL` in your env.

## How it works

1. `cc-daemon.py` connects to the selected provider:
   - `CC_MAIL_PROVIDER=agentmail` subscribes to AgentMail's WebSocket for `AGENTMAIL_INBOX`.
   - `CC_MAIL_PROVIDER=primitive` polls Primitive for `PRIMITIVE_INBOX`.
2. On each incoming message, the handler drops anything whose sender isn't in `CC_ALLOWED_FROM` or that failed DKIM/SPF.
3. Attachments are downloaded under `CC_HOME/attachments/<message-id>/`. Per-attachment failures log a warning and continue.
4. A prompt is built from `From:`, `Subject:`, the body, and a footer listing every downloaded attachment by absolute path. It's written to `CC_HOME/prompts/<message-id>.md`.
5. The selected backend opens a new Claude Code session and sends a single-line pointer telling Claude to read and act on the prompt file:
   - **cmux:** `surface.create` (auto-launches Claude Code via the Ghostty config cmux reads) -> poll `surface.read_text` until ready -> `surface.send_text` the pointer -> `surface.send_key enter`. Every call targets a specific `surface_id`, so concurrent emails never collide.
   - **ghostty:** `osascript` activates Ghostty and opens a new tab (Ghostty auto-launches Claude Code in new tabs), then pastes the pointer via the clipboard and presses Return.
6. On a successful dispatch the message is marked read via the AgentMail REST API. If the dispatch fails (terminal unreachable), the message is left **unread** and a reply is bounced back to the sender so the task is never silently lost. `surface.create` is retried with backoff to ride out a briefly unresponsive cmux.

The email is referenced by file path rather than typed inline, which keeps multi-line bodies intact across both backends. Image attachments are read by Claude with the Read tool from the paths in the prompt file.

## Requirements

- macOS (AppleScript, launchd)
- Python 3.12+
- Either:
  - an [AgentMail](https://agentmail.to) account with an inbox-scoped API key, or
  - a [Primitive](https://primitive.dev) account with a receiving domain and API token
- One of:
  - **cmux** installed, or
  - **Ghostty** installed
- Claude Code, configured to auto-launch in a new terminal session (see your chosen backend below)

## Setup

```bash
git clone https://github.com/mvanhorn/agentmail-to-claude-code.git
cd agentmail-to-claude-code
python3 -m venv venv
./venv/bin/pip install agentmail httpx pytest

cp cc.env.example cc.env
$EDITOR cc.env   # fill in provider credentials, inbox, CC_ALLOWED_FROM, CC_TERMINAL
chmod 600 cc.env

# Verify it connects to the selected provider
./run-daemon.sh
```

`CC_ALLOWED_FROM` is the gate on who can drive your machine. Keep it to addresses you control. Mail from anyone else, or anything that fails DKIM/SPF, is dropped before a session is ever opened.

### Primitive backend

Primitive does not require provisioning each role inbox. After your Primitive
domain is receiving, any local-part at that domain can receive mail, so you can
use readable addresses like `taskcodex@your-domain.primitive.email` or
`calendar@your-domain.primitive.email`.

```bash
export CC_MAIL_PROVIDER=primitive
export PRIMITIVE_AUTH_TOKEN=your-primitive-bearer-token
export PRIMITIVE_INBOX=taskcodex@your-domain.primitive.email
```

The Primitive backend polls recent `completed` and `accepted` messages, stores
processed IDs in `CC_HOME/state.json`, and uses the same allowlist,
authentication check, attachment download, and terminal dispatch flow as the
AgentMail backend.

## Choose your terminal

The daemon needs your terminal to start Claude Code automatically when a new session opens. Both backends rely on a small launcher script so Claude restarts cleanly if it exits:

```bash
# ~/.local/bin/claude-launcher.sh
#!/bin/zsh
caffeinate -dimsu -- claude --dangerously-skip-permissions
echo "Claude exited. Type 'claude' to relaunch or 'exit' to close."
exec /bin/zsh -li
```

```bash
chmod +x ~/.local/bin/claude-launcher.sh
```

`--dangerously-skip-permissions` lets the emailed task run without prompting. Only use it on a machine and inbox you trust, with a tight `CC_ALLOWED_FROM`.

### Option A: cmux

cmux runs Ghostty surfaces and reads `~/.config/ghostty/config`, so point Ghostty's `command` at the launcher:

```
# ~/.config/ghostty/config
command = /Users/YOUR_USERNAME/.local/bin/claude-launcher.sh
```

Set `CC_TERMINAL=cmux` in `cc.env`. The daemon calls the cmux CLI by absolute path; if cmux isn't at the default location, set `CMUX_BIN`.

**Required: open cmux's socket to the daemon.** cmux's RPC socket defaults to `automation.socketControlMode = "cmuxOnly"`, which only accepts connections from processes started *inside* cmux. This daemon runs under launchd and is **not** a cmux child, so with the default it is rejected on every dispatch (`Failed to write to socket (Broken pipe)` or `Access denied - only processes started inside cmux can connect`). The same `cmux rpc` call works from an interactive shell because that shell *is* a cmux child, which makes this easy to miss.

Run the helper to set a mode that lets the daemon in:

```bash
# allowAll: any local process running as you can drive cmux (no password).
# The socket is user-only, so marginal risk is low. Simplest.
python3 setup_cmux.py --mode allowAll

# password: the daemon must present CMUX_SOCKET_PASSWORD. Tighter.
python3 setup_cmux.py --mode password --password "$(openssl rand -hex 20)"
# then add the same value as CMUX_SOCKET_PASSWORD in cc.env
```

**You must fully restart cmux (Quit + reopen) afterward.** `socketControlMode` is read at app launch only; `cmux reload-config` does not apply it and there is no live RPC to set it. Verify with `cmux capabilities | grep access_mode` (it should no longer say `cmuxOnly`).

### Option B: standalone Ghostty

Configure Ghostty to launch the launcher in new tabs:

```
# ~/.config/ghostty/config
command = /Users/YOUR_USERNAME/.local/bin/claude-launcher.sh
```

Set `CC_TERMINAL=ghostty` in `cc.env`. The Ghostty backend uses AppleScript and System Events, so grant your terminal/automation Accessibility permission under System Settings -> Privacy & Security -> Accessibility.

## Install as a launchd job

```bash
# Point the plist at this repo
sed "s#__INSTALL_DIR__#$(pwd)#g" com.agentmail.cc.plist.example > ~/Library/LaunchAgents/com.agentmail.cc.plist

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.agentmail.cc.plist
launchctl kickstart gui/$(id -u)/com.agentmail.cc
```

Reload after editing the daemon:

```bash
launchctl kickstart -k gui/$(id -u)/com.agentmail.cc
```

## Sending a task to the inbox

The daemon reacts to inbound mail, so anything that can email the watched inbox
from an allowlisted address can hand Claude Code a task: another agent, a cron
job, a shortcut, your phone. `examples/send_task.py` is a minimal sender built
on the AgentMail REST API:

```bash
AGENTMAIL_API_KEY=... AGENTMAIL_SEND_INBOX=your-agent@agentmail.to \
  python3 examples/send_task.py you@agentmail.to "Resize these" "Crop to 1:1" ~/Desktop/pic.png
```

The sending inbox must be listed in the daemon's `CC_ALLOWED_FROM`. An optional
image is attached as base64 that's read and encoded in-process, so when an agent
shells out to this script the raw image bytes never pass through its LLM context
(the agent passes a file path, not the pixels). Both the API key and the sending
inbox come from the environment; nothing is hardcoded.

With Primitive, you can send through the Primitive CLI or API. The sender still
has to be listed in `CC_ALLOWED_FROM`:

```bash
primitive send \
  --from agent@your-domain.primitive.email \
  --to taskcodex@your-domain.primitive.email \
  --subject "Resize these" \
  --body "Crop to 1:1" \
  --wait
```

### How I use it with Hermes

I've wired the sender up as a `cc` command in Hermes, my agent, so dispatching
to my desktop is one shot. From my phone I run `cc <task>` and Hermes fires the
email to my main computer's inbox; the daemon spins up a Claude Code session
right there on the Mac and starts working. No VPN, no SSH into the box, just a
command that turns into an email.

The in-process base64 is why the sender is a separate script: I can hand `cc` a
screenshot and Hermes attaches it to the task without the image bytes ever
entering its LLM context, it just passes a file path.

### Giving an agent its own inbox

The sending agent needs its own AgentMail inbox. An agent can sign itself up,
no console clicking, via the SDK's agent flow (a one-time code lands in your
email to verify):

```python
from agentmail import AgentMail
client = AgentMail()
client.agent.sign_up(human_email="you@example.com", username="my-agent")
client.agent.verify(otp_code="123456")          # code from the email
inbox = client.inboxes.create(display_name="My Agent")  # -> my-agent@agentmail.to
```

Or just point a coding agent at AgentMail's agent docs and let it do the whole
thing: "read https://docs.agentmail.to/llms.txt and make yourself an email
address." There's also an MCP server (`npx -y agentmail-mcp`) for tool-based
clients. Add the new inbox to the daemon's `CC_ALLOWED_FROM` so it's allowed to
dispatch.

## Tests

```bash
./venv/bin/pytest test_cc_daemon.py -v
```

## Security notes

- The API key lives only in `cc.env` (gitignored). It is never committed.
- `CC_ALLOWED_FROM` plus AgentMail's DKIM/SPF labeling is the trust boundary. A new Claude Code session is opened only for allowlisted, authenticated senders.
- Sessions run `claude --dangerously-skip-permissions`. Treat the watched inbox as a remote control for your machine and scope the allowlist accordingly.

## Files

- `cc-daemon.py` - the daemon (both backends)
- `setup_cmux.py` - one-shot helper to open cmux's socket to the daemon (cmux backend)
- `run-daemon.sh` - sources `cc.env` and execs the daemon
- `test_cc_daemon.py` - unit tests
- `com.agentmail.cc.plist.example` - launchd job template
- `cc.env.example` - env template
- `examples/send_task.py` - minimal sender to trigger the daemon from any allowlisted inbox
