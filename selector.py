"""Choose which task a Minos round runs, from a block hash.

This is the selection itself, not a description of it. Minos publishes a
rolling window of upcoming tasks together with the genesis height its draws
run from; this module reads that, reads a Bittensor block hash from a node,
and works out which entry the round gets. The platform reads the answer from
this service.

The rule:

    draw_height       = genesis_draw_height + draws_made * blocks_per_round
    index             = int(draw_block_hash, 16) % len(candidate_positions)
    selected_position = sorted(candidate_positions)[index]

``genesis_draw_height`` is pinned once, when a window is published, a full
round ahead of the chain head — so even the first draw uses a block that did
not exist when the window was announced. Every later height follows in closed
form, which means the entire sequence a window will ever use is computable
from one public number, and nothing written per round enters the formula.

Nothing here holds state. Every answer is recomputed from the published window
and the chain, so two people running this at the same moment get the same
result, and running it later against a recorded round reproduces that round's
draw exactly.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from websocket import create_connection

DEFAULT_API = "https://api.theminos.ai"
DEFAULT_RPC = "wss://entrypoint-finney.opentensor.ai:443"

SELECTION_RULE = (
    "draw_height = genesis_draw_height + draws_made * blocks_per_round; "
    "index = int(draw_block_hash, 16) % len(candidate_positions); "
    "selected_position = sorted(candidate_positions)[index]"
)


class SelectionError(ValueError):
    """The draw cannot be made from these inputs."""


class SourceUnavailable(RuntimeError):
    """A published window, a round, or the chain could not be read.

    Separate from SelectionError because it says nothing about the draw: the
    inputs were never obtained, so there is no verdict to report either way.
    Callers surface it as a message rather than as a failed verification.
    """


# --- the rule -----------------------------------------------------------------

def normalize_block_hash(block_hash: Any) -> str:
    """Lowercase, ``0x``-free hex.

    A block is one value and its spelling is not: an RPC may return ``0xAB…``
    or ``ab…`` for the same block, and the two must not produce two different
    draws.
    """
    if not isinstance(block_hash, str):
        raise SelectionError(f"block hash must be a string, got {type(block_hash).__name__}")
    cleaned = block_hash.strip().lower()
    if cleaned.startswith("0x"):
        cleaned = cleaned[2:]
    if len(cleaned) != 64:
        raise SelectionError(f"block hash must be 64 hex chars, got {len(cleaned)}")
    try:
        int(cleaned, 16)
    except ValueError:
        raise SelectionError(f"block hash is not hex: {block_hash!r}") from None
    return cleaned


def draw_height(genesis_height: int, draws_made: int, blocks_per_round: int) -> int:
    """Closed form, so any future height is computable now.

    Deriving each height from something recorded on the previous round would
    leave an input that is only settled as the sequence runs. A genesis fixed
    before any draw means every height is determined from the outset.
    """
    # Checked as values rather than trusted as numbers: these arrive over the
    # network, and a missing field would otherwise surface as a TypeError from
    # int(None) — a traceback that says nothing about which input was absent.
    for name, value in (("genesis_draw_height", genesis_height),
                        ("draws_made", draws_made),
                        ("blocks_per_round", blocks_per_round)):
        if value is None:
            raise SelectionError(f"window is missing {name}")
        try:
            int(value)
        except (TypeError, ValueError):
            raise SelectionError(f"{name} is not a number: {value!r}") from None

    if int(draws_made) < 0:
        raise SelectionError(f"draws_made must be non-negative, got {draws_made}")
    if int(blocks_per_round) <= 0:
        raise SelectionError(f"blocks_per_round must be positive, got {blocks_per_round}")
    return int(genesis_height) + int(draws_made) * int(blocks_per_round)


def select_position(candidate_positions: List[int], block_hash: str) -> int:
    """Apply the rule.

    Positions are sorted rather than taken in the order they arrived, so the
    answer cannot depend on how the window happened to be serialised or on
    which client did the asking.
    """
    positions = sorted({int(p) for p in candidate_positions})
    if not positions:
        raise SelectionError("no candidates to draw from")
    digest = int(normalize_block_hash(block_hash), 16)
    return positions[digest % len(positions)]


# --- the chain ----------------------------------------------------------------

def block_hash_at(height: int, rpc: str = DEFAULT_RPC, timeout: int = 30) -> Optional[str]:
    """``chain_getBlockHash`` straight off a Bittensor node.

    A raw JSON-RPC call rather than a client library, so a reader can see
    exactly what was asked of the chain and be sure nothing was interpreted on
    the way. Returns None when the block does not exist yet, which is the
    normal state for most of a round and is not an error.
    """
    try:
        connection = create_connection(rpc, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        raise SourceUnavailable(f"cannot reach the Bittensor node at {rpc}: {e}") from None

    try:
        connection.send(json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "chain_getBlockHash", "params": [int(height)],
        }))
        reply = json.loads(connection.recv())
    except Exception as e:  # noqa: BLE001
        raise SourceUnavailable(f"no usable reply from {rpc}: {e}") from None
    finally:
        connection.close()

    if "error" in reply:
        raise SourceUnavailable(f"RPC error from {rpc}: {reply['error']}")
    result = reply.get("result")
    return normalize_block_hash(result) if result else None


# --- what Minos published -----------------------------------------------------

def fetch_json(url: str, timeout: int = 30) -> Dict[str, Any]:
    """GET and parse JSON, or raise SourceUnavailable.

    Network and decoding failures are folded into one exception so a caller
    reports "could not read X" rather than leaking a URLError or a JSON
    decoding error — neither of which tells a reader anything useful about
    what went wrong.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read())
    except Exception as e:  # noqa: BLE001
        raise SourceUnavailable(f"cannot read {url}: {e}") from None


