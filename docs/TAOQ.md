# taoq: the bot's chain tool, and the first conversation

`taoq` is a read-only command in the bot image (`/opt/fugal-bot/tools/taoq.py`,
on `PATH` as `taoq`) that answers the four starter jobs from public Bittensor
chain data. No wallet, no key, no transaction: it opens a websocket to a public
Subtensor endpoint, reads state at the latest **finalized** block and prints
one line of JSON, or with `--text` a short report for a person.

It exists because the first real-model test spent its whole daily allowance
(30 model calls, about $0.36) trying to read stake by hand (no pip, a failed
`npm install`, hand-encoded RPC) and still never read it. The house rules
(`bot/seed/AGENTS.md`) now tell the agent to run `taoq` first, never to
install packages or hand-write RPC, and to stop after two failures.

## Commands

| Command | What it reads |
|---|---|
| `taoq stake <coldkey>` | every stake position (subnet, name, hotkey, validator name, amount, price, value, what unstaking now would return), free balance, totals; change since an earlier snapshot; warnings |
| `taoq subnet <netuid>...` | price, EMA price, pool reserves, market cap, TAO emission per day and share, alpha emission, tempo, neurons / max, registration cost, immunity period, age, owner hotkey and coldkey, on-chain identity (GitHub, URL, description), GitHub API links for releases and commits, price change against stored snapshots |
| `taoq hotkey <hotkey> [--netuid N]` | each subnet where it is registered (all subnets scanned without `--netuid`): uid, active, validator permit, incentive, dividends, consensus, trust, validator trust, rank, emission per epoch and per day, stake, last weight-setting block, immunity, and a deregistration reading |
| `taoq week` | price movers (top 5 up and down), emission share changes, new subnets (last 7 days), deregistered or replaced subnets, top subnets by emission; compared with a stored snapshot about 7 days old, else with the state 7 days ago from an archive node |
| `taoq subnet-brief <netuid>` | the `subnet` facts plus the metagraph: validators with permit and how many set weights, miners earning incentive and from how many coldkeys, incentive concentration, top validators and miners, immune neurons, the next uid to be pruned |
| `taoq news [<netuid>...] [--mine <coldkey>]` | not the chain: the shared Fugal feed (releases, announcements, chain events, news headlines) for those subnets, the wallet's subnets plus the network, or (no netuids) the network; below |
| `taoq briefing <coldkey>` | the daily briefing in one run: the `stake` report, then what changed on the wallet's subnets since the last briefing, then the network's headlines; below |

Options: `--text` (report for a person), `--no-save` (write no snapshot),
`stake --since-hours H` (compare with the newest snapshot at least H hours
old, default 20; else the oldest snapshot at least an hour old),
`stake --price-alert PCT` (default 10), `stake --stale-blocks N` (default 3
tempos), `subnet --since-hours H`, `week --top N`, `week --no-archive`,
`hotkey|subnet --watch` (below; `subnet --price-alert PCT` also sets the
watch's threshold; `subnet --watch --news` adds the feed's items), `stake
--no-news` (with `--text`: no news sections), `news --since-hours H | --since
ISO`, `news --limit N` (1 to 100, default 30), `news --watch`,
`briefing --news-since-hours H`.

Examples from finney (block 9568260, trimmed):

```
$ taoq stake 5GbydRVj98hQZvR2cf4t8WtcoUVjAuJykxxuqauiJpJqwB7m --text
Stake report for 5GbydR…wB7m (block 9568288, 2026-11-23T08:45:59Z)
- SN70 Fugal: 26,699.55 α × 0.003494 = 93.2788 TAO on your own hotkey 5E4x81…TL5U
Total: 93.2788 TAO staked (at current prices; about 72.0706 TAO if unstaked now), 2.3535 TAO free, 95.6323 TAO in all.

$ taoq hotkey 5E2LP6EnZ54m3wS8s1yPvD5c3xo71kQroBw7aUVK32TKeZ5u --netuid 70 --text
- SN70 Fugal uid 1 (validator): incentive 0, dividends 0.39611, 1,166.12 α/day, weights set 334 blocks ago; deregistration: low (position 244 of 245 ...)

$ taoq week --text
Compared with block 9517872 (7.0 days earlier, archive node).
Up: SN35 logic +137.5%, SN26 Pocket +106.8%, SN40 Ralph +39.9%, ...
New: SN57 Roa (1.5 d), SN84 ansuz (3.0 d), SN109 armeni (4.5 d), SN113 gje (6.0 d)
```

