# Where Bittensor information lives (source map)

Checked 2026-11-23. Part 1 is for the bot ("where do I look for X?"). Part 2
is the evidence behind it, for the owner.

Trust order, highest first: **the chain** (via `taoq`) > **code and release
notes in the repo** > **official docs** > **a subnet's own channels** (self-
published, may be marketing) > **aggregators** (third-party, may lag or be
wrong) > **news and social** (opinion). A subnet identity on chain is
self-declared: the owner wrote it; the chain does not check it.

## The news feed: what it covers

`taoq news` and `taoq briefing` read the Fugal news feed (fugalai.com/feed/v1),
which gathers these once for every bot, read-only and without logging in:

- **Chain events** (finney, hourly): a subnet registered, replaced or
  dissolved; identity, owner, emission split or mechanism changes; gamma on;
  emission off; runtime upgrades. These are facts.
- **Releases**: subtensor runtime releases; the `bittensor` and
  `bittensor-cli` versions on PyPI; each subnet's GitHub releases and a daily
  count of its commits (repository from its chain identity).
- **Subnet blogs** that a subnet's website advertises as RSS or Atom.
- **Talks and news headlines**: Novelty Search videos (titles); taodaily.io,
  taonews.ai and taostats.io blog (title, date and link only).

Not covered: **Discord chat** of any server (later, at most the published
posts of announcement channels the owner follows), **X**, **Telegram**,
tao.media, anything behind a login, and a subnet with no GitHub, site or
feed in its identity. So "did anything happen on subnet N?" is answered
"nothing reached the feed" when it lists nothing, never "nothing happened",
and the bot says it cannot see Discord chat or X. Items are their sources'
words (chain items are facts); give the source and the link. If the feed
cannot be reached, `taoq` says so: say that, never guess.

## Part 1: for each question, where to look first

| Question | First | Then | Notes |
|---|---|---|---|
| Stake, positions, value, what unstaking returns | `taoq stake <coldkey>` | nothing else | Never estimate. |
| A subnet's price, pool, emission, owner, registration cost, links | `taoq subnet <netuid>` | `taoq subnet-brief <netuid>` for validators and miners | `identity` gives GitHub, website, Discord, contact. |
| A hotkey's standing, permit, pruning risk | `taoq hotkey <hotkey> [--netuid N]` | | |
| What moved this week across Bittensor | `taoq week` | `taoq news --since-hours 168` | |
| What is new for subnets X, Y (releases, announcements, chain events) | `taoq news X Y --since-hours 168` (the Fugal news feed) | GitHub releases of the subnet's repo (the link `taoq subnet` prints), the subnet's own channels | What the feed covers: "The news feed" below. |
| A subnet's code releases | `taoq subnet <n>` prints the GitHub API releases link; or `https://github.com/<owner>/<repo>/releases.atom` | `.../commits.atom` (noisy) | Unauthenticated GitHub API: 60 requests/hour per IP. The Atom feeds are lighter. |
| How a mechanism works (emissions, staking, pruning, Gamma...) | PRIMER.md (this pack) | official docs as markdown: `https://www.bittensor.com/llms.mdx/docs/<slug>/content.md` (index: `https://www.bittensor.com/llms.txt`) | Docs pages are long: fetch one page, read the section you need. |
| Exact chain rule, a parameter, an error message | `https://www.bittensor.com/catalog/reads.json`, `intents.json`, `errors.json` | source search: `https://www.bittensor.com/code/search.json?q=<literal>` | Primary. |
| Network upgrades (runtime releases) | `https://github.com/RaoFoundation/subtensor/releases.atom` | `https://www.bittensor.com/releases` (HTML, no feed) | One release per runtime version; "(proposed)" means not live yet. The same text is posted to Discord. |
| SDK / btcli versions | PyPI JSON: `https://pypi.org/pypi/bittensor/json`, `https://pypi.org/pypi/bittensor-cli/json` | | The SDK now lives in the subtensor repo (`sdk/python`). The old bittensor and btcli repos were archived in July 2026. |
| Foundation talk, the regular community call | Novelty Search on YouTube, feed: `https://www.youtube.com/feeds/videos.xml?channel_id=UCq-UgxLUK1mYJBxrmOfP7aA` | `https://taodaily.io/category/podcasts/novelty-search/feed/` | Titles and links; no transcripts in the feed. |
| Ecosystem news | `https://taodaily.io/feed/`, `https://www.taonews.ai/rss.xml`, `https://taostats.io/blog/rss.xml` | | Opinion and AI-written stories (taonews says so). Give titles and links; do not republish articles. **Do not use tao.media content**: its terms forbid using it to "ground" an AI model. |
| Subnet market data beyond taoq (history, volumes) | `https://api.taomarketcap.com/public/v1/...` (no key; 10 requests/min per IP) | taostats needs an API key (free tier 5 requests/min) | Prefer `taoq`; aggregators lag and can disagree with the chain. |
| Who to contact about a subnet | `taoq subnet <n>` → `identity.contact`, `identity.discord`, `identity.url` | the subnet's GitHub README | Ask the user before sending anything to anyone. |
| X (Twitter), Discord chat, Telegram chats | Not readable by the bot. | The news feed may carry Discord announcement posts later (only published announcements, never chat). | Do not log in, scrape or automate a user account. See Part 2.6. |

