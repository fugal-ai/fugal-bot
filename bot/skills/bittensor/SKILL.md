---
name: bittensor
description: How Bittensor works (subnets, alpha pools, emissions, staking, root, keys, registration, pruning), its terms, and where to look things up.
---
# Bittensor reference

Use for "how does ... work", "what is ...", "why did ... change" and "where can I find ..." questions. Live figures still come only from `taoq`; these files explain them and never replace a reading.

Read only what the question needs, never a whole file:

- Mechanisms: `grep -n '^## ' {baseDir}/PRIMER.md` lists the sections; read one with `sed -n '/^## Staking/,/^## /p' {baseDir}/PRIMER.md` (after `^## ` put only the heading's first word: no quotes or apostrophes in the pattern). "Reading taoq's fields" says what each taoq figure means.
- A term: `grep -i -m3 '^- \*\*<term>' {baseDir}/GLOSSARY.md`.
- Where to look (docs, repos, which source to trust): `sed -n '/^## Part 1/,/^## Part 2/p' {baseDir}/SOURCES.md`.

Answer in plain words, short, with the figure's meaning; say when something is marked "Not verified". No investment advice or price predictions.