## Watches: only what is new

A scheduled watch used to repeat every warning on every run (a validator that
had not set weights for 2,000 blocks was reported four times a day), and the
model turned `low` readings into "deregistration risks". `--watch` on `hotkey`
and `subnet` makes the tool itself decide what is worth saying.

Each finding has a **stable key** and a **severity** (`info`, `warning`,
`critical`). The findings of the last check are kept in
`$TAOQ_DATA_DIR/watch-hotkey-<hotkey>-<snN|all>.json` /
`watch-subnet-<netuids>.json` (next to the other snapshots; `--no-save` reads
them but writes nothing). A run compares and prints:

```
$ taoq hotkey 5E2LP6… --netuid 70 --watch          (six hours after the last check)
{"ok":true,"command":"hotkey","watch":true,"alert":true,"hotkey":"5E2LP6…","registered_on":[70],
 "block":9568291,"at":"…","first_check":false,"previous_check":{"at":"…","block":9566131,"hours":6.0},
 "new":[{"key":"deregistration:SN70","severity":"warning","text":"SN70 uid 1: deregistration risk high (position 3 of 241 …)"}],
 "escalated":[],"resolved":[{"key":"weights_stale:SN70","was":"warning","text":"…"}],
 "ongoing":["permit_lost:SN12 warning since 2026-11-22T08:00:00Z"],
 "summary":"1 new, 0 worse, 1 resolved; 1 unchanged (do not repeat those)"}

$ taoq hotkey 5E2LP6… --netuid 70 --watch          (nothing changed)
{"ok":true,"command":"hotkey","watch":true,"alert":false,…,"new":[],"escalated":[],"resolved":[],"ongoing":[…],
 "summary":"nothing new, worse or resolved since 2026-11-23T02:13:20Z; send no message"}
```

- `new`: not in the last check. `escalated`: there, but the severity went up
  (`was` names the old one). `resolved`: there before, gone now. `ongoing`:
  there before at the same severity (or a lower one), listed in one short
  string each with the time it was first seen; never to be told again, even
  though its numbers (blocks since weights) have moved.
- **`alert` is true when there is anything in `new`, `escalated` or `resolved`
  (a cleared problem is news), and on the first check** (which records the
  state and prints the full report). Otherwise it is false and the summary says
  to send no message. A watch result has no metagraph dump: it is a few hundred
  bytes when quiet.
- With `--text` the output is, when `alert` is false, exactly `NO_REPLY`: the
  token OpenClaw's scheduler does not deliver. Otherwise one line per finding
  (`NEW warning: …`, `WORSE, now critical (was warning): …`, `RESOLVED (was …): …`)
  and one `Same as before, not new: …` line. Amounts keep their exact decimals;
  an event's numbers are also in its `data` object.

What counts, for `hotkey`: `deregistration:SN<n>` (reading `next` is `critical`,
`high` is `warning`; `medium` and `low` are normal and never a finding),
`weights_stale:SN<n>` (a validator permit and no weights for over 3 tempos:
`warning`; the block count is in `data`, not in the key), `permit_lost:SN<n>`
(`warning`) and `registration_lost:SN<n>` (`critical`), both remembered until
regained. For `subnet`, all events (told when they happen, not kept):
`price_move:SN<n>` (`warning`, a move of `--price-alert` percent, default 10,
since the last check, with `price_before`, `price_after` and `price_pct` in
`data`), `subnet_replaced:SN<n>` (`critical`: the netuid has a new registration
block) and `release:SN<n>:<tag>` (`info`: a GitHub release that was not in the
last check's list, read from the public API link, not read in fixture mode; if
it cannot be read it is named in `unread` and the old list is kept). The first
check only records the baseline.

