# CLAUDE.md

Guidance for Claude Code and other coding agents working in this repository.
`AGENTS.md` points here.

## What This Is

A Home Assistant custom integration (HACS) for local control of Duosida EV
wallboxes over TCP `9988` (OCPP 1.6 encoded as Protobuf). Config flow from the
UI (IP + port), one persistent session per wallbox, push updates to entities.

## Commands

```bash
python -m venv .venv && .venv/Scripts/python -m pip install pytest pytest-asyncio   # Windows
python -m pytest -q                       # all tests
python -m pytest -q tests/test_client.py  # client against a simulated wallbox
```

`tests/test_protocol.py` and `tests/test_client.py` need no Home Assistant;
`tests/conftest.py` loads `protocol.py` and `client.py` under a stub package.
`tests/test_init.py` needs `pytest-homeassistant-custom-component`.

## Layout

| File | Role |
|---|---|
| `protocol.py` | Pure: Protobuf primitives, frame splitter, message parsers and builders. No I/O, no HA imports |
| `client.py` | `DuosidaClient`: persistent asyncio session, polling, commands, transaction handling. No HA imports |
| `__init__.py` | Entry setup: client + push `DataUpdateCoordinator`, platforms, unload |
| `config_flow.py` | User step (host, port, identity probe), options (poll interval) |
| `entity.py` | Base entity: device info, availability = session connected |
| `sensor.py`, `number.py`, `switch.py`, `button.py` | Thin entity wrappers around the client |

## Wallbox Rules (learned on real hardware, do not break)

- **One session only.** Never open a second TCP session while one is open
  (the config flow aborts on an existing entry *before* probing). A second
  session once locked the port until a power cycle.
- **Slow reconnects.** After a close the wallbox keeps the slot ~30 s busy.
  Reconnect delays start at 60 s. Never reconnect in a tight loop.
- **Never send BootNotificationConf.** Only the opening TriggerMessage.
- **Confirm transactions.** The wallbox sends StartTransaction/StopTransaction
  to the local session and retries until confirmed. StartTransactionConf uses
  an empty idTagInfo (status defaults to Accepted) and a local transaction id.
- **Do not burst.** 0.2 s between outgoing messages (802.11b module).
- Wi-Fi passwords from the configuration pull must never be stored or logged.

## Conventions

- **No co-author lines in commits.** Sole author is `ZaBug`. Never add
  `Co-Authored-By:` trailers. Use the local repo identity
  (`git config user.name` must be `ZaBug`).
- **English only** for commits, PRs, release notes, tags, code comments, logs
  and docs, even when the conversation is in another language.
- Conventional commit prefixes: `feat:`, `fix:`, `refactor:`, `chore:`, `docs:`,
  `test:`. Version bump commits carry `(vX.Y.Z)` in the message.
- **Version sync**: `manifest.json` `version` and `const.py`
  `INTEGRATION_VERSION` must match.
- `manifest.json` `requirements` stays empty; stdlib + HA APIs only.
- All `.py` and `.json` files are UTF-8 without BOM.
- `strings.json` and `translations/en.json` stay identical.

## HA Quality Rules

- Every entity implements `available` (via `DuosidaEntity`).
- Listeners and tasks are cancelled on unload (`entry.async_on_unload`,
  `client.stop()`).
- Entities call only public client methods.
- Never hardcode `suggested_area` in `DeviceInfo`.
- Type hints: `str | None`, not `Optional[str]`.