## Part 2: evidence

### 2.1 Official channels

| What | Where | Evidence |
|---|---|---|
| Main site | https://www.bittensor.com (About, Whitepaper, Docs, Wallet, Explore) | fetched; links only to Discord, X and GitHub |
| Docs | https://www.bittensor.com/docs; `docs.learnbittensor.org` 301-redirects there | curl `-I` |
| Docs for machines | `/llms.txt` (20 KB index), `/llms-full.txt` (2.8 MB, grep it, do not load it), `/llms.mdx/docs/<slug>/content.md`, `/catalog/{intents,reads,errors}.json`, `/code/search.json?q=`, `/sitemap.xml` | curl, 200 |
| Upgrade notes | https://www.bittensor.com/releases (HTML only; no RSS/Atom found) | curl: `/rss.xml`, `/feed.xml`, `/releases.xml` all 404 |
| Chain + SDK + btcli code | https://github.com/RaoFoundation/subtensor (homepage bittensor.com; releases `v<spec>`; `v494 (proposed)` on 2026-11-23, finney runs 493) | GitHub API |
| Old repos | `github.com/opentensor/{subtensor,bittensor,btcli}` 301 to RaoFoundation; `RaoFoundation/bittensor` and `btcli` archived 2026-07 | curl, GitHub API |
| Governance proposals | https://github.com/opentensor/bits (Bittensor Improvement Tenets; last push 2026-04-14) | GitHub API |
| Runtime upgrade process | CI proposes; 2-of-3 "triumvirate" multisig approves; a watcher publishes the GitHub release and (since v493) a Discord announcement | `docs/internals/release-process.mdx`, `.github/scripts/announce-release.sh` @v493 |
| Community docs (Latent) | https://github.com/latent-to/developer-docs (still updated 2026-11-21; its community page lists @opentensor, Novelty Search, `discord.com/invite/bittensor`, explorers tao.app, taostats, taomarketcap) | raw GitHub |
| Official X | https://x.com/bittensor (linked from bittensor.com/about); https://x.com/opentensor ("official announcements", per the community docs page) | page fetch |
| Official Discord | Bittensor server, guild 799672011265015819, about 50,000 members; links: `discord.gg/qasY3HA9F9` (bittensor.com), vanity `discord.gg/bittensor` (community docs) | Discord invite API |
| Telegram | No official announcement channel found. `t.me/bittensor` is a group (about 5,600 members); `t.me/s/...` previews return nothing useful for any Bittensor name tried | curl |
| Podcasts/calls | Novelty Search (Opentensor YouTube; latest 2026-11-20); "Revenue Search: Inside Bittensor" `https://anchor.fm/s/1072f10cc/podcast/rss` (latest 2026-11-13); Bittensor Guru dormant since 2025-12 | feeds fetched |
| Organisation | Code is in the RaoFoundation GitHub org; bittensor.com/about names "core dev @ Rao Foundation". Reports about the Opentensor Foundation, Rao Foundation and Latent's roles in 2026 conflict: **unverified**. `raofoundation.org` is an unrelated NGO. | search, page fetch |

The official docs say nothing about which Discord channels to use; the site
only links the server invite. Per-subnet discussion lives in two places:
channels inside the main Bittensor server (22 subnets put a link to a channel
in guild 799672011265015819 in their on-chain identity, SN70 among them) and
subnets' own servers (30 identities hold an invite link, e.g. `discord.gg/chutes`,
`discord.gg/lium`).