**Silent finish, no model call (checked against OpenClaw 2026.11.6's docs and
tool schema).** Possible, and the seed recipe uses it: the `automations` tool
accepts `payload: {kind: "script", script: …}` (agent-created; a `command`
payload can only be made by the operator CLI). The script runs headless in
OpenClaw's Code Mode with the agent's `exec`, makes no model call, and its
returned `notify` text is delivered to the creating conversation; a script that
returns nothing delivers nothing. The recipe's script runs the `taoq … --watch
--text` command, throws if it fails (an error in the run history, never a
silent miss) and returns `{notify: output}` unless the output is `NO_REPLY`.
So a quiet check costs 0 model calls and an alert costs 0 too (4 checks a day
used to be 8 calls). **Known wrinkle (measured, forced runs in the container
test):** on 2026.11.6 the first script run after the gateway process starts
fails once with "Plugin runtime changed" (4 fresh starts, 4 times; an in-script
retry does not help); the next run works. A watch can therefore miss one check
after each restart of the bot. A run started by the scheduler's own timer was
not observed (the first one is hours away) so it is not known whether it
behaves the same. If a gateway turns scripts off (`cron.triggers.enabled:
false`) the recipe falls back to an `agentTurn` that runs the command and
replies `NO_REPLY` or the output unchanged: 2 calls per check. The container
tests (`scripts/first-conversation.sh`) run exactly the recipe text from
`AGENTS.md`: a quiet check and a delivered change, each with no model call.

## News and the daily briefing

The chain says nothing about releases, announcements or the week's stories.
Those come from the **shared feed** (the controller's `feed/` service,
`fugalai.com/feed/v1`, its reference in the controller repository's
`docs/FEED.md`), which gathers them once for every bot. `taoq` only reads it:
`GET /feed/v1/items`, no login, no key; `TAOQ_FEED_URL` changes the address.

`taoq news` passes the feed's items on as they are, each cut to one line
(control characters removed, links only if `http(s)`): `t` (published), `sn`
(subnets; 0 is the network), `k` (`chain`, `runtime`, `release`,
`announcement`, `blog`, `video`, `news`, `code`), `src`, `title`, `text` (at
most 200 characters), `url`; plus the feed's `not_covered` and `failing`.
Without `--watch` it reads a window (`--since-hours`, default 24);
`--watch` keeps a cursor in `$TAOQ_DATA_DIR/news-<netuids>.json` (the feed's
own clock, so nothing is missed) and the items already told, and with
`--text` prints exactly `NO_REPLY` when nothing is new. `--text` is sized for
a small model: one line per item, facts and releases before news and commit
activity, **at most 2,400 characters** (the rest counted, "…and N more not
shown here"), and one line on what the feed does not read (Discord chat, X).
`--mine <coldkey>` asks the chain which subnets the wallet holds stake in and
the feed for those plus the network.

**The feed unreachable** (no answer, an HTTP error, a web page instead of
JSON): `taoq news` says so plainly (`News: the news feed (...) could not be
reached: HTTP 404. Nothing is shown rather than a guess.`) and exits **3**;
nothing is invented.

`taoq briefing <coldkey>` is the daily briefing as ONE command (JSON with
`stake`, `news`, `usd: null`; or `--text`, ready to send):

1. the `stake` report, unchanged (positions, value, change since yesterday split
   into "from more alpha" and "from price moves", the `!` lines);
2. "What changed on your subnets since <time>": the feed's items for the
   subnets the wallet holds (two requests: those subnets; the network), only
   what the last briefing did not show (`$TAOQ_DATA_DIR/news-briefing-<coldkey>.json`);
   at most 1,600 characters;
3. "Bittensor headlines": network items (runtime releases and upgrades, SDK
   versions, new, replaced or dissolved subnets, Novelty Search, news
   headlines), at most 900 characters; daily commit counts are left out here;
4. one line on what the feed does not read.

It degrades without failing: **no feed** gives the stake report and one line
(`News: the news feed could not be reached (...); no news in this report
rather than a guess.`), exit 0, and the cursor stays where it was; **no
stake** gives one line (free balance) and the network headlines; the chain
unreadable is exit 1 as for `stake`. **No US dollar figures**: the feed has no
prices endpoint (the only no-key source found publishes no terms of use), so
amounts stay in TAO and alpha from the chain.

**Daily jobs made before the briefing** run `taoq stake <address> --text`:
that command now appends sections 2 to 4 (sharing the briefing's cursor) when
the feed answers, and prints exactly what it printed before when it does not.
So existing bots get the news with no job edit, and nothing changes for them
until the feed is live. (`taoq stake` without `--text`, the JSON the agent reads
for questions, never reads the feed.)

A real briefing, produced on 2026-11-23 with live finney and the feed run
locally from the controller repository after one collection pass (no model):

```
$ taoq briefing 5GbydRVj98hQZvR2cf4t8WtcoUVjAuJykxxuqauiJpJqwB7m --text
Stake report for 5GbydR…wB7m (block 9572135, 2026-11-23T21:35:27Z)
- SN70 Fugal: 27,479.97 α × 0.003537 = 97.2062 TAO on your own hotkey 5E4x81…TL5U
Total: 97.2062 TAO staked (at current prices; about 74.5917 TAO if unstaked now), 2.3535 TAO free, 99.5597 TAO in all.
First report: changes will show from the next one.

