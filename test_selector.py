"""The selection rule is a published contract, so these pin its outputs.

Minos runs the same rule on its side and takes this service's answer for the
round it creates. If an edit here quietly changed what a given (candidates,
block hash) produces, the two would disagree and rounds would stop — or worse,
agree on something neither intended. The concrete vectors below exist so that
cannot happen silently.

The last test replays a real round end to end, which is the one that would
catch a change nothing else noticed.

Run with: python -m pytest test_selector.py
"""
import pytest

import selector


# A real hash from finney. Anyone can fetch block 8983271 and confirm it, which
# is what ties these vectors to the chain rather than to our own arithmetic.
REAL_BLOCK_HEIGHT = 8983271
REAL_BLOCK_HASH = "6c34332d0c1842229581c00337e61d53c543544d0c8d9726c91ae931b04efd87"
REAL_CANDIDATES = list(range(20))
REAL_POSITION = 15


class TestBlockHashNormalisation:
    def test_strips_0x_and_lowercases(self):
        assert selector.normalize_block_hash("0x" + "AB" * 32) == "ab" * 32

    def test_tolerates_surrounding_whitespace(self):
        assert selector.normalize_block_hash(f"  {REAL_BLOCK_HASH}\n") == REAL_BLOCK_HASH

    def test_spelling_does_not_change_the_draw(self):
        """One RPC returns 0xAB..., another ab.... Same block, same answer."""
        plain = selector.select_position(REAL_CANDIDATES, REAL_BLOCK_HASH)
        for variant in ("0x" + REAL_BLOCK_HASH, REAL_BLOCK_HASH.upper(),
                        f"  {REAL_BLOCK_HASH}  "):
            assert selector.select_position(REAL_CANDIDATES, variant) == plain

    @pytest.mark.parametrize("bad", ["", "0x", "abc", "z" * 64, "a" * 63, "a" * 65])
    def test_rejects_malformed(self, bad):
        with pytest.raises(selector.SelectionError):
            selector.normalize_block_hash(bad)

    def test_rejects_non_string(self):
        with pytest.raises(selector.SelectionError):
            selector.normalize_block_hash(12345)


class TestDrawHeight:
    def test_the_first_draw_is_the_genesis_itself(self):
        assert selector.draw_height(8_983_271, 0, 360) == 8_983_271

    def test_each_draw_advances_by_a_round_of_blocks(self):
        assert selector.draw_height(8_983_271, 1, 360) == 8_983_631
        assert selector.draw_height(8_983_271, 2, 360) == 8_983_991

    def test_the_whole_sequence_follows_from_one_number(self):
        """Any future height is computable now, with nothing per-round in it."""
        assert [selector.draw_height(8_983_271, n, 360) for n in range(4)] == [
            8_983_271, 8_983_631, 8_983_991, 8_984_351,
        ]

    def test_rejects_a_missing_genesis(self):
        with pytest.raises(selector.SelectionError):
            selector.draw_height(None, 0, 360)

    def test_rejects_a_negative_draw_count(self):
        with pytest.raises(selector.SelectionError):
            selector.draw_height(100, -1, 360)

    def test_rejects_a_non_positive_interval(self):
        with pytest.raises(selector.SelectionError):
            selector.draw_height(100, 1, 0)


class TestSelection:
    def test_reproduces_a_real_draw(self):
        """Block 8983271 selects position 15 from a full window of twenty.

        The single most valuable test here: it ties the code to a block that
        exists on chain, so a change in behaviour shows up as a contradiction
        with something external rather than as a passing suite.
        """
        assert selector.select_position(REAL_CANDIDATES, REAL_BLOCK_HASH) == REAL_POSITION

    def test_is_deterministic(self):
        answers = {
            selector.select_position(REAL_CANDIDATES, REAL_BLOCK_HASH)
            for _ in range(500)
        }
        assert answers == {REAL_POSITION}

    def test_the_result_is_always_one_of_the_candidates(self):
        pool = [2, 4, 6, 8, 10]
        for i in range(256):
            assert selector.select_position(pool, f"{i:064x}") in pool

    def test_input_order_does_not_matter(self):
        """The window defines the order, never whoever happens to be asking."""
        pool = [11, 2, 7, 4]
        expected = selector.select_position(sorted(pool), REAL_BLOCK_HASH)
        assert selector.select_position(pool, REAL_BLOCK_HASH) == expected
        assert selector.select_position(list(reversed(pool)), REAL_BLOCK_HASH) == expected

    def test_duplicates_do_not_shift_the_draw(self):
        assert (selector.select_position([1, 2, 2, 3], REAL_BLOCK_HASH)
                == selector.select_position([1, 2, 3], REAL_BLOCK_HASH))

    def test_a_single_candidate_is_drawn_regardless_of_the_hash(self):
        assert selector.select_position([7], REAL_BLOCK_HASH) == 7

    def test_an_empty_pool_raises(self):
        with pytest.raises(selector.SelectionError):
            selector.select_position([], REAL_BLOCK_HASH)

    def test_draining_a_window_never_repeats_or_strands_a_slot(self):
        remaining, drawn = list(range(20)), []
        for i in range(20):
            pick = selector.select_position(remaining, f"{i * 7919:064x}")
            remaining.remove(pick)
            drawn.append(pick)
        assert sorted(drawn) == list(range(20))

    def test_the_draw_is_not_pinned_to_one_slot(self):
        """Catches the implementation bug where truncation fixes every round
        on position 0 while every other test still passes."""
        counts = {p: 0 for p in range(8)}
        for i in range(4000):
            counts[selector.select_position(list(range(8)),
                                            f"{(i * 2654435761) % (1 << 256):064x}")] += 1
        assert all(counts.values())
        assert max(counts.values()) < 3 * min(counts.values())


