"""HTTP service that picks the task for the next Minos round.

Minos calls ``/next-draw`` to find out which of its published candidates the
chain has selected, so the selection runs here, from published code, rather
than inside the caller.

The result is reproducible by anyone: the window is public, the block hash is
on chain, and the rule is a few lines of arithmetic. Running this against the
same window and the same block produces the same answer, wherever it runs and
whenever it is run.

Nothing is stored. Every response is recomputed from the published window and
the chain, so restarting the service cannot change an answer, and two
instances running side by side agree by construction.
"""
from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException

import selector

# Defaults point at the public Minos API and a public Bittensor node, so the
# service runs with no configuration. Override BITTENSOR_RPC to read the chain
# through your own node instead of a public one.
API = os.getenv("MINOS_API", selector.DEFAULT_API)
RPC = os.getenv("BITTENSOR_RPC", selector.DEFAULT_RPC)

app = FastAPI(
    title="Minos round selector",
    description=(
        "Picks the task for the next round from a Bittensor block hash. "
        "Stateless: every answer is recomputed from the published window and "
        "the chain."
    ),
)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "api": API,
        "rpc": RPC,
        "selection_rule": selector.SELECTION_RULE,
    }


@app.get("/next-draw")
async def next_draw():
    """Which candidate the chain has selected for the next round.

    ``pending`` means the deciding block does not exist yet. That is the
    honest answer for most of a round and callers must treat it as "not yet",
    never as "choose something else" — a caller that fell back to its own pick
    on a pending response would be doing exactly what this exists to prevent.
    """
    try:
        window = selector.fetch_window(API)
        return selector.resolve(window, RPC).as_dict()
    except selector.SourceUnavailable as e:
        # 502: this service is fine, something it depends on is not. A caller
        # must treat it as "ask again", never as an answer.
        raise HTTPException(status_code=502, detail=str(e))
    except selector.SelectionError as e:
        # 422: the window was read but cannot be drawn from, which is a
        # statement about the published data rather than about reachability.
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/verify/{round_id:path}")
async def verify(round_id: str):
    """Recompute a created round's draw and report whether it matches."""
    try:
        round_data = selector.fetch_round(round_id, API)
        # Returned as-is: the individual checks are the answer, and flattening
        # them into a verdict plus a sentence would make a caller parse prose
        # to learn which part failed.
        return selector.check_round(round_data, RPC)
    except selector.SourceUnavailable as e:
        # Never reported as a failed verification: nothing was checked, so
        # saying so would claim a check that did not happen.
        raise HTTPException(status_code=502, detail=str(e))
    except selector.SelectionError as e:
        raise HTTPException(status_code=422, detail=str(e))