What changed on your subnets since 22 Nov 21:35 UTC:
- SN70 release: Miner image image-v2026.11.22b (v0.7.0, protocol v4) — Approved entries image-v2026.11.22b network entry compose file finney f4be9383e31c6cc87707b2b01ace9… (github fugal-ai/fugal-subnet, 22 Nov 23:09 UTC) https://github.com/fugal-ai/fugal-subnet/releases/tag/image-v2026.11.22b
- SN70 code activity: 5 commits on main — latest: Merge pull request #204 from fugal-ai/release/v0.7.0 (github fugal-ai/fugal-subnet (commits), 23 Nov 21:27 UTC) https://github.com/fugal-ai/fugal-subnet/commits/main
Bittensor headlines:
- Network runtime: Runtime 494 — Fix v493 stable Python floor check and runtime announcements by @UnArbosFive in #3351 Release v494… (github RaoFoundation/subtensor, 23 Nov 16:32 UTC) https://github.com/RaoFoundation/subtensor/releases/tag/v494
- Network news: Bittensor Expands Subnet Limit to 160, Opening the Door for More AI Builders (news taodaily.io, 23 Nov 17:35 UTC) https://taodaily.io/bittensor-expands-subnet-limit-to-160-opening-the-door-for-more-ai-builders/
- Network news: TPN-009 Wants to Make Google’s Gemma 4 Small Enough for Home Computers (news taodaily.io, 23 Nov 16:34 UTC) https://taodaily.io/tpn-009-wants-to-make-googles-gemma-4-small-enough-for-home-computers/
- Network news: Which Bittensor Subnets Turn Customer Revenue Into Alpha Buybacks? (news taodaily.io, 23 Nov 15:06 UTC) https://taodaily.io/which-bittensor-subnets-turn-customer-revenue-into-alpha-buybacks/
Not covered: Discord chat and X (the feed does not read them).
```

"Follow subnet N" is `taoq subnet N --watch --news --text` in a script job:
the watch's own events plus the feed's new announcements, blog posts and chain
events for N (GitHub releases come from the watch's own read, daily commit
counts are left out), each told once; an unreachable feed is named only when
something else is told.

## Units (every output)

- TAO and alpha amounts are decimal numbers of **whole TAO / whole alpha**,
  exact to the rao: 1 TAO = 1,000,000,000 rao, and the same for one alpha
  unit. At most 9 decimals; JSON numbers are written from exact decimals,
  never through binary floating point.
- Prices are **TAO per 1 alpha** (`SwapRuntimeApi.current_alpha_price`).
  Netuid 0 (root) is staked in TAO at price 1.
- `value_tao` = amount x price, rounded half-up to the rao. It is the value at
  the current price; `unstake_now_tao` is what the chain's own swap simulation
  (`SwapRuntimeApi.sim_swap_alpha_for_tao`) says unstaking the whole position
  now would return, after slippage and fees. For large positions in small
  pools the two differ a lot (93.28 against 72.07 TAO above).
- Shares (incentive, dividends, trust, consensus, rank) are fractions 0..1 of
  the subnet total (stored on chain as u16 / 65535).
- Emission: a subnet's TAO emission per block is `SubnetTaoInEmission` (into
  its pool) plus `SubnetExcessTao` (used to buy its alpha); over all subnets
  the two add up to the block emission (0.5 TAO; checked live). Per day = per
  block x 7,200. A neuron's emission is alpha per epoch (`Emission`, one epoch
  = tempo + 1 blocks); per day = per epoch x 7,200 / (tempo + 1).
- Percentages are numbers like 12.5 meaning 12.5 %, rounded to 2 decimals.
  Blocks are about 12 s.
- A value that could not be read is `null` and named in `unread`. Nothing is
  estimated in its place. If the chain cannot be read at all the output is
  `{"ok":false,"error":"..."}`, one line on stderr, exit status 1 (2 for a bad
  argument, such as an address with a wrong checksum).

## How the figures are decided

- **Stake** comes from `StakeInfoRuntimeApi.get_stake_info_for_coldkey` (the
  call the controller's verified stake check uses). Positions of 0 rao are left
  out. A position on the coldkey's own hotkey is marked `own_hotkey` (a miner
  or validator holding its own stake) and is not warned about.
- **Change since last time**: each run appends a snapshot (block, amounts,
  prices, permits) to `$TAOQ_DATA_DIR/stake-<coldkey>.json` (default
  `~/workspace/taoq/`, at most 200 snapshots). The change is split in two,
  adding up exactly to the change in value: `value_from_alpha_tao` = (alpha now
  - alpha then) x price now, and `value_from_price_tao` = alpha then x (price
  now - price then). Alpha growth is staking rewards only if the user did not
  add, remove or move stake in between; the output says so.
- **Warnings** (`flags`): a position that disappeared, a new position, stake
  that went down, a price move of at least `--price-alert` %, stake on a hotkey
  not registered on that subnet, stake on someone else's hotkey without a
  validator permit (earns no dividends), a validator permit lost since the last
  snapshot, a validator that has not set weights for more than 3 tempos.
- **Deregistration** follows subtensor's `get_neuron_to_prune`
  (`pallets/subtensor/src/subnets/registration.rs`, read at spec 494 while
  finney ran 493): when the subnet is full, each registration replaces the
  neuron with the lowest emission, ties going to the oldest registration, then
  the lowest uid; neurons in their immunity period and the owner's immortal
  uids (`ImmuneOwnerUidsLimit`, the subnet owner hotkey first) are skipped,
  unless no more than `MinNonImmuneUids` non-immune neurons are left. Reading:
  `next` (the next registration replaces it), `high` (within the next 3),
  `medium` (within 10), `low`, `immune` (with blocks left), `none_now` (subnet
  not full), `none` (owner's immortal uid). `next_to_prune_uid` names the
  current candidate.
- **Prices over time**: every run stores all subnets' prices in
  `subnets.json` (at most one snapshot per 4 hours, 45 days). `week` compares
  with the snapshot nearest to 7 days ago if one is at least 6 days old;
  otherwise it reads the state 7 days ago (50,400 blocks) from an archive node,
  so the first research brief already has real 7-day changes.

## Endpoints, retries, timeouts

`TAOQ_ENDPOINTS` (default `wss://entrypoint-finney.opentensor.ai:443,wss://lite.chain.opentensor.ai:443`)
are tried in order; each attempt has `TAOQ_TIMEOUT` seconds (default 40) for
the whole read, and the list is tried `TAOQ_ROUNDS` times (default 2) with a
2 s pause. `TAOQ_ARCHIVE_ENDPOINTS` (default
`wss://archive.chain.opentensor.ai:443`) is used by `week` only. All reads of
one run are made at one finalized block. A typical run takes 3 to 6 s; `hotkey`
without `--netuid` for a validator on 121 subnets took 9 s, `week` with the
archive read 10 s.

