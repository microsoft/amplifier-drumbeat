---
pack_format: 1
name: rule3-bin-missing
description: declares a tool that has no file in bin/
tools:
  - good-tool
---

The card declares `good-tool`, but bin/good-tool does not exist -- a card that
names a tool the agent cannot run.
