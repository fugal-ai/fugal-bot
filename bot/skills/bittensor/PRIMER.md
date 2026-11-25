# Bittensor primer (for explaining and interpreting)

2026-11-23. Read one section at a time: each `##` heading stands alone
(`sed -n '/^## Staking/,/^## /p' PRIMER.md`).

**Basis.** Checked against the chain's own documentation and source at
subtensor tag `v493` (the runtime finney runs: spec 493, read from chain at
block 9,570,532 on 2026-11-23), plus live chain reads that day. The docs live
at https://www.bittensor.com/docs (one page as markdown:
`https://www.bittensor.com/llms.mdx/docs/<slug>/content.md`). Where the docs
and the live chain disagree, the live value is given and the difference
noted. Section "Not verified" lists what was not checked. Numbers marked
"live" change; re-read them with `taoq` before quoting.

Rules for using this: say what a figure means, never what to do with money.
No price predictions, no "good/bad investment". Quote `taoq` figures as they
are; never fill a missing figure from this primer.

---

## The network in one paragraph

Bittensor is a blockchain (the **subtensor** chain, one block about every 12
seconds) that pays for useful work done off-chain in **subnets**. Each subnet
(identified by a number, its **netuid**) is a competition defined by its
owner's code: **miners** produce something (inference, storage, predictions,
training...), **validators** score the miners by setting **weights**, and
the chain turns those scores into payments every **epoch** using **Yuma
Consensus**. There are 129 subnets today including root (live: netuid 0 is
**root**, which has no miners and no token of its own). The chain's token is
**TAO**; every subnet also has its own token, **alpha**.

## TAO, alpha and the pools

- 1 TAO = 1,000,000,000 rao. Each subnet's alpha has the same unit size.
  Alpha of subnet 3 is a different asset from alpha of subnet 64; never add
  alpha amounts across subnets.
- Each subnet has a **pool** holding TAO and its alpha. The **price** of
  alpha is TAO per 1 alpha, the pool's reserve ratio (weighted: the pool is
  Balancer-style; weights start at 0.5/0.5, where price = TAO reserve /
  alpha reserve; protocol injections nudge the weights instead of the
  price).
- **Staking into a subnet is a swap**: your TAO goes into the pool and you
  get alpha out. Unstaking is the reverse swap. There is no unbonding period:
  the TAO is free at once. (Root is the exception, below.)
- **Price impact / slippage**: a swap moves the price against you as it
  fills; the bigger the trade relative to the pool, the worse the average
  price. So a position's **value at the current price** (alpha x price) is
  not what you would get by selling it. The chain can simulate the sale
  exactly; `taoq` reports it as `unstake_now_tao`. In a small pool the gap is
  large (example from taoq's docs: 93.28 TAO at price, 72.07 TAO if sold now).
- **Swap fee**: about 0.05% (FeeRate 33/65,535, per subnet), taken from what
  you put in. Moving stake between validators on the same subnet is not a
  swap (no fee, no price impact); moving between subnets is two swaps but
  pays the fee once.
- **EMA price** ("moving price"): a slow moving average of the price. It is
  what decides the subnet's share of TAO emission (next section). New
  subnets' EMA starts near zero and adapts very slowly on purpose.
- **Market cap** in `taoq` = all alpha in existence (in the pool and staked)
  x price. Like any market cap it is not money that could be withdrawn.
- Minimum stake operation: 0.002 TAO (default). An account below 500 rao is
  wiped (existential deposit).

## Emissions: where new tokens go

**TAO.** The chain mints TAO every block. It started at 1 TAO per block and
halves each time total issuance crosses the halfway point of what is left
of 21 million. The first halving was in December 2025: **0.5 TAO per
block, about 3,600 TAO a day** (live). Recycled TAO (registration fees,
transaction fees) is subtracted from issuance and can be minted again, which
pushes halvings later.

**How TAO is split across subnets** (source `subnet_emissions.rs` @v493):
1. Each subnet's share starts as its **EMA price / sum of all EMA prices**.
2. It is cut in proportion to `MinerBurned` (the part of last epoch's miner
   pay that the owner sent to its own hotkeys, i.e. burned instead of paid
   to miners) and renormalised.
3. An **emission gate** shrinks small shares: the midpoint is the share of
   the 32nd-largest subnet (live: rank 32, exponent 3, recomputed every 360
   blocks). A subnet at the midpoint keeps half its share; well above keeps
   almost all; well below keeps little.
4. Root can switch a subnet's emission off; its share then goes to the others.

The subnet's TAO goes into its own pool (`tao_in`, with matching alpha so the
price does not move) or, as a subnet matures, is used by the chain to **buy
the subnet's alpha** in its pool (chain buybacks). `taoq subnet` shows these
as `tao_into_pool_per_day` and `tao_for_alpha_buys_per_day`, and their sum as
the subnet's TAO emission and share of the block emission.