## Image and size

Python 3.13 is already in the image (for websockify). The Dockerfile's prep
stage builds a virtual environment with `async-substrate-interface` 2.2.1 and
its four dependencies (`aiosqlite`, `cyscale`, `websockets`, `xxhash`), every
wheel pinned by sha256 in `bot/tools/requirements.txt`; pip runs from its own
hash-checked wheel and is not kept, so the agent still has no package
installer. Added: 32 MB unpacked, 8.7 MB compressed (809.0 MB against 800.3 MB
for the image without it, measured on the same host).

## Tests and evidence

- Unit tests, stdlib only, no network (also in CI):
  `python3 -I bot/tools/test_taoq.py`. They replay a recorded read of finney
  (`bot/tools/fixtures/finney.json.gz`, block 9568291, contact fields blanked)
  through the same code path as a live read and recompute the figures from the
  raw values; they also cover addresses (checksum, prefix 42), exact number
  output, the snapshot choice, the change split, the warnings and the pruning
  order. Re-record with `TAOQ_RECORD=<file> taoq ...`.
- Live check against finney, every figure read a second, independent way at
  the same block: `python3 bot/tools/livecheck.py` (needs
  `async-substrate-interface`; addresses picked from the SN70 metagraph at run
  time). Stake against the share-pool storage (`AlphaV2` x `TotalHotkeyAlpha` /
  `TotalHotkeySharesV2`), the set of positions against `StakingHotkeys` x
  `AlphaV2`, the free balance against raw `System.Account` bytes decoded by
  hand, prices against `SubnetTAO` / `SubnetAlphaIn`, subnet facts against
  plain storage items, a hotkey's uid, emission, permit, last update and
  registration block against storage, the next uid to prune recomputed from
  storage alone, the 7-day price change against the pool reserves at the old
  block on the archive node, the brief's counts against `ValidatorPermit` and
  `get_neurons_lite`. Result on 2026-11-23: 119 passed, 0 failed.
