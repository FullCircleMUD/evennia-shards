# Library Integration Risks

Where `evennia-shards` couples to Evennia internals, and the risks each coupling carries. Two audiences read this doc:

- **Library maintainer upgrading Evennia** — diff each coupling against the new Evennia version.
- **Consumer game subclassing or customising Evennia** — know which Evennia surfaces the library has already taken a position on, and the recommended composition pattern.

Each coupling section follows the same template:

- **What we patch / extend** — the Evennia symbol the library touches, and where the library code lives.
- **Why** — the requirement that drives the coupling.
- **Risk on Evennia upgrade** — what to diff in the new Evennia version.
- **Risk in consumer override** — what consumer-side customisation would collide, and the recommended pattern.

This doc is filled in lazily — a coupling is added when it first lands or is next touched. Currently covered: `WebSocketClient.onOpen`, `DefaultAccount.at_post_login`, `Account.create_character`, `CmdIC` / `CmdOOC` (hard overrides — library territory), `evennia._init()` wrap + `CharacterCmdSet.at_cmdset_creation`, webclient HTML injection (middleware regex match against the rendered template). Other library couplings (`ObjectDB.from_db`, `pre_save`/`pre_delete` signals, `QuerySet.update`, `WEBSOCKET_PROTOCOL_CLASS` rewiring) will be backfilled as we revisit them.

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

**What we patch / extend:** `evennia/accounts/accounts.py` → `DefaultAccount.at_post_login`. Two role-specific patches, both installed via monkey-patch in `AppConfig.ready()`. Based on Evennia 6.0.0.

- **Router** (`get_role() == ROLE_ROUTER`): full replacement → `evennia_shards/hooks.py` → `shard_aware_at_post_login`.
- **Shard** (`get_role() == ROLE_SHARD`): thin wrapper around Evennia's original → `evennia_shards/hooks.py` → `make_shard_at_post_login(original)`. Flushes the `_last_puppet` character from the idmapper and refreshes it from the DB before delegating to the original. Needed because `cross_shard_character_move` on the source shard updates the character's `shard_id` in the DB, but the destination shard's Account Attribute-handler cache may still hold the stale Python object with the old `shard_id` — causing `puppet_object` to trip the `pre_save` chokepoint.

**Why:**

