# Library Integration Risks

Where `evennia-shards` couples to Evennia internals, and the risks each coupling carries. Two audiences read this doc:

- **Library maintainer upgrading Evennia** — diff each coupling against the new Evennia version.
- **Consumer game subclassing or customising Evennia** — know which Evennia surfaces the library has already taken a position on, and the recommended composition pattern.

Each coupling section follows the same template:

- **What we patch / extend** — the Evennia symbol the library touches, and where the library code lives.
- **Why** — the requirement that drives the coupling.
- **Risk on Evennia upgrade** — what to diff in the new Evennia version.
- **Risk in consumer override** — what consumer-side customisation would collide, and the recommended pattern.

This doc is filled in lazily — a coupling is added when it first lands or is next touched. Currently covered: `WebSocketClient.onOpen`, `DefaultAccount.at_post_login`, `evennia._init()` wrap + `CharacterCmdSet.at_cmdset_creation`. Other library couplings (CmdIC/CmdOOC patches, ObjectDB.from_db, pre_save/pre_delete signals, QuerySet.update, WEBSOCKET_PROTOCOL_CLASS rewiring, middleware injection) will be backfilled as we revisit them.

## WebSocketClient.onOpen() override

**What we patch / extend:** `evennia/server/portal/webclient.py` → `WebSocketClient.onOpen()`. Library code: `evennia_shards/protocols.py` → `ShardWebSocketClient.onOpen()`. Based on Evennia 6.0.0.

**Why:**

Ticket-based auth needs `self.uid` and `self.logged_in` set on the session *after* `init_session()` (which resets both to `None`/`False`) but *before* `sessionhandler.connect()` (which snapshots session state via `get_sync_data()` and sends it to the Server over AMP). The Server's `portal_connect()` auto-logins sessions that arrive with `logged_in=True` and a valid `uid`.

There is no method-level seam between `init_session()` and `sessionhandler.connect()` — both are called inline in `onOpen()`. Three alternatives were ruled out:

- **Swapping call order** (ticket validation before `super().onOpen()`): `init_session()` wipes `uid`/`logged_in`, and `self.address` isn't set yet for IP validation.
- **Post-connect re-sync** (`sessionhandler.sync()`): the Portal's `sync()` deliberately excludes `uid` and `logged_in` from the data it sends to the Server. There is no Portal→Server "please login this session" AMP operation.
- **Overriding `get_sync_data()`**: works mechanically but splits the auth logic across two methods with non-obvious interaction, and makes a data-serialisation method responsible for auth decisions.

Overriding `onOpen()` is the only clean approach. Our override reproduces the parent method and adds a ticket-auth check in the same position as the existing browser-session auth check (between `init_session()` and `sessionhandler.connect()`).

**Risk on Evennia upgrade:**

- New logic added between `init_session()` and `sessionhandler.connect()` — our override would miss it.
- Changes to the `init_session()` / `get_client_session()` / `sessionhandler.connect()` call sequence.
- New protocol flags or connection setup steps added to `onOpen()`.
- Changes to `SESSION_SYNC_ATTRS` that affect what `get_sync_data()` sends.

How to check: diff the upstream `onOpen()` against the snapshot in our override. The override carries a comment citing the Evennia version it was based on.

**Risk in consumer override:**

A consumer that sets a custom `WEBSOCKET_PROTOCOL_CLASS` is **safe by construction**: `AppConfig.ready()` stashes the consumer's class as `_SHARDS_ORIGINAL_WS_PROTOCOL` and `ShardWebSocketClient` subclasses *that* dynamically. Consumer customisations are preserved underneath the library's onOpen logic.

The hazard is a consumer overriding `onOpen()` on their custom class without calling `super().onOpen()` — that bypasses our ticket-auth injection entirely. Recommended pattern: any consumer override of `onOpen()` must call `super().onOpen()` (or accept that ticket-auth will not run on their connections).

## DefaultAccount.at_post_login() override

**What we patch / extend:** `evennia/accounts/accounts.py` → `DefaultAccount.at_post_login`. Library code: `evennia_shards/hooks.py` → `shard_aware_at_post_login`. Installed via monkey-patch in `AppConfig.ready()`, gated on `get_role() == ROLE_ROUTER`. Based on Evennia 6.0.0.

**Why:**