- In the container (`scripts/hosted-test.sh`, `scripts/smoke.sh`, via
  `scripts/first-conversation.sh`): the scripted model carries the first
  conversation and the bot's own shell runs `taoq` for real (live by default;
  `HOSTED_TEST_TAOQ=fixture` / `SMOKE_TAOQ=fixture` replays the recorded read,
  no network). See below.

## The first conversation and the daily briefing

`bot/seed/BOOTSTRAP.md` is written so that the first reply is the approved
greeting and nothing else (no tool call either), without tags, and says that
the file is private; it is deleted only in the turn that sets up what the user
chose. For each starter job the "Schedules" section of `bot/seed/AGENTS.md`
gives the exact `taoq` command and the `automations` job to create; it lives
there, not in BOOTSTRAP.md, so that a watch asked for days later is made the
same way. (A real-model run on 2026-11-23 with the recipes in BOOTSTRAP.md:
the agent deleted the file while greeting, then made every later job up
itself: a non-isolated daily job that answered NO_REPLY, trigger scripts
every 15 minutes for the watches.)

Schedules use OpenClaw's own scheduler (the agent tool `automations`, alias
`cron`; jobs are stored in the `cron_jobs` table of
`~/.openclaw/state/openclaw.sqlite`, shown on the Automations page):

```
{action: "add", job: {name: "Daily briefing",
  schedule: {kind: "cron", expr: "0 8 * * *", tz: "America/New_York"},
  sessionTarget: "isolated",
  payload: {kind: "agentTurn", lightContext: true, toolsAllow: ["exec"], timeoutSeconds: 300,
            message: "Run exactly: taoq briefing <address> --text. Reply with its output unchanged, ..."}}}
```

No `delivery` is given: OpenClaw stores `delivery: {mode: "announce"}` with the
creating conversation as the job's owner and sends each result there (the
creating conversation is the fallback when there is no external route). In
this OpenClaw version the agent sees `automations` only in Tool Search's
directory and calls it through `dispatch_action` (`id: "automations"`); the
file says so. Deleting `BOOTSTRAP.md` is how this version expects the first
run to end: OpenClaw then records `setup_completed_at` in its
`workspace_setup_state` table and stops putting "Bootstrap Pending" in front of
the model.