def fetch_window(api: str = DEFAULT_API) -> Dict[str, Any]:
    return fetch_json(f"{api}/verification/task-window", timeout=30)


def fetch_round(round_id: str, api: str = DEFAULT_API) -> Dict[str, Any]:
    return fetch_json(f"{api}/verification/round/{round_id}", timeout=30)


# --- resolving a draw ---------------------------------------------------------

@dataclass
class Draw:
    """One resolved selection, or the reason it is not resolvable yet.

    Serialised with the same grouping the platform uses, so a reader moving
    between the two is not re-learning a second vocabulary for the same
    quantities.
    """
    status: str
    window_id: Optional[str] = None
    genesis_draw_height: Optional[int] = None
    draws_made: Optional[int] = None
    blocks_per_round: Optional[int] = None
    draw_block_height: Optional[int] = None
    draw_block_hash: Optional[str] = None
    candidate_positions: List[int] = field(default_factory=list)
    selected_position: Optional[int] = None
    selected: Optional[Dict[str, Any]] = None
    detail: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "status": self.status,
            "window_id": self.window_id,
            "candidate_positions": self.candidate_positions,
            "selection_rule": SELECTION_RULE,
        }
        # Present as soon as the height is known, which is before the block
        # exists: naming the block being waited for is the useful half of a
        # pending answer. Omitted entirely when even the height cannot be
        # derived, since there is then nothing to say.
        if self.draw_block_height is not None:
            out["draw"] = {
                "genesis_draw_height": self.genesis_draw_height,
                "draws_made": self.draws_made,
                "blocks_per_round": self.blocks_per_round,
                "block_height": self.draw_block_height,
                "block_hash": self.draw_block_hash,
            }
        if self.selected_position is not None:
            out["selected_position"] = self.selected_position
            out["selected"] = self.selected
        if self.detail:
            out["detail"] = self.detail
        return out


def resolve(window: Dict[str, Any], rpc: str = DEFAULT_RPC) -> Draw:
    """Resolve the draw for a published window.

    ``pending`` is a real answer, not a failure. Until the deciding block is
    produced the selection is unknowable — to this service, to Minos, and to
    everyone else — and reporting anything else would be inventing one.
    """
    candidates = sorted(int(p) for p in window.get("candidate_positions") or [])
    genesis = window.get("genesis_draw_height")
    draws_made = window.get("draws_made") or 0
    blocks_per_round = window.get("blocks_per_round")
    window_id = window.get("window_id")

    if not candidates:
        return Draw(status="pending", window_id=window_id,
                    detail="window has no candidates")
    if genesis is None:
        return Draw(status="pending", window_id=window_id,
                    candidate_positions=candidates,
                    detail="window has no genesis draw height")

    height = draw_height(genesis, draws_made, blocks_per_round)
    common = dict(window_id=window_id, genesis_draw_height=genesis,
                  draws_made=draws_made, blocks_per_round=blocks_per_round,
                  draw_block_height=height, candidate_positions=candidates)

    block_hash = block_hash_at(height, rpc)
    if block_hash is None:
        return Draw(status="pending",
                    detail=f"block {height} has not been produced yet", **common)

    position = select_position(candidates, block_hash)
    entry = next(
        (e for e in window.get("entries", []) if e.get("position") == position), None
    )
    return Draw(status="resolved", draw_block_hash=block_hash,
                selected_position=position, selected=entry, **common)


def check_round(round_data: Dict[str, Any], rpc: str = DEFAULT_RPC) -> Dict[str, Any]:
    """Recompute a created round's draw and compare it with what was used.

    The block hash is re-fetched using only the height the round reports.
    Accepting the hash reported alongside it would leave unchecked the single
    value that decides the outcome.

    Returns the individual checks rather than only a verdict, so a caller can
    see WHICH part failed without parsing prose.
    """
    draw = round_data.get("draw") or {}
    candidates = round_data.get("candidate_positions") or []
    claimed = round_data.get("selected_position")
    height = draw.get("block_height")
    reported_hash = draw.get("block_hash")

    result: Dict[str, Any] = {
        "round_id": round_data.get("round_id"),
        "checked": False,
        "verified": None,
        "block_height": height,
        "candidate_count": len(candidates),
        "claimed_position": claimed,
        "selection_rule": SELECTION_RULE,
    }

    if claimed is None or not candidates or height is None:
        result["detail"] = "round was not drawn from a window; nothing to check"
        return result

    result["checked"] = True
    actual = block_hash_at(height, rpc)
    if actual is None:
        result.update(verified=False, block_hash_matches=None,
                      detail=f"block {height} could not be read from the chain")
        return result

    result["block_hash"] = actual
    hash_ok = not reported_hash or normalize_block_hash(reported_hash) == actual
    result["block_hash_matches"] = hash_ok
    if not hash_ok:
        result.update(
            verified=False,
            detail=(f"block {height} hashes to {actual}, round reports "
                    f"{normalize_block_hash(reported_hash)}"))
        return result

    expected = select_position(candidates, actual)
    result["expected_position"] = expected
    result["position_matches"] = expected == claimed
    result["verified"] = expected == claimed
    result["detail"] = (
        f"position {claimed} of {len(candidates)} at block {height}"
        if expected == claimed
        else f"the rule gives position {expected}, the round used {claimed}"
    )
    return result
