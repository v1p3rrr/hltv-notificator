"""Print a match's transcript, for when the bot is not the way you want it.

    docker compose exec app python -m hltv_notify.matchlog_cli > match.txt

With no match id it prints the most recent one; with `--list` it says which
are still kept. The transcript lives in the database and is pruned by age, so
there is nothing to find on disk — this is the way to get it out as a file.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from .config import Config
from .state.db import Storage
from .state import matchlog


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("match_id", type=int, nargs="?",
                        help="which match; the most recent one by default")
    parser.add_argument("--list", action="store_true",
                        help="list the transcripts still kept and exit")
    args = parser.parse_args(argv)

    config = Config()
    storage = Storage(config.db_path)
    try:
        kept = storage.match_log_ids()
        if args.list or not kept:
            if not kept:
                print("No transcripts kept.", file=sys.stderr)
                return 1
            for row in kept:
                print(f"{row['match_id']}\t{row['lines']} lines\t{row['last_utc']}")
            return 0

        match_id = args.match_id or int(kept[0]["match_id"])
        rows = storage.match_log_lines(match_id)
        if not rows:
            print(f"No transcript for match {match_id}.", file=sys.stderr)
            return 1
        row = storage.get_match(match_id)
        title = ""
        if row is not None:
            team = storage.team_name(
                storage.canonical_team(match_id) or config.team_id,
                config.team_name)
            title = f"{team} vs {row['opponent_name']}"
        # Written to stdout rather than to a file this picks the name of: the
        # caller redirects it where they want it, and a container writing files
        # into its own filesystem helps nobody.
        sys.stdout.write(matchlog.to_text(match_id, title, rows))
        return 0
    finally:
        storage.close()


if __name__ == "__main__":
    sys.exit(main())
