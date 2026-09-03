"""Command line for the same selection the service performs.

Runs against the public Minos API and a public Bittensor node by default, so
it works with no configuration. Point ``--rpc`` at your own node to read block
hashes directly, and ``--api`` at a different deployment.

Exit codes are meant for scripting: 0 when a round verifies or when there was
nothing to verify, 1 when a round contradicts the rule, 2 when something could
not be read at all. The third is deliberately distinct — being unable to check
is not the same as having checked and found a problem.
"""
from __future__ import annotations

import argparse
import sys
import time

import selector

OK, BAD, DIM, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def cmd_next(args) -> int:
    window = selector.fetch_window(args.api)
    draw = selector.resolve(window, args.rpc)

    print(f"window        {draw.window_id}  ({len(draw.candidate_positions)} candidates)")
    if draw.genesis_draw_height is not None:
        print(f"genesis       {draw.genesis_draw_height}  "
              f"{DIM}(pinned when the window was published){OFF}")
        print(f"draw height   {draw.draw_block_height}  "
              f"{DIM}= {draw.genesis_draw_height} + {draw.draws_made} "
              f"x {draw.blocks_per_round}{OFF}")

    if draw.status != "resolved":
        print(f"\n{draw.detail}")
        return 0

    print(f"block hash    {draw.draw_block_hash}")
    print(f"\n{OK}selected position {draw.selected_position}{OFF}")
    hashes = (draw.selected or {}).get("file_hashes") or {}
    if hashes:
        # Hashes only. The window identifies a task by the content of its
        # files, which is what a check runs on; anyone served those files can
        # match them here without the task being described in advance.
        print(f"  bam    {hashes.get('bam')}")
        print(f"  truth  {hashes.get('truth_vcf')}")
        print(f"  mut    {hashes.get('mutations_vcf')}")
    return 0


def _verdict(result) -> str:
    """PASS, FAIL, or SKIP.

    SKIP is its own outcome rather than a pass: a round created before this
    mechanism existed was never drawn from a window, so there is nothing to
    check, and colouring that green would claim a verification that never
    happened.
    """
    if not result.get("checked"):
        return f"{DIM}SKIP{OFF}"
    return f"{OK}PASS{OFF}" if result.get("verified") else f"{BAD}FAIL{OFF}"


def cmd_verify(args) -> int:
    data = selector.fetch_round(args.round_id, args.api)
    result = selector.check_round(data, args.rpc)
    hashes = data.get("file_hashes") or {}

    print(f"{_verdict(result)}  {result.get('round_id')}  {result.get('detail')}")
    print(f"      bam {hashes.get('bam')}")
    # Exit 0 for a pass and for nothing-to-check; only a round that contradicts
    # the rule is a failure, so this is usable in a script without treating
    # older rounds as alarms.
    return 0 if result.get("verified") or not result.get("checked") else 1


def cmd_audit(args) -> int:
    window = selector.fetch_window(args.api)
    drawn = [e for e in window.get("entries", []) if e.get("status") == "drawn"]
    if not drawn:
        print("No drawn entries in the window yet.")
        return 0

    failures = 0
    for entry in drawn[: args.limit]:
        round_id = (entry.get("drawn") or {}).get("round_id")
        if not round_id:
            continue
        # `is False` rather than falsy: an unchecked round reports None, and
        # counting that as a failure would raise an alarm about a round that
        # was never claimed to have been drawn this way.
        result = selector.check_round(selector.fetch_round(round_id, args.api), args.rpc)
        failures += result.get("verified") is False
        print(f"{_verdict(result)}  {round_id}  {result.get('detail')}")
    print(f"\n{min(len(drawn), args.limit)} checked, {failures} failed")
    return 1 if failures else 0


def cmd_watch(args) -> int:
    """Check each draw as it appears, and keep going.

    Rounds are only checked once — the window keeps recent draws visible for
    several polls, and re-reporting them would bury a new result. Transient
    failures are retried rather than fatal, since a watcher that dies on one
    bad response stops watching precisely when something is going wrong.
    """
    seen = set()
    print(f"Watching {args.api} every {args.interval}s. Ctrl-C to stop.\n")
    while True:
        try:
            window = selector.fetch_window(args.api)
            for entry in window.get("entries", []):
                round_id = (entry.get("drawn") or {}).get("round_id")
                if entry.get("status") != "drawn" or not round_id or round_id in seen:
                    continue
                seen.add(round_id)
                result = selector.check_round(
                    selector.fetch_round(round_id, args.api), args.rpc)
                print(f"{time.strftime('%H:%M:%S')}  {_verdict(result)}  "
                      f"{round_id}  {result.get('detail')}")
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # noqa: BLE001
            print(f"{time.strftime('%H:%M:%S')}  {DIM}retrying after: {e}{OFF}")
        time.sleep(args.interval)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--api", default=selector.DEFAULT_API,
        help=f"Minos API to read the window from (default: {selector.DEFAULT_API})")
    parser.add_argument(
        "--rpc", default=selector.DEFAULT_RPC,
        help=f"Bittensor node to read block hashes from "
             f"(default: {selector.DEFAULT_RPC})")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("next", help="resolve the next draw")

    p_verify = sub.add_parser("verify", help="check one round")
    p_verify.add_argument("round_id")

    p_audit = sub.add_parser("audit", help="check drawn rounds")
    p_audit.add_argument("--limit", type=int, default=20,
                         help="how many recent draws to check (default: 20)")

    p_watch = sub.add_parser("watch", help="check draws as they land")
    p_watch.add_argument("--interval", type=int, default=60,
                         help="seconds between polls (default: 60)")

    args = parser.parse_args()
    command = {"next": cmd_next, "verify": cmd_verify,
               "audit": cmd_audit, "watch": cmd_watch}[args.command]

    try:
        return command(args)
    except selector.SourceUnavailable as e:
        # Exit 2, not 1: nothing was verified, so reporting a failed
        # verification would claim a check that never ran.
        print(f"{BAD}cannot check{OFF}  {e}", file=sys.stderr)
        return 2
    except selector.SelectionError as e:
        print(f"{BAD}unusable input{OFF}  {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
