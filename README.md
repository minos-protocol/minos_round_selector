# Minos round selector

Decides which task each Minos round runs, from a Bittensor block hash.

Minos (Bittensor subnet 107) publishes a rolling window of upcoming tasks
before any of them is served. Which one a round receives is worked out **here**
— by this code, reading a block hash off the chain — and Minos fetches the
answer. The selection runs as its own service, from published code, against
inputs anyone can read.

You can also run it purely as a checker: point it at a round that already
happened and it will re-derive the selection from the public record and report
whether it matches.

## Quick start

```
pip install -r requirements.txt

python cli.py next                  # what the next round should get
python cli.py verify <round_id>     # check a round that already ran
python cli.py audit                 # check every draw on record
python cli.py watch                 # check draws as they land
```

**It works with no configuration.** The defaults already point at the public
Minos API and a public Bittensor node — no account, no key, nothing to
register, nothing to fill in.

`audit` walks the draws on record and re-derives each one:

```
$ python cli.py audit
PASS  <round-id>  position 15 of 20 at block 8983271
```

Most of the time `next` has no answer, which is the system working rather than
a fault:

```
$ python cli.py next
window        w-...  (20 candidates)
genesis       9000000  (pinned when the window was published)
draw height   9000720  = 9000000 + 2 x 360

block 9000720 has not been produced yet
```

The block that decides the next task has not been produced, so the answer does
not exist — not for this service, not for Minos, not for anyone. A caller
seeing `pending` must wait; falling back to a choice of its own would be
exactly what this exists to prevent.

### Configuration

Both settings have working defaults; see `.env.example`.

| setting | flag | default | why change it |
|---|---|---|---|
| Minos API | `--api` | `https://api.theminos.ai` | to check a different deployment |
| Bittensor node | `--rpc` | `wss://entrypoint-finney.opentensor.ai:443` | to read block hashes through your own node |

Pointing `--rpc` at your own node is the one worth doing: the block hash is the
single input that decides the outcome, so reading it directly removes any
intermediary from the result.

### Exit codes

Meant for scripting, and `2` is deliberately distinct from `1`: being unable to
check is not the same as having checked and found a problem.

| code | meaning |
|---|---|
| `0` | the round verifies, or there was nothing to verify |
| `1` | a round contradicts the rule |
| `2` | the API or the chain could not be read |

## The rule

```
draw_height       = genesis_draw_height + draws_made * blocks_per_round
index             = int(draw_block_hash, 16) % len(candidate_positions)
selected_position = sorted(candidate_positions)[index]
```

`genesis_draw_height` is pinned once, when a window is published, a full round
ahead of the chain head — so even the first draw uses a block that did not
exist when the window was announced. Every later height follows from it by
arithmetic, so the entire sequence a window will ever use is computable,
arbitrarily far ahead, from one number fixed before anything was drawn.

## Worked example

Every value is public and every step is arithmetic. Run
`python cli.py audit` to re-derive real draws the same way.

**Input 1 — the candidate list**, published before any of it was served:

```
[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19]
```

**Input 2 — a block hash**, read from Bittensor at the height the rule names:

```
block 8983271
6c34332d0c1842229581c00337e61d53c543544d0c8d9726c91ae931b04efd87
```

**The arithmetic:**

```
int(hash, 16) = 48942016897896259072354181231431395306149750449619907468556298970004213136775
         % 20 = 15            <- an index into the sorted candidate list
sorted(candidates)[15] = 15   <- the position at that index
```

Index and position are the same number here only because this window's
positions happen to run 0..19. Once entries have been drawn the list has gaps,
and the two diverge.

**Result — position 15**, whose files are the ones that round serves.

Run it a thousand times and it returns 15 a thousand times. Run it next year
against the same two inputs and it still returns 15. There is nowhere in that
sequence for an opinion to enter.

## Where each input comes from

| input | supplied by | fixed when |
|---|---|---|
| `draw_block_hash` | Bittensor | when the block is produced |
| `candidate_positions` | the published window | when the window is published |
| `blocks_per_round` | the published window | when the window is published |
| `genesis_draw_height` | the published window | when the window is published |
| `draws_made` | count of draws the window has made | derived, not set |

