#!/usr/bin/env python3
"""EXAMPLE, NOT PRODUCT -- copy this file and own it.

Tails the engine's outbox and fires a desktop notification for every
delivery intent whose verdict is `deliver`. Stdlib only, no config file, no
service unit, no retries, no state beyond a file offset in memory. It is
deliberately the smallest thing that closes the loop, so you can read all of
it in one sitting and then throw it away and write yours.

The engine never delivers anything itself. It appends a reasoned record to
`<data-dir>/engine-events.jsonl` and stops. THIS is the half you own.

    python3 tail_intents.py /path/to/data-dir

Starts at the END of the file: existing history is not replayed. That is the
right default for a demo and the wrong one for a real consumer -- a real one
persists its cursor so a restart cannot skip an intent that arrived while it
was down. Named here because that gap is the whole difference between an
example and a delivery worker.

Withheld intents are printed, not notified. Reading a few of those is the
fastest way to see what the engine is actually for: the run that decided to
stay quiet wrote down why.
"""

import json
import subprocess
import sys
import time
from pathlib import Path


def notify(title: str, body: str) -> None:
    """Fire a desktop notification. Replace this with your transport."""
    try:
        subprocess.run(["notify-send", title, body], check=False)
    except FileNotFoundError:
        # No notify-send on this box (headless, container, macOS). Say so once
        # per event rather than dying: an example that crashes on the first
        # intent teaches nothing about the intent.
        print(f"[would notify] {title}: {body}", flush=True)

    # ntfy.sh variant -- push to a phone instead of a desktop, no account,
    # no daemon. Pick an unguessable topic; anyone who knows it can read it.
    #
    # import urllib.request
    # urllib.request.urlopen(urllib.request.Request(
    #     "https://ntfy.sh/your-unguessable-topic-here",
    #     data=body.encode("utf-8"),
    #     headers={"Title": title},
    # ))


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    path = Path(sys.argv[1]).expanduser() / "engine-events.jsonl"
    print(f"following {path} (ctrl-c to stop)", file=sys.stderr)

    while not path.exists():
        time.sleep(1.0)

    with open(path, encoding="utf-8") as f:
        f.seek(0, 2)  # start at the end: no replay of history
        while True:
            line = f.readline()
            if not line:
                time.sleep(1.0)
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # A partial line: the writer is mid-append. Rewind and retry
                # rather than dropping it -- silently skipping an intent is
                # the one bug a delivery consumer must not have.
                f.seek(f.tell() - len(line))
                time.sleep(0.2)
                continue

            kind = event.get("type")
            name = event.get("automation", "?")

            if kind == "automation_error":
                notify(f"{name} FAILED", str(event.get("reason", ""))[:400])
            elif kind == "delivery_intent":
                if event.get("verdict") == "deliver":
                    notify(name, str(event.get("text", ""))[:400])
                else:
                    print(
                        f"[withheld] {name}: gate={event.get('gate')} "
                        f"reason={event.get('reason')}",
                        flush=True,
                    )


if __name__ == "__main__":
    raise SystemExit(main())
