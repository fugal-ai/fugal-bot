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

Options: `--text` (report for a person), `--no-save` (write no snapshot),
`stake --since-hours H` (compare with the newest snapshot at least H hours
old, default 20; else the oldest snapshot at least an hour old),
`stake --price-alert PCT` (default 10), `stake --stale-blocks N` (default 3
tempos), `subnet --since-hours H`, `week --top N`, `week --no-archive`.

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

## The first conversation and the daily report

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
{action: "add", job: {name: "Daily stake report",
  schedule: {kind: "cron", expr: "0 8 * * *", tz: "America/New_York"},
  sessionTarget: "isolated",
  payload: {kind: "agentTurn", lightContext: true, toolsAllow: ["exec"], timeoutSeconds: 300,
            message: "Run exactly: taoq stake <address> --text. Reply with its output unchanged, ..."}}}
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
exactly; one `taoq stake` run in the bot's shell (live finney by default) and
its report in the reply; the job in `cron_jobs` (cron `0 8 * * *`,
`America/New_York`, isolated, `agentTurn`, light context, `exec` only, the
`taoq` command) with a next run time; `BOOTSTRAP.md` gone, `USER.md` and
`MEMORY.md` written, `setup_completed_at` set; then `automations run` (forced)
queues the job, the run takes exactly 2 model calls, finishes `ok`, and its
report is delivered into the conversation that created the job
(`lastDeliveryStatus: delivered`). The second turn takes 4 model calls
(`taoq`, the job, the notes, the reply).

**Cost of one daily report: 2 model calls.** The run is an isolated turn with
`lightContext` (no workspace files in the prompt; a scheduled run never gets
`BOOTSTRAP.md`) and only the `exec` tool: call 1 asks for `taoq stake ... --text`,
call 2 writes the report from its output. A watch that finds nothing replies
`NO_REPLY`, which OpenClaw does not deliver; it still costs 2 calls per run
(4 runs a day for the 6-hourly watches). OpenClaw's trigger scripts could run
`taoq` with no model call and wake the model only when there is something to
say; not used yet.

## Real-model check (to run once, by someone with a key)

On a new bot (`SEED_WORKSPACE=1`, `OWNER_WALLET_SS58` set to a wallet with
stake), in the web chat:

1. `Hi`
   Success: the reply is exactly the greeting, with the bot's name and emoji,
   nothing before or after it: no "BOOTSTRAP", no talk about instructions or
   files, no `<message>` tags. 1 model call.
2. `Yes please, the daily stake report. My time zone is America/New_York.`
   Success, in one turn and in about 4 to 6 model calls (one `taoq` run, the
   job through `dispatch_action`, the notes and `rm`, the reply): the reply
   shows the `taoq` report (positions, totals, "First report: ...") and says
   the report is scheduled every day at 08:00 America/New_York. Then check:
   - the Automations page, or in the container
     `docker compose exec bot node -e 'const {DatabaseSync}=require("node:sqlite");console.log(new DatabaseSync("/home/node/.openclaw/state/openclaw.sqlite").prepare("select name, job_json from cron_jobs").all())'`:
     "Daily stake report", `0 8 * * *`, `America/New_York`, `isolated`,
     `agentTurn` with `lightContext`, `toolsAllow: ["exec"]` and the
     `taoq stake <address> --text` command;
   - `~/workspace/BOOTSTRAP.md` is gone; `USER.md` has the time zone;
     `MEMORY.md` has the job;
   - the model proxy's `/usage` shows how many calls the turn took.
3. On the Automations page, "Run now" on the job (or ask the bot: "Run the
   daily stake report now"). Success: the report arrives in the same chat
   within a minute; the run itself takes 2 model calls (the asking turn,
   if used, 2 more).
4. Failure signs to look for: any `pip`, `npm`, `curl` to an RPC endpoint or
   websocket code in the tool calls; more than one `taoq` run per request; the
   file left in place; a greeting that mentions its instructions.

Optional: `Watch subnet 70 for me` (expect `taoq subnet 70 --text` once and a
6-hourly "Subnet watch" job), `Watch hotkey <ss58> on subnet 70` (a "Hotkey
watch" job), `What happened on Bittensor this week?` (`taoq week --text`, then
a short brief).