When `AUTO_PUPPET_ON_LOGIN = True` (Evennia's default), `at_post_login` calls `puppet_object(session, self.db._last_puppet)` directly, with no exposed seam between the function's prelude (protocol-flag load, `logged_in` OOB, connect-channel msg) and the if/else that decides whether to puppet. On a router this is doubly broken: `_last_puppet=None` raises and the player sees `"The Character does not exist."`; `_last_puppet=<character on shardN>` puppets the character on the *router* (chokepoint-exempt for reads) and the next save raises `ShardIsolationError`.

The library needs to make a routing decision (redirect-to-shard vs render OOC menu) at exactly the position where Evennia's if/else lives. Three alternatives were considered and rejected:

- **Mutate `_AUTO_PUPPET_ON_LOGIN`** to force the else-branch: it's a module-level cached constant. Mutating it is process-global, races on concurrent logins, and the redirect would still need to happen *after* the else-branch rendered the OOC menu (visible flash before page navigates).
- **Override `puppet_object` instead**: wrong granularity — by the time `puppet_object` is called the prelude has run and the if-branch has been taken. Doesn't handle the `_last_puppet=None` case at all (because `puppet_object` is never called when it raises early). Also broadens blast radius (`puppet_object` is also called from `CmdIC` etc.).
- **Use `SIGNAL_ACCOUNT_POST_LOGIN`**: fires *after* `at_post_login` completes — by which point the puppet has already happened or the OOC menu has rendered. Three state transitions instead of one.

Reproducing the body and swapping the if-branch is the only clean approach. The else-branch (OOC character-select menu via `at_look(target=characters)`) is reproduced for the fallback path so a broken `_last_puppet` lands the player in the OOC menu rather than failing login.

**Risk on Evennia upgrade:**

- New steps added to the prelude (protocol-flag load, `logged_in` OOB, connect-channel msg). Our reproduction would miss them.
- Changes to the if-condition (e.g. new conditions added beyond `AUTO_PUPPET_ON_LOGIN`).
- Changes to the else-branch's OOC-menu rendering — currently `self.msg(self.at_look(target=self.characters, session=session), session=session)`. We reproduce that line.
- Signature or call-order changes around `at_post_login` in `sessionhandler.login()`.

How to check: diff upstream `DefaultAccount.at_post_login` against the snapshot in `shard_aware_at_post_login`. The override carries a comment citing the Evennia version it was based on.

**Risk in consumer override:**

A consumer that subclasses `DefaultAccount` and overrides `at_post_login` without calling `super().at_post_login(...)` will bypass our patch — the consumer's body runs instead, and auto-puppet on the router goes back to its broken default. Recommended pattern: any consumer override of `at_post_login` must call `super().at_post_login(session=session, **kwargs)` (or accept that auto-puppet redirect will not run on their accounts).

A consumer that *doesn't* override `at_post_login` is safe by Python MRO — `account.at_post_login(...)` resolves to our patched `DefaultAccount.at_post_login` automatically.

## `evennia._init()` wrap + `CharacterCmdSet.at_cmdset_creation` override

**What we patch / extend:** `evennia._init` (the function in `evennia/__init__.py` that populates Evennia's lazy top-level exports — `Command`, `CmdSet`, etc.) is wrapped from `AppConfig.ready()` to install a follow-on patch on `evennia.commands.default.cmdset_character.CharacterCmdSet.at_cmdset_creation`. The follow-on patch adds the library's permanent admin commands (`CmdShardCheck`, `CmdCrossShardDig`) after the parent populates the default cmdset. Library code: `evennia_shards/apps.py` (the wrap installation) and `evennia_shards/commands.py` (the commands themselves). Based on Evennia 6.0.0.

**Why:**

The library ships permanent superuser commands ("Shard Management" category) that should appear on every sharded deployment with no consumer-side cmdset wiring required. Evennia's standard cmdset-extension point is `CharacterCmdSet.at_cmdset_creation`: subclass and call `self.add(...)` after `super()`. The library does the equivalent via monkey-patch so consumers get the commands without editing `default_cmdsets.py`.

The `evennia._init()` wrap is the indirection that makes this safe. Importing `cmdset_character` at `AppConfig.ready()` time eagerly pulls the chain `cmdset_character → building → prototypes/menus → evmenu`, and `evmenu.py` does `from evennia import Command` at module level. At `ready()` time `evennia.Command` is still `None` (the lazy-init pattern in `evennia/__init__.py`), so `class CmdEvMenuNode(Command):` fails with `TypeError: NoneType takes no arguments`. Real-runtime entry points (`server.py`, `portal.py`, `evennia_launcher`) call `django.setup()` *before* `evennia._init()`, so any `ready()`-time import of `cmdset_character` would trip this in production too — not just in tests. Wrapping `_init` instead defers the import until the lazy exports are populated.

**Risk on Evennia upgrade:**

- Changes to `evennia._init`'s signature or call order — the wrap calls `_original_init(*args, **kwargs)` and assumes the original returns normally before the lazy exports are populated.
- Changes to `CharacterCmdSet.at_cmdset_creation`'s contract — our wrap calls the original first then `self.add()`s our commands.
- If Evennia ever populates top-level exports at module-load time (no more `_init` indirection), the whole wrap is unnecessary; remove it.

How to check: diff `evennia/__init__.py`'s `_init` and the lazy-export block; diff `cmdset_character.CharacterCmdSet.at_cmdset_creation`. The `AdminCommandAutoInstallTests.test_character_cmdset_contains_library_commands` test is the canary on this patch firing as expected.

**Risk in consumer override:**

A consumer that subclasses `CharacterCmdSet` and calls `super().at_cmdset_creation()` inherits the library's command additions transparently — standard Evennia pattern, works as expected. A consumer that subclasses but **doesn't** call `super()` skips both the default cmdset population *and* the library's additions. Recommended pattern: always call `super().at_cmdset_creation()` first.

A consumer that points `CMDSET_CHARACTER` at a class that doesn't derive from `evennia.commands.default.cmdset_character.CharacterCmdSet` won't get the library commands. They'd need to manually add them (`from evennia_shards.commands import CmdShardCheck, CmdCrossShardDig`) — the library exposes them as part of its public command surface for exactly that case.

The test runner depends on this wrap behaving — `runtests.py` calls `evennia._init()` explicitly so the deferred patch installation runs in time for tests. Real-runtime startup paths already call `evennia._init()` after `django.setup()`, so no additional integration is needed there.
