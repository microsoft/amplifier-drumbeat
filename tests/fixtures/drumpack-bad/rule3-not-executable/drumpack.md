---
pack_format: 1
name: rule3-not-executable
description: declares a tool whose file exists but has no exec bit
tools:
  - good-tool
---

The card declares `good-tool` and the file exists, but its exec bit is unset --
theater with a file extension.