When `AUTO_PUPPET_ON_LOGIN = True` (Evennia's default), `at_post_login` calls `puppet_object(session, self.db._last_puppet)` directly, with no exposed seam between the function's prelude (protocol-flag load, `logged_in` OOB, connect-channel msg) and the if/else that decides whether to puppet. On a router this is doubly broken: `_last_puppet=None` raises and the player sees `"The Character does not exist."`; `_last_puppet=<character on shardN>` puppets the character on the *router* (chokepoint-exempt for reads) and the next save raises `ShardIsolationError`.

The library needs to make a routing decision (redirect-to-shard vs render OOC menu) at exactly the position where Evennia's if/else lives. Three alternatives were considered and rejected:

- **Mutate `_AUTO_PUPPET_ON_LOGIN`** to force the else-branch: it's a module-level cached constant. Mutating it is process-global, races on concurrent logins, and the redirect would still need to happen *after* the else-branch rendered the OOC menu (visible flash before the WS-level redirect kicks in).
- **Override `puppet_object` instead**: wrong granularity — by the time `puppet_object` is called the prelude has run and the if-branch has been taken. Doesn't handle the `_last_puppet=None` case at all (because `puppet_object` is never called when it raises early). Also broadens blast radius (`puppet_object` is also called from `CmdIC` etc.).
- **Use `SIGNAL_ACCOUNT_POST_LOGIN`**: fires *after* `at_post_login` completes — by which point the puppet has already happened or the OOC menu has rendered. Three state transitions instead of one.

Reproducing the body and swapping the if-branch is the only clean approach. The else-branch (OOC character-select menu via `at_look(target=characters)`) is reproduced for the fallback path so a broken `_last_puppet` lands the player in the OOC menu rather than failing login.

**Risk on Evennia upgrade:**

- New steps added to the prelude (protocol-flag load, `logged_in` OOB, connect-channel msg). Our reproduction would miss them.
- Changes to the if-condition (e.g. new conditions added beyond `AUTO_PUPPET_ON_LOGIN`).
- Changes to the else-branch's OOC-menu rendering — currently `self.msg(self.at_look(target=self.characters, session=session), session=session)`. We reproduce that line.
- Signature or call-order changes around `at_post_login` in `sessionhandler.login()`.

How to check: diff upstream `DefaultAccount.at_post_login` against the snapshot in `shard_aware_at_post_login`. The override carries a comment citing the Evennia version it was based on. The shard wrapper is thin (flush + refresh + delegate), so only the router replacement needs a line-by-line diff.

**Risk in consumer override:**

A consumer that subclasses `DefaultAccount` and overrides `at_post_login` without calling `super().at_post_login(...)` will bypass our patch — the consumer's body runs instead. On the router, auto-puppet redirect stops working. On shards, the cache-busting preamble is skipped and cross-shard moves back to a previously-visited shard will trip the `pre_save` chokepoint. Recommended pattern: any consumer override of `at_post_login` must call `super().at_post_login(session=session, **kwargs)` (or accept that these behaviours will not run on their accounts).

A consumer that *doesn't* override `at_post_login` is safe by Python MRO — `account.at_post_login(...)` resolves to our patched `DefaultAccount.at_post_login` automatically.

**Asymmetry with `Account.create_character` (deliberate).** The chargen wrapper (below) patches the consumer's configured `BASE_ACCOUNT_TYPECLASS`, so consumer overrides compose; this wrapper patches `DefaultAccount` directly, so consumer overrides shadow ours via MRO unless they call `super()`. The asymmetry is intentional. Chargen is naturally wrap-shaped (call vanilla, read the resulting row, stamp); `at_post_login` is replacement-shaped (the auto-puppet branch is exactly what we replace, vanilla's body would do the wrong thing if called as the inner step of a wrap). Switching to a `BASE_ACCOUNT_TYPECLASS` patch here would silently *clobber* a consumer's override instead of being silently shadowed by it — a different failure mode, not an obviously better one. The current pattern leaves consumer overrides reachable and documents the recovery (call `super()`). A partial improvement (delegate to `original` on the `AUTO_PUPPET_ON_LOGIN=False` path so consumer overrides compose for that branch, while keeping the inline router redirect logic for `AUTO_PUPPET=True`) is captured as future work in [open-questions.md](open-questions.md).

**Install-time detection of consumer overrides.** Because the silent-shadow failure mode is hard to discover at runtime (the library's behaviour just doesn't run, no error fires), `AppConfig.ready()` calls `warn_if_at_post_login_overridden(AccountCls, role)` after resolving `BASE_ACCOUNT_TYPECLASS`. The helper walks the MRO from the consumer's account class up to (but not including) `DefaultAccount`; if any class along the way has `at_post_login` in its `__dict__`, a warning is emitted at startup naming the offending class and reminding the consumer to call `super()`. The detection is structural (`__dict__` membership), so a well-behaved override that *does* call `super()` will also trigger the warning — false-positive cost is one log line at startup, which is much cheaper than a silent shadow.

## `Account.create_character()` wrapper

**What we patch / extend:** `evennia/accounts/accounts.py` → `Account.create_character` (the consumer-configured account class via `settings.BASE_ACCOUNT_TYPECLASS`, not `DefaultAccount` directly). Library code: `evennia_shards/chargen.py` → `make_shard_aware_create_character(original)`. Installed only on the router. Based on Evennia 6.0.0.

**Why:**

