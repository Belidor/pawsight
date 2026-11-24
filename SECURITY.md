# Security policy

pawsight watches the inside of someone's home, so security problems matter
more than usual. Thank you for reporting them responsibly.

## Reporting a vulnerability

Please **do not open a public issue**. Report privately through GitHub:
[Security → Report a vulnerability](https://github.com/Belidor/pawsight/security/advisories/new).

Include what an attacker could do, the steps to reproduce, and the version or
commit. You should get a first answer within a week. Fixes are released as soon
as they are ready, and you are credited in the advisory unless you prefer not to be.

## Supported versions

Only the latest release and the `main` branch get security fixes.

## Design assumptions

Knowing these helps judge whether something is a vulnerability:

- **Everything listens on `127.0.0.1`.** The dashboard, go2rtc, the MQTT broker
  and Ollama are not meant to be reachable from the network. Exposing them on
  purpose (port forwarding, `0.0.0.0`) is outside the supported setup.
- **Browsers are part of the threat model.** Any web page you open can send
  requests to `127.0.0.1`. The dashboard checks `Host` and `Origin` and requires
  a custom `X-Pawsight` header on every state-changing request; go2rtc and the
  broker have passwords on new installs. A way around these checks from a web
  page is a vulnerability.
- **Secrets live in two files only**: `.env` and `go2rtc/go2rtc.yaml`, both
  mode 600 and ignored by git. A secret appearing in logs, the database, API
  responses or the dashboard is a vulnerability.
- **Telegram** commands are accepted only from the chat IDs in
  `TELEGRAM_CHAT_ID`.
