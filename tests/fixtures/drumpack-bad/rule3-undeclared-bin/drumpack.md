---
pack_format: 1
name: rule3-undeclared-bin
description: declares good-tool but ships a second, undeclared executable
tools:
  - good-tool
---

The card declares only `good-tool`, but bin/ also holds an executable `sneaky`
the card never declares -- a card that lies in the other direction.