### 2.2 The three Discord invites (Discord's public invite API, no login, 2026-11-23)

| Invite | Server | Members (online) | Notes |
|---|---|---|---|
| `discord.gg/6FegYuqNm` | **Bittensor** (guild 799672011265015819), the official server | 50,056 (5,559) | Same server as bittensor.com's invite and `discord.gg/bittensor`. Lands in channel `0・rao・🏛` (a normal text channel, type 0): the per-subnet channel for netuid 0. Verification level 4 (phone), member screening on. |
| `discord.gg/EXWKHfCym` | **dτao** (guild 1341792850932924437): "the Bittensor server for everything DTAO" | 3,939 (774) | Community server about dynamic TAO / trading; lands in `🪙｜price-τalk`. |
| `discord.gg/WvGCJn35Z` | **The Church of Rao** (guild 1120750674595024897): "Bittensor development community" | 2,300 (559) | Developer community; lands in `bittensor-family`. |

All three invites are live but **temporary: they expire 2026-12-23**. All three
servers carry Discord's `NEWS` feature (announcement channels are enabled), so
each probably has at least one followable announcement channel; which
channels, and who may see them, cannot be seen without joining (the widget is
disabled and the preview endpoint needs login on all three).

### 2.3 Per-subnet identity on chain

`SubtensorModule.SubnetIdentitiesV3` holds, per subnet: `subnet_name`,
`github_repo`, `subnet_contact`, `subnet_url`, `discord`, `description`,
`logo_url`, `additional`. The owner coldkey sets it (`set_subnet_identity`);
it is public and self-declared. `taoq subnet` already prints github, url,
description, discord and contact.

Completeness (chain read, block 9,570,515; 129 subnets, 122 with an identity):

| Field | Filled |
|---|---|
| name | 121 |
| github_repo | 114 (113 on github.com) |
| subnet_url | 113 |
| description | 114 |
| logo_url | 106 |
| subnet_contact | 88 |
| discord | 73: 30 invite links, 22 links to a channel in the main Bittensor server, 19 handles or other text; 51 empty |
| additional | 41 |

There is **no X/Telegram field**. tao.app's `TAOAppv1:` format (spec:
https://github.com/latent-to/TAOApp-specs, frozen 2026-09-10) packs a JSON
with `links.x`, `links.telegram`, `links.docs`... into `additional`
(deflate + base64url with a fixed dictionary); only SN14 and SN62 use it so
far. About 12 identities mention an X URL in free text.

Spot check of large subnets: Chutes (64), Lium (51), Ridges (62), iota (9),
Apex (1) give GitHub, site, Discord invite and contact. Targon (4),
Hippius (75), Score (44), Hone (5), blockmachine (19) have no Discord.
Affine (120), Teutonic (3), Vanta (8) put a personal handle in `discord`.
Gradients (56) literally has "None". So: GitHub and website can be discovered
automatically for about 90% of subnets; Discord for about 40%, X for about 10%.
A small hand-kept override file is needed for the rest.

### 2.4 Aggregators and data sites

| Site | Offers | Machine access | Terms |
|---|---|---|---|
| taostats.io | explorer, subnet pages, validator data, blog | API **needs a key** (401 without); plans Free 5/min and 10,000/month, $9, $49, $199; OpenAPI `https://api.taostats.io/openapi.json`; MCP server; blog RSS `https://taostats.io/blog/rss.xml` | no resale of API data, no rate-limit bypass (taostats.io/terms) |
| taomarketcap.com | subnet and price data | `https://api.taomarketcap.com/public/v1/...` with **no key**: 10/min, 600/h, 100,000/month per IP; docs `https://api.taomarketcap.com/llms-full.txt` | no terms page found |
| tao.app (Latent) | explorer, subnet "About" pages from chain (TAOAppv1), Savant assistant | site returns 403 to scripts; an `api.tao.app` → `/developer` exists, not readable | unknown |
| subnetalpha.ai | subnet directory | RSS exists but empty; mostly 2025 content | |
| taoyield.com, backprop.finance, subnetradar.com | staking yield, DEX/terminal, dashboard | HTML only | |
| taodaily.io | news, weekly ecosystem roundups, Novelty Search posts | RSS `https://taodaily.io/feed/` | boilerplate, no scraping clause |
| taonews.ai | AI-written stories from chain and taostats data | RSS `https://www.taonews.ai/rss.xml` | no reproducing the site elsewhere |
| tao.media ("Intelligence") | news | RSS `https://www.tao.media/latest/rss/`, `/tag/bittensor/rss/`, `/tag/subnets/rss/` | **bans scraping and using content to "train, fine-tune, evaluate, ground" AI**: exclude |
| tensorplex-labs/subnet-docs | subnets.json knowledge base | GitHub (last commit 2026-05) | |