What the container test (`scripts/first-conversation.sh`) checks, with the
scripted model doing what the file says: the greeting is the first reply,
exactly; the briefing offer accepted; one `taoq briefing` run in the bot's
shell (live finney by default; the news from the recorded feed answers in
`bot/tools/fixtures/feed-*.json`) and the whole briefing in the reply (stake,
"What changed on your subnets", headlines); the job in `cron_jobs` (cron `0 8 * * *`,
`America/New_York`, isolated, `agentTurn`, light context, `exec` only, the
`taoq briefing` command and "numbers exactly as printed") with a next run time; `BOOTSTRAP.md` gone, `USER.md` and
`MEMORY.md` written, `setup_completed_at` set; then `automations run` (forced)
queues the job, the run takes exactly 2 model calls, finishes `ok`, and its
report is delivered into the conversation that created the job
(`lastDeliveryStatus: delivered`). The second turn takes 4 model calls
(`taoq`, the job, the notes, the reply).

**Cost of one daily briefing: 2 model calls** (one to run the tool, at most one
to phrase it; the job's message says to reply with the output unchanged plus
at most one sentence, with its numbers exactly as printed). The run is an isolated turn with
`lightContext` (no workspace files in the prompt; a scheduled run never gets
`BOOTSTRAP.md`) and only the `exec` tool: call 1 asks for `taoq briefing ... --text`,
call 2 writes the briefing from its output. (A script job would cost 0 calls
but, on OpenClaw 2026.11.6, its first run after each gateway start fails once:
acceptable for a 6-hourly watch, not for the one daily message.) The 6-hourly watches are
script jobs (see "Watches" above) and make no model call at all, quiet or not.

## Real-model check (to run once, by someone with a key)

On a new bot (`SEED_WORKSPACE=1`, `OWNER_WALLET_SS58` set to a wallet with
stake), in the web chat:

1. `Hi`
   Success: the reply is exactly the greeting, with the bot's name and emoji,
   nothing before or after it: no "BOOTSTRAP", no talk about instructions or
   files, no `<message>` tags. 1 model call.
2. `Yes please, the daily briefing. My time zone is America/New_York.`
   Success, in one turn and in about 4 to 6 model calls (one `taoq` run, the
   job through `dispatch_action`, the notes and `rm`, the reply): the reply
   shows the `taoq briefing` output (positions, totals, "First report: ...",
   what changed on the wallet's subnets, headlines, or the one-line note that
   the feed could not be reached) with every number as printed, and says the
   briefing is scheduled every day at 08:00 America/New_York. Then check:
   - the Automations page, or in the container
     `docker compose exec bot node -e 'const {DatabaseSync}=require("node:sqlite");console.log(new DatabaseSync("/home/node/.openclaw/state/openclaw.sqlite").prepare("select name, job_json from cron_jobs").all())'`:
     "Daily briefing", `0 8 * * *`, `America/New_York`, `isolated`,
     `agentTurn` with `lightContext`, `toolsAllow: ["exec"]` and the
     `taoq briefing <address> --text` command;
   - `~/workspace/BOOTSTRAP.md` is gone; `USER.md` has the time zone;
     `MEMORY.md` has the job;
   - the model proxy's `/usage` shows how many calls the turn took.
3. On the Automations page, "Run now" on the job (or ask the bot: "Run the
   daily briefing now"). Success: the report arrives in the same chat
   within a minute; the run itself takes 2 model calls (the asking turn,
   if used, 2 more).
4. Failure signs to look for: any `pip`, `npm`, `curl` to an RPC endpoint or
   websocket code in the tool calls; more than one `taoq` run per request; the
   file left in place; a greeting that mentions its instructions.

Optional: `Follow subnet 70 for me` (expect `taoq subnet 70 --watch --news --text` once
and a 6-hourly "Subnet watch" script job), `Watch hotkey <ss58> on subnet 70`
(a "Hotkey watch" script job; later checks send nothing unless something
changed), `What happened on Bittensor this week?` (`taoq week --text; taoq news
--since-hours 168 --text` in one run, then a short summary with links),
`What's new on subnet 64?` (`taoq news 64 --since-hours 168 --text`).
