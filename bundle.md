---
bundle:
  name: drumbeat
  version: 0.1.0
  description: >-
    Skills for the drumbeat automation engine -- operate it, author automation
    files, build drumpacks. This root bundle is the standalone entry point; the
    composable surface for adding these skills to an existing bundle is
    behaviors/drumbeat.yaml (see README "Skills").

includes:
  - bundle: git+https://github.com/microsoft/amplifier-foundation@main
  - bundle: drumbeat:behaviors/drumbeat

tools:
  - module: tool-skills
    source: git+https://github.com/microsoft/amplifier-bundle-skills@main#subdirectory=modules/tool-skills
    config:
      skills:
        - "@drumbeat:skills"
---

# drumbeat -- skills bundle

This bundle exists so an Amplifier session can learn drumbeat from the repo
itself. It ships exactly three skills and nothing else:

| Skill | What it teaches |
|---|---|
| `drumbeat-operations` | Install, run, supervise, health-check, rotate sessions, drain, troubleshoot |
| `drumbeat-automation-authoring` | The automation-file contract as a how-to |
| `drumbeat-drumpack-authoring` | The drumpack card, `bin/` conventions, wiring |

Most users want the **behavior**, composed onto their own bundle:

```bash
amplifier bundle add git+https://github.com/microsoft/amplifier-drumbeat@main#subdirectory=behaviors/drumbeat.yaml --app
```

Running this root bundle standalone gives a foundation session with the same
three skills loaded. The engine itself is installed separately with
`uv tool install git+https://github.com/microsoft/amplifier-drumbeat` -- see
the repository README.