### 2.5 Readable today without logging in

- **Chain**: everything `taoq` reads (public endpoints `wss://entrypoint-finney.opentensor.ai:443`, lite and archive nodes).
- **GitHub**: `releases.atom`, `commits.atom` (`/commits/<branch>.atom`), `tags.atom` per repo; REST API at 60 requests/hour per IP unauthenticated.
- **Official docs**: llms.txt, per-page markdown, JSON catalogs.
- **RSS**: taodaily, taonews, taostats blog, YouTube channel feeds, podcast feeds.
- **JSON**: taomarketcap public API, PyPI JSON.
- **Discord**: only the invite metadata endpoint (server name, counts). No messages.
- **Telegram**: nothing useful (`t.me/s/<channel>` works only for public channels; none relevant found).
- **X**: nothing within the rules without paying (2.6).

### 2.6 Discord and X: what the rules allow (checked 2026-11-23)

**Discord**
- **Announcement channel following**: only Community servers have
  announcement channels. A person who can see the channel clicks Follow and
  picks a channel in a server where they have **Manage Webhooks**; Discord
  adds a webhook there. Only messages the source admins **Publish** arrive,
  not every message; edits propagate; publishing is limited to 10 per hour
  per channel. API: `POST /channels/{id}/followers`. Webhooks: at most 15 per
  channel, 1,000 per server. No documented cap on followers.
  (support.discord.com articles 360032008192 and 360028384531;
  docs.discord.com channel resource.)
- **A bot reading them**: a bot in the receiving server needs the
  **MESSAGE_CONTENT** privileged intent to see message text (followed posts
  are not exempt). Apps with fewer than **10,000 users** switch it on in the
  Developer Portal without review (docs.discord.com/developers/events/gateway,
  read 2026-11-23).
- **Not allowed**: automating a normal user account ("self-bots": "forbidden,
  and can result in an account termination", Discord help article
  115002192352; Community Guidelines rule 14); scraping without written
  consent (Terms of Service); mining or scraping data, training AI on message
  content without permission (Developer Policy). Discord reportedly banned
  the accounts behind the Spy.pet scraper in 2024 (news reports, not checked).

**X**
- **No free reading tier.** Since early 2026 the X API is **pay-per-use**:
  credits bought up front; reading costs **$0.005 per post**, $0.010 per user,
  $0.005 per list; a post read twice in one UTC day is charged once; cap
  3 million post reads a month (Enterprise above, no public price). The old
  Basic ($200) and Pro ($5,000) plans were replaced in 2026. New payers get
  $20 of credit. (docs.x.com/x-api/getting-started/pricing.md, fetched.)
- Rate limits per 15 minutes: user timeline `GET /2/users/:id/tweets` 10,000
  per app; list timeline `GET /2/lists/:id/tweets` 900.
- **Not allowed**: "crawling or scraping the Services in any form, for any
  purpose without our prior written consent" (X Terms, effective 2026-10-09);
  liquidated damages of $15,000 per 1,000,000 posts viewed in 24 hours;
  developer guidelines: "Non-API automation (scraping, browser automation)
  results in permanent suspension". Nitter and XCancel were shut down again in
  September 2026 after legal letters. Third-party "Twitter APIs" scrape and
  carry the same problem.
- **Grey**: the embed widget's backend (`syndication.twitter.com/srv/timeline-profile/screen-name/<name>`)
  returned 21 recent @opentensor posts to a plain request on 2026-11-23. It is
  undocumented, made for browsers showing the widget; polling it from a server
  is close to what the terms forbid. Not recommended.
- **Allowed and free**: showing X's official embedded timeline to a human in a
  web page (the human's browser loads it).
