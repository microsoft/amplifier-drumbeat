---
pack_format: 1
name: rule2-unknown-key
description: everything valid except one unknown top-level key
tools:
  - good-tool
maintainer: someone
---

The card body says something. The only lie is the `maintainer:` key, which is
not in the closed drumpack-card.v1 vocabulary and must be refused, not dropped.