`Account.create_character` is the converging seam for all chargen paths — `CmdCharCreate`, `AUTO_CREATE_CHARACTER_WITH_ACCOUNT`, and the guest path all funnel through it. It runs on the router (chargen is an OOC operation; the player's session lives on the router while OOC). Without intervention, the new row is auto-stamped by `pre_save` with `current = get_shard_id() = "router"`, which is not a member of `SHARD_URLS` — so `ShardAwareCmdIC` and the `at_post_login` auto-puppet path cannot redirect the player to any shard.

The wrapper calls vanilla unmodified, then reads the new character's `db_location_id`'s `shard_id` via `.values_list` and overwrites the router auto-stamp. The character's shard is by definition the shard that owns its location row — there is no separate policy decision. The two `save()`s (vanilla's plus the wrapper's `update_fields=["shard_id"]`) are both router-side, exempt from the foreign-shard refusal in `pre_save`, so no bypass is needed.

`DEFAULT_HOME` is not touched at chargen time — vanilla `create_character` does not set `db_home`, and any later cross-shard home transfer is a runtime move handled by `cross_shard_character_move`.

**Risk on Evennia upgrade:**

- Changes to `Account.create_character`'s return contract (currently `(character, errs)`). Wrapper assumes a tuple and a falsy `character` on failure.
- Changes to whether `db_location_id` is set inline by `account.create_character` before returning (currently set from `settings.START_LOCATION` if not provided). If a future Evennia version delays location assignment, the wrapper's lookup would see `None` and skip the stamp.
- A new chargen path that bypasses `Account.create_character` (e.g. a separate `Account.create_npc` or rewritten `DefaultGuest.authenticate`) would not pass through the wrapper.

How to check: diff upstream `Account.create_character` (and `DefaultGuest.authenticate`'s character-creation block) against the calling pattern the wrapper assumes.

**Risk in consumer override:**

A consumer that subclasses `DefaultAccount` and overrides `create_character` is **safe by construction**: the wrapper is installed on the configured `BASE_ACCOUNT_TYPECLASS` and reads `AccountCls.create_character` at install time, so MRO picks up either the consumer's override or the inherited `DefaultAccount` method — whichever is in effect — and wraps that. The consumer's body runs first, then the stamp.

The hazard is a consumer override that doesn't actually call `create.create_object` (or otherwise produces a character whose `db_location_id` is `None`). The wrapper logs a warning and leaves the character router-stamped; chargen succeeds but IC won't work. Recommended pattern: any consumer override should set `db_location` via the same `START_LOCATION` (or equivalent) source as vanilla.

A consumer who points `BASE_ACCOUNT_TYPECLASS` at a class that doesn't derive from `DefaultAccount` would not have `create_character` at all; the wrapper install would fail at startup. This is symmetrical to other base-class assumptions in Evennia (locks, `_playable_characters`, etc.).

## `CmdIC` / `CmdOOC` — hard overrides

**What we patch / extend:** `evennia/commands/default/account.py` → `CmdIC` (both router and shard roles) and `CmdOOC` (shard role only). Library code: `evennia_shards/commands.py` → `ShardAwareCmdIC`, `ShardAwareCmdOOC`. Installed via module-attribute swap in `AppConfig.ready()` (`_account_module.CmdIC = ShardAwareCmdIC`). Based on Evennia 6.0.0.

**Why:**

In sharded mode, `CmdIC` and `CmdOOC` *are* the cross-shard redirect mechanism — that's their entire job. `ShardAwareCmdIC.func` does not call `super().func()`; it implements the IC flow from scratch (resolve character → create ticket → emit `shard_redirect` OOB → close session). Vanilla `CmdIC.func` would puppet the character locally on the router, which is structurally wrong (characters live on shards, not the router) and would trip the chokepoints anyway. There is no compose-with-vanilla story: sharded IC isn't "vanilla IC plus some redirect logic," it's a different operation entirely. The same reasoning applies to `CmdOOC` on shards — going OOC from a shard means redirecting back to the router, not unpuppeting locally.

**Library territory, not a consumer extension point.**

These two commands are owned by the library in non-monolith roles. Subclassing or replacing them is **not** a supported integration pattern:

- `class MyCmdIC(CmdIC): ...` in a sharded deployment is a category error — the consumer is extending vanilla IC semantics, but the runtime IC semantics are the library's. Whether the resulting subclass picks up `ShardAwareCmdIC` or vanilla `CmdIC` as its base depends on Python import order (whether the consumer's module was imported before or after `AppConfig.ready()` ran our patch). Either way the resulting code is wrong: with vanilla as base, the cross-shard redirect never runs; with `ShardAwareCmdIC` as base, the consumer's customisation is layered on top of a flow whose contract they didn't design against.
- Adding `CmdIC()` directly to a custom cmdset (rather than letting `AccountCmdSet` resolve it via the patched module attribute) has the same import-order failure: the local binding may have snapshotted vanilla.

The recommended posture is **don't subclass or replace IC/OOC in sharded deployments.** If a consumer game genuinely needs IC/OOC behaviour the library doesn't provide (audit logging on IC, custom error messaging on OOC, etc.), the right path is to discuss it as a library feature or contrib pattern — not to layer their own semantics on top of an infrastructure command. The library deliberately does not document a "subclass `ShardAwareCmdIC` like this" pattern, because doing so would imply support for use cases that haven't been thought through.

**Risk on Evennia upgrade:**

- Changes to `CmdIC.func` / `CmdOOC.func` body — the router redirect and OOC-return logic in our overrides reproduces the parts of vanilla we explicitly do *not* want (e.g. character resolution from `account.characters`), so a diff against upstream is needed when bumping Evennia.
- Changes to where `AccountCmdSet` looks up `CmdIC` / `CmdOOC` — currently a module-attribute reference (`account_module.CmdIC`), which is what makes the swap work. If Evennia ever changes `AccountCmdSet` to import `CmdIC` directly into its own module at definition time, our module-attribute swap stops being seen and we'd need to switch to a different patch shape.

How to check: diff upstream `CmdIC.func` and `CmdOOC.func` bodies against what `ShardAwareCmdIC._resolve_character` reproduces; verify `AccountCmdSet` still references commands via module attribute (`from evennia.commands.default import account; account.CmdIC` or equivalent).

**Risk in consumer override:** see "Library territory" above. If a consumer does subclass or replace these, behaviour is undefined — not because of a recoverable bug, but because the consumer is replacing infrastructure they don't own. The library does not detect this case at install time (unlike `at_post_login`); the recommendation is documentation only. Consumer subclasses can be detected by the same MRO walk we use for `at_post_login`, but the failure isn't recoverable via a documented `super()` pattern, so the warning would just say "don't do this" — easier to say it once in the docs than at every startup.

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

## Webclient HTML injection (`ShardRedirectScriptMiddleware`)

**What we patch / extend:** Django response middleware that rewrites the rendered HTML of `/webclient*` responses to inject library-side JS. Library code: `evennia_shards/middleware.py` → `ShardRedirectScriptMiddleware`. Auto-installed into `settings.MIDDLEWARE` by `AppConfig.ready()` when `get_role() != ROLE_MONOLITH`. Based on Evennia 6.0.0's webclient template (`evennia/web/templates/webclient/base.html`).

**Why:**

Two pieces of JS need to load in the webclient:

1. **An early inline `<script>`** that runs synchronously *after* the template's inline `var wsurl = ...` block but *before* `evennia.js` loads. This implements refresh routing — read `localStorage` and `PerformanceNavigationTiming.type`, override `window.wsurl` if the page-load is a refresh and a saved shard target exists. The override has to take effect before `evennia.js`'s `WebsocketConnection` reads `window.wsurl` (~500ms later inside `Evennia.init`); any later seam (e.g. `$(document).ready` in `shard_redirect.js`, which fires at end-of-body) misses the read. See [ticket-auth-flow.md](ticket-auth-flow.md#refresh-routing-via-localstorage).

2. **The `shard_redirect.js` script tag**, appended just before `</body>`, for the OOB `shard_redirect` handler (server-emitted `@ic` / `@ooc` / cross-shard redirects). This is allowed to load late because it just registers a listener for server messages.

A consumer-side template edit (`{% block extrascripts %}` or similar) was rejected as a configuration burden — the library's invariant is "drop into INSTALLED_APPS and it works." Middleware injection achieves that.

The early-injection seam is found by regex match on the `<script src="...evennia.js"...>` tag in the rendered HTML (`re.compile(rb"<script\b[^>]*\bsrc=[^>]*evennia\.js[^>]*>\s*</script>")`). The override script is inserted immediately before that tag.

The middleware also has a legacy ticket-injection path (`?ticket=` in the page URL → inline `<script>` appending `&ticket=TOKEN` to `window.csessid`), kept for the manual-paste / bookmark edge case where a webclient page is loaded directly with a ticket query parameter.

**Risk on Evennia upgrade:**

- **Webclient template restructured.** The injection is keyed on the template's `<script src=...evennia.js...>` tag. If a future Evennia version inlines `evennia.js`, bundles it via webpack / ES modules, renames the file, or removes the explicit `<script>` tag in favour of an import map, the regex won't match and the override won't be injected. Failure mode: refresh routing stops working (player goes to router on every refresh), but core auth and IC/OOC flows continue to work because they use OOB `shard_redirect`. Detection: the inline override script's `console.log` line goes silent in the browser console.
- **`var wsurl = ...` removed from template.** The override mutates `window.wsurl`; if the template stops setting it (e.g. `evennia.js` reads connection URL from a `<meta>` tag or computed at init time), the override becomes a no-op. Same failure mode as above.
- **Middleware ordering changes.** The middleware is appended to `settings.MIDDLEWARE` in `AppConfig.ready()`. If Evennia ever moves `SharedLoginMiddleware` (or another middleware) such that our injection runs before HTML is fully rendered, `process_response` could miss the response. Currently it runs after the view, so this hasn't been an issue.
- **Content-Length header semantics.** We update `Content-Length` after injection — if Evennia switches to chunked encoding by default, this becomes a no-op (which is fine), but a regression where Content-Length is required and we mismatch it would truncate the response. Detection: webclient page fails to fully load.

How to check: render `/webclient` from a router gamedir, view source, confirm the early inline override script appears immediately before the `evennia.js` script tag. Verify `shard_redirect.js` is referenced before `</body>`.

**Risk in consumer override:**

A consumer that supplies their own webclient template (e.g. via `{% extends %}` or a custom Django app providing `webclient/base.html`) is the main hazard. The middleware's regex matches "any `<script>` tag whose `src` attribute mentions `evennia.js`," which is robust against attribute order and whitespace variations but assumes the tag exists. A consumer template that:

- Inlines `evennia.js` directly with `<script>...</script>` (no `src=`) would not match. Override is skipped.
- Renames the file to a custom path (`<script src="my-evennia.js">`) would not match either.
- Bundles `evennia.js` into a single bundle with no separate tag — same.

Recommended pattern: consumer templates should keep the `<script src="...evennia.js"...>` form recognisable to the regex. If a consumer needs a fundamentally different webclient bundling strategy, the library's middleware injection is not the right seam — they'd need to inject the early override themselves at the right point in their bundle.

A consumer that strips the `<script src=...shard_redirect.js...>` injection from their pipeline (e.g. with their own response-rewriting middleware that runs after ours) breaks server-side `shard_redirect` OOBs entirely. No documented detection — the library trusts that custom middleware doesn't actively undo our work.

**Note on file scope.** This middleware is scoped to URLs containing `/webclient` — see `process_response`'s early return. It does not touch the website pages, admin, or other Django-served URLs. Evennia's webclient view path is currently `/webclient` (and variants); a future rename would require updating the path filter.