class TestResolve:
    def _window(self, **over):
        window = {
            "window_id": "w-test",
            "candidate_positions": REAL_CANDIDATES,
            "genesis_draw_height": REAL_BLOCK_HEIGHT,
            "draws_made": 0,
            "blocks_per_round": 360,
            "entries": [{"position": p} for p in REAL_CANDIDATES],
        }
        window.update(over)
        return window

    def test_pending_still_names_the_block_being_waited_for(self, monkeypatch):
        """The normal state for most of a round, and not a failure.

        The height is known long before the block exists, and saying which
        block is awaited is the useful half of a pending answer — a caller can
        then tell "not yet" from "something is wrong".
        """
        monkeypatch.setattr(selector, "block_hash_at", lambda *_a, **_k: None)
        draw = selector.resolve(self._window())
        body = draw.as_dict()

        assert draw.status == "pending"
        assert "has not been produced" in draw.detail
        assert body["draw"]["block_height"] == REAL_BLOCK_HEIGHT
        assert body["draw"]["block_hash"] is None
        assert "selected_position" not in body

    def test_pending_without_a_genesis(self, monkeypatch):
        monkeypatch.setattr(selector, "block_hash_at", lambda *_a, **_k: REAL_BLOCK_HASH)
        draw = selector.resolve(self._window(genesis_draw_height=None))
        assert draw.status == "pending"

    def test_resolved_names_the_position_and_the_block(self, monkeypatch):
        monkeypatch.setattr(selector, "block_hash_at", lambda *_a, **_k: REAL_BLOCK_HASH)
        body = selector.resolve(self._window()).as_dict()
        assert body["status"] == "resolved"
        assert body["selected_position"] == REAL_POSITION
        assert body["draw"]["block_height"] == REAL_BLOCK_HEIGHT
        assert body["draw"]["block_hash"] == REAL_BLOCK_HASH

    def test_the_height_advances_with_draws_made(self, monkeypatch):
        seen = {}

        def fake(height, *_a, **_k):
            seen["height"] = height
            return REAL_BLOCK_HASH

        monkeypatch.setattr(selector, "block_hash_at", fake)
        selector.resolve(self._window(draws_made=3))
        assert seen["height"] == REAL_BLOCK_HEIGHT + 3 * 360


class TestCheckRound:
    def _round(self, **over):
        data = {
            "round_id": "round-under-test",
            "candidate_positions": REAL_CANDIDATES,
            "selected_position": REAL_POSITION,
            "draw": {"block_height": REAL_BLOCK_HEIGHT, "block_hash": REAL_BLOCK_HASH},
        }
        data.update(over)
        return data

    def test_a_correct_round_verifies(self, monkeypatch):
        monkeypatch.setattr(selector, "block_hash_at", lambda *_a, **_k: REAL_BLOCK_HASH)
        result = selector.check_round(self._round())
        assert result["verified"] and result["checked"]
        assert result["block_hash_matches"] and result["position_matches"]

    def test_a_round_using_the_wrong_position_fails(self, monkeypatch):
        monkeypatch.setattr(selector, "block_hash_at", lambda *_a, **_k: REAL_BLOCK_HASH)
        result = selector.check_round(self._round(selected_position=3))
        assert result["verified"] is False
        assert result["expected_position"] == REAL_POSITION
        assert result["position_matches"] is False

    def test_a_round_reporting_a_hash_the_chain_disagrees_with_fails(self, monkeypatch):
        """The reason the hash is re-fetched rather than believed."""
        monkeypatch.setattr(selector, "block_hash_at", lambda *_a, **_k: "cd" * 32)
        result = selector.check_round(self._round())
        assert result["verified"] is False
        assert result["block_hash_matches"] is False

    def test_a_round_never_drawn_from_a_window_is_skipped_not_failed(self, monkeypatch):
        """Reporting a pre-window round as a failure would be wrong; reporting
        it as a pass would be worse."""
        monkeypatch.setattr(selector, "block_hash_at", lambda *_a, **_k: REAL_BLOCK_HASH)
        result = selector.check_round(self._round(selected_position=None, draw={}))
        assert result["checked"] is False
        assert result["verified"] is None

    def test_an_unreadable_block_is_a_failure_not_a_pass(self, monkeypatch):
        monkeypatch.setattr(selector, "block_hash_at", lambda *_a, **_k: None)
        result = selector.check_round(self._round())
        assert result["verified"] is False