**Nothing in that table is written per round.** A window fixes its genesis when
it is published and every height it will ever use follows by arithmetic, so
after publication there is no per-round input at all.

The genesis names a block roughly 360 blocks — about 72 minutes — ahead of the
chain head at the moment the window goes out. Its contents are therefore
unknown to everyone when the window is published, which is what makes the
first draw as unpredictable as every later one.

Heights are deliberately not derived from the time a round starts. Rounds are
created off a wall clock, so a height tied to that clock would move with it;
tying it to a published genesis instead means the sequence is settled in
advance.

## Running it as a service

```
uvicorn service:app --port 8080
```

| endpoint | returns |
|---|---|
| `GET /next-draw` | the selection for the next round, or `pending` with the height it is waiting on |
| `GET /verify/<round_id>` | each check separately — `block_hash_matches`, `position_matches`, `expected_position` — rather than a single verdict |
| `GET /health` | the API and RPC in use, and the rule being applied |

`/verify` distinguishes `verified: false` from `checked: false`. A round created
before this mechanism existed was never drawn from a window, so there is
nothing to check — reporting that as a failure would be wrong, and reporting it
as a pass would be worse.

`verify` re-fetches the block hash from the chain using only the height the
round reports, so the value that decides the outcome is read from the chain
rather than from the response being checked.

## Checking the files themselves

A window entry is a position and the SHA-256 of every file that round will
serve. Nothing describes what the task is:

```json
{
  "position": 0,
  "status": "upcoming",
  "file_hashes": {
    "bam": "3f11ac13869a26ad...",
    "truth_vcf": "c4e5d70d235c6578...",
    "mutations_vcf": "d45241d29c67de30..."
  }
}
```

Those are hashes of file **content**. If you are served one of these files,
hash the bytes you received and look for them here — the hash covers the
bytes, not the filename, and it is how everyone receiving a round can confirm
they received the same file.

Describing the tasks is unnecessary for that and would announce what is coming
in advance, so the window stays a list of commitments rather than a schedule.

An entry already drawn stays in the list as `"status": "drawn"` and gains a
`drawn` group naming the round that took it and the block that picked it:

```json
"drawn": {
  "round_id": "<round-id>",
  "block_height": 8983271,
  "block_hash": "6c34332d0c184222..."
}
```

Without that, a candidate would vanish on the round it was selected for, and
anyone who had not already saved a copy of the list would have no way to tell
it had ever been there.

Groups are omitted rather than sent as nulls: an upcoming entry has no `drawn`,
an unsigned one no `attestation`. A field absent because it does not apply
should not look like one that failed.

## What this does and does not establish

**Does:** the task a round runs was one of a set published in advance, and the
choice among them followed a block hash produced after that set was published.
`verify` and `watch` recompute any recorded draw and report one that does not
match the rule, within a round of it happening.

**Does not: replace running it yourself.** Any deployment of this service is
only as good as the code it is running. The reason the logic is published is
that the result is reproducible — same window, same block, same answer — so a
result that does not match is visible to anyone who runs it.

**Does not: say anything about how the tasks were generated.** This settles
*which* task a round gets and fixes it in advance. How the data inside it was
produced is a separate question this repository does not address.

## Tests

```
python -m pytest test_selector.py
```

They pin the rule against concrete vectors, including a block hash taken from
the chain. Minos takes this service's answer for the round it creates, so an
edit that quietly changed what a given (candidates, block hash) produces would
make the two disagree. Tying the suite to a real block means such a change
surfaces as a contradiction with the chain rather than as a suite that still
passes.

## Design notes

`selector.py` holds the rule and the two sources it reads — the published
window and the chain — and nothing else. It
is stateless on purpose: every answer is recomputed from the published window
and the chain, so restarting changes no answer, two instances agree by
construction, and a draw resolved months ago reproduces exactly.

Chain access is a raw JSON-RPC `chain_getBlockHash` rather than a client
library, so a reader can see exactly what was asked of the chain, with no
intermediate layer between the request and the result. It also keeps the
dependency list short, which matters for something meant to run in a minute.

## Licence

MIT.