Note: older articles describe a "flow-based" or "TAO flow" rule (shares by
net staking inflow). At v493 the flow is still recorded but shares are
computed from the EMA price, as above.

**Alpha.** Each subnet also mints its own alpha: up to 1 alpha per block
(halving on the subnet's own schedule, cap 21 million) for participants
(`alpha_out_emission`), plus alpha injected into the pool beside the TAO.

## The per-epoch split: owner, validators, miners, root

Every **tempo** blocks (default 360, about 72 minutes; owners may set 360 to
50,400) the subnet's accumulated alpha is paid out at once. Payment lands on
whoever holds each UID at that moment; a neuron removed mid-epoch gets
nothing for the partial epoch.

The split is a five-slot vector `[burn, recycle, owner, validators, miners]`
set by the owner since runtime 480 ("Etao"). Default, and still the most
common: **18% owner, 41% validators (and their stakers), 41% miners**.
Rules: owner 0 to 33%; validators at most 50% and above root's share; miners
at least 33%; burn + recycle at most 33%; change at most once a week. A subnet
may also send up to 10% per target to another subnet's **Gamma** (below).
Read a subnet's actual split from chain; do not assume 18/41/41.

**Root's share comes off the top of the validator slot.** Root (TAO) stakers
are paid `0.41 x x / (x + A)` of each subnet's emission, where
`x = root TAO x TaoWeight` and `A` = the subnet's alpha issuance. Young
subnets (little alpha issued) pay root a large part: subnet 70 at 28 days old
paid root 33.8% of all its emission, leaving 7.2% for alpha validators
(chain read 2026-11-22). As alpha issuance grows, root's part shrinks. Root's
alpha is only paid while the sum of non-root EMA prices exceeds 1.0;
otherwise it is recycled.

**TaoWeight** = 0.18 (live): when the chain weighs stake, 1 TAO staked on
root counts as 0.18 of one alpha on the subnet. Dividends inside a validator
are split between its TAO (root) stakers and alpha stakers by that same
weighting.

If an epoch has no miner incentive at all, the miners' half goes to
validators.

## Validators, miners, weights, incentive, dividends

- A **neuron** is a hotkey registered on a subnet; it holds one **UID**
  (slot). Most subnets have 256 slots (owner can trim down to 64).
- **Miners** do the work. **Incentive** is a miner's share (0 to 1) of the
  miners' emission, decided by Yuma Consensus from validators' weights.
- **Validators** score miners by setting **weights** (a list of scores over
  UIDs). Only neurons with a **validator permit** count: each epoch the top
  K neurons by **stake weight** get one (K = `max_allowed_validators`,
  default 128; subnet 70 uses 64). Stake weight = alpha staked on the hotkey
  on that subnet + root TAO stake x 0.18. A chain-wide minimum stake weight
  also applies (live: 1,000). The subnet owner's own UID always has a permit.
- **Yuma Consensus** (each epoch): for each miner, the stake-weighted median
  of validators' weights is the **consensus**; weights above it are clipped
  down to it. Miners' **incentive** follows the clipped, stake-weighted
  scores. **Validator trust** = how much of a validator's weight survived
  clipping. Validators earn **dividends** through **bonds**, a moving average
  of the weights they set: a validator that backed good miners early earns
  more when the rest agree.
- **Trust and rank are no longer computed** (docs @v493; the storage items
  are gone from the source). Treat any `trust` or `rank` field as empty;
  do not interpret it.
- **Commit-reveal** (on by default): weights are committed hashed and
  revealed a tempo later, so nobody can copy them in time.
- **Activity cutoff**: a validator that has not set weights for about 5,000
  blocks (at tempo 360) is left out of consensus until it sets weights again.
  `taoq` warns earlier (no weights for more than 3 tempos).
- Losing a permit does not deregister a validator but stops its dividends
  and **deletes its bonds** (it starts again from zero when it regains one).
- Variants: some subnets run **Yuma3** and **liquid alpha** (different bond
  maths); 7 subnets run **Null consensus** (only the largest-stake
  validator's weights count; up to 2,500 UIDs).

## Multiple mechanisms per subnet

A subnet can run several scoring **mechanisms** (formerly "subsubnets"),
each with its own weights, consensus and bonds, splitting the subnet's
emission by an owner-set ratio. Registration and UIDs are shared: one UID,
one fee, emission summed over mechanisms. Under Yuma, max UIDs x mechanism
count must be at most 256. The docs say "currently caps subnets at 2"; the
live chain allows **8** (`MaxMechanismCount`, read 2026-11-23). Examples:
SN44 and SN87 pay mechanism 1 only; SN120 splits 90/10.

## Staking, validators' take, and what a staker earns

- Stakers (nominators) stake to a validator's hotkey on a subnet. They earn
  a share of that validator's **dividends** in alpha, after the validator's
  **take** (default 18%; may be lowered at any time, raised at most once per
  ~30 days).
- Staking to a hotkey **without a validator permit** earns nothing (taoq
  flags it). Staking to a hotkey not registered on that subnet also earns
  nothing.
- A staker's TAO value moves with two things: alpha earned (more alpha) and
  the alpha price. `taoq stake` splits a change into `value_from_alpha_tao`
  and `value_from_price_tao`. Alpha growth equals rewards only if the user
  did not add, remove or move stake in between.
- **Auto-stake**: rewards can be set to stake themselves to a chosen hotkey.
- **Conviction locks** (opt-in): a coldkey can lock alpha on a subnet; the
  lock is a floor on unstaking, locked alpha keeps earning, and it accrues
  "conviction" toward a hotkey. Decaying (unlocks over ~130 days) or
  perpetual. After a subnet is a year old, a hotkey whose own conviction
  exceeds 18% of eligible alpha takes over subnet ownership.

## Root (netuid 0)

- Root stake is TAO, not alpha: staking and unstaking on root is 1:1, no
  pool, no fee, no slippage.
- Root stake counts on every subnet at TaoWeight 0.18 toward its validator's
  stake weight, and root stakers are paid root's share of every subnet's
  emission (previous sections).
- Since runtime 441 ("Root Reborn") root's alpha is **not sold** on
  arrival. Each root validator runs a **basket**: a fund holding the subnet
  alpha it earned, which the validator may rebalance by trading. Root
  stakers own a share of their validator's basket counted in **beta**
  (a share count, not a coin; 1 beta's TAO value = fund value / all beta).
  Accrued yield must be **claimed** (`claim_root`) to become root TAO stake;
  claiming costs a fee (about 0.057 TAO on a full 128-holding basket).
  Different validators' beta are different funds; compare them in TAO.
- `taoq stake` shows root positions at price 1. **Unverified**: whether
  `taoq` includes unclaimed basket yield (it reads stake positions; accrued
  beta is probably not included). Say so if a user asks about root yield.

## Hotkeys, coldkeys, childkeys

- **Coldkey**: owns funds; signs transfers, staking, registration, subnet
  ownership. Should be kept offline. Addresses start with 5 (SS58 prefix 42).
- **Hotkey**: an operating identity; gets the UID when registered; signs
  weights and serves the miner's endpoint. Stake positions are
  (coldkey, hotkey, subnet).
- One hotkey can be registered on many subnets, one UID per subnet.
- **Childkeys** (current): a validator can lend a share of its stake weight
  to up to 5 child hotkeys per subnet. Changes take effect after about 24 h.
  The child's take is 0 to 18%. Dividends flow back to parents in proportion
  to the weight each lent. New root validators are childkeyed automatically
  to subnet owners and vice versa unless they opt out. This is how a new
  subnet's validator gets stake weight without its own stake.
- Wallets can now also use post-quantum key types (ML-DSA, Falcon) since
  runtime 487. Most users still use sr25519.

## Registration, immunity and deregistration (pruning)

- **Registration cost** (taoq: `registration_cost_tao`) is a floating price
  per subnet: it halves every 360 blocks (default) with no registrations and
  is multiplied by 1.26 (default) after each one, clamped between
  `min_burn` (default 0.0005 TAO) and `max_burn` (default 100 TAO). It is
  burned/recycled and never refunded. Some subnets instead lock part of it as
  **collateral** that the miner earns back from its own emission; some allow
  free proof-of-work registration.
- **Immunity period**: a new neuron cannot be pruned for this many blocks
  (default 4,096, about 13.7 h; many subnets set more; SN70 5,000). taoq:
  `immunity_period_blocks`, `immunity_blocks_left`.
- **Pruning** happens only when the subnet is full: each new registration
  replaces the non-immune neuron with the **lowest emission** (summed over
  mechanisms), ties to the oldest, then lowest UID. The owner's hotkey (up to
  `immune_owner_uids_limit`, default 1) is never pruned. Miners and
  validators are treated alike. Being pruned costs the UID only; stake stays.
- taoq's **deregistration reading**: `next` (the next registration replaces
  it), `high` (within the next 3), `medium` (within 10), `low`, `immune`,
  `none_now` (subnet not full), `none` (owner's immortal UID). Only `high`
  and `next` are warnings; `low` and `medium` are normal.
- **Subnet deregistration**: the chain allows 160 subnets (live; docs say
  128). When full, a new subnet registration replaces the non-immune subnet
  with the **lowest EMA price**. New subnets are immune for 432,000 blocks,
  about 60 days (live; the docs' "6 months" is the code default, not the
  live setting). On dissolution the pool's TAO is paid out pro-rata to all
  alpha holders as free TAO; protocol-held alpha's share returns to the chain.
- **Creating a subnet** costs a lock (live: about 484 TAO on 2026-11-23); it
  doubles after each registration and decays over about 2 weeks. The whole
  lock becomes the new pool's TAO. At most one new subnet per 10,800 blocks
  (~36 h, live).

## Gamma credits (runtime 487+, enabled network-wide 2026-11-09)

- **Gamma** is a prepaid service credit for one subnet, counted in US
  dollars. Anyone can mint it by spending TAO (the chain buys that subnet's
  alpha and burns it) or by burning alpha of that subnet they already hold.
  Rate: 1 gamma per dollar of alpha burned, using the lower of a slow
  "gamma price" and spot, and a TAO/USD price set by one oracle account that
  root chooses ("The oracle is trusted", docs).
- It sits in a **deposit** owned by a coldkey and keyed by a hotkey that acts
  as an API key; the hotkey spends it at a service. It cannot be transferred
  to another coldkey, turned back into TAO or alpha, and it is destroyed if
  the subnet dissolves. Each subnet decides what its gamma buys.
- A subnet owner can route up to 10% of its emission per target into another
  subnet's gamma (only targets root has whitelisted: SN51 Lium and SN53 Engy
  as of 2026-11-22).
- 13 subnets had gamma enabled on 2026-11-22; recorded spending was tiny
  (8.45 gamma). It is new: describe it, do not promise anything about it.

## Common risks (describe, never advise)

For stakers:
- **Price risk**: alpha positions float with the pool price; a subnet's
  alpha can fall a lot even while the staker earns more alpha.
- **Liquidity**: small pools mean selling costs a lot (`unstake_now_tao` vs
  `value_tao`). Market cap figures overstate what can be realised.
- **Validator risk**: validator loses its permit, stops setting weights,
  raises its take, or is not registered on the subnet: dividends stop or
  shrink (taoq flags these).
- **Subnet risk**: deregistration of the whole subnet (lowest EMA price, after
  immunity), owner changes the emission split, emission switched off by
  root, the subnet's product fails.
- **Concentration**: if a few miners or validators hold most of a subnet's
  incentive (taoq `subnet-brief`: incentive concentration, distinct miner
  coldkeys), the subnet may depend on few operators or be owner-controlled.
- **Key risk**: anyone with the seed phrase owns the funds; leaked proxies
  can drain value. Never ask for or accept seed phrases.
- **Front-running**: large swaps without a price limit can be sandwiched.

For miners:
- **Pruning**: earning least after immunity means losing the UID; the
  registration fee is gone.
- **Validator dependence**: only validators score miners; miners that game
  the scoring may be zeroed when the code changes.
- **Selling pressure**: miner emission is alpha; turning it into TAO moves
  the price in a small pool.
- **Owner burn**: some subnets send most miner emission to the owner's
  hotkey (burned), so "41% to miners" may be mostly burned. Check incentive
  on chain.

## Reading taoq's fields

Units: TAO and alpha in whole units (9 decimals max); prices in TAO per
alpha; shares 0..1; percentages as 12.5 = 12.5%; per day = 7,200 blocks.
`null` plus `unread` means it could not be read: never estimate it.

| Field | Means |
|---|---|
| `price` / `price_rao` | spot TAO per alpha (rao per alpha) |
| `ema_price` | the moving average that sets emission share |
| `value_tao` | amount x spot price; not sale proceeds |
| `unstake_now_tao` | chain-simulated proceeds of selling all now, after slippage and fee |
| `value_from_alpha_tao`, `value_from_price_tao` | the change since last report, split into "more alpha" and "price moved" |
| `own_hotkey` | stake on the coldkey's own hotkey (a miner or validator's own) |
| `emission.tao_per_day`, `emission.share_pct` | the subnet's TAO per day and its share of all TAO emission (`week`: `emission_share_pct`, change in `emission_share_change_pts`) |
| `emission.tao_into_pool_per_day`, `emission.tao_for_alpha_buys_per_day` | the two uses of that TAO (liquidity vs chain buybacks) |
| `emission.alpha_per_day` | alpha per day for participants (owner, validators, miners, root) |
| `pool.tao`, `pool.alpha` | the pool's reserves; a small `pool.tao` means selling moves the price a lot |
| `alpha_outstanding` | alpha held outside the pool (staked) |
| `market_cap_tao` | all alpha x price |
| `tempo` | blocks per epoch |
| `registration_cost_tao` | current price of a UID (falls when nobody registers) |
| `immunity_period_blocks`, `immunity_blocks_left` | protection from pruning |
| `validator_permit` | holds a permit; without it, no dividends |
| `incentive` | share of miner emission (0..1) |
| `dividends` | share of validator dividends (0..1) |
| `consensus` | the consensus weight the miner received |
| `validator_trust` | share of the validator's weights that survived clipping |
| `trust`, `rank` | no longer computed by the chain; ignore |
| `emission_alpha_per_epoch`, `emission_alpha_per_day` | the neuron's pay (alpha) |
| `blocks_since_weights`, `last_weights_block` | validator activity |
| `stake_weight` | alpha stake + 0.18 x root TAO stake |
| `deregistration.risk`, `next_to_prune_uid` | see Registration section |
| `leader` (`taoq uids`) | the uid with the most incentive, the owner's own hotkey left out: on a winner-take-all subnet, the champion; `tied_with`, `runner_up` |
| `role` (`taoq uids`) | `validator` = holds a permit and earns more dividends than incentive; otherwise `miner` (a permit holder that earns incentive is mining) |
| `incentive_rank` | 1 = most incentive on the subnet; `null` = earns none |
| `concentration.top1_share` ... `top10_share` | share of the subnet's incentive held by the top 1 / 3 / 10 (owner's hotkey left out); `owner_hotkey_share` is what the owner's hotkey takes (often a burn) |
| `identity` | owner-published GitHub, URL, Discord, contact, description: self-declared, not checked by the chain |
| `severity` | `info`, `warning`, `critical`: repeat as given, never upgrade |

## Not verified

- Whether `taoq stake` includes unclaimed root basket (beta) yield.
- The exact current numbers of subnets on Yuma3 / liquid alpha.
- Validator "rule of thumb" stake needed to be competitive (docs say about
  20k TAO-equivalent; community figure, not a rule).
- Root's whitelisting criteria for gamma emission; gamma's real usage beyond
  2026-11-22.
- Planned changes after v493 (v494 was proposed on GitHub on 2026-11-23 and
  not read). Re-check this primer when the runtime version changes:
  `https://github.com/RaoFoundation/subtensor/releases`.
- Organisational history (Opentensor Foundation, Rao Foundation, Latent):
  reports conflict; the code is at github.com/RaoFoundation/subtensor (the
  SDK and btcli now live inside it; the old bittensor and btcli repos were
  archived in July 2026).
