# Third-party material

The root [MIT License](LICENSE) applies to original Vibe Squad material only. The files below are
distributed under the licence shown for each file; their accompanying notices control for those
files. Entries marked "private source only" are not part of the public projection.

| Path | Licence | Notes |
|---|---|---|
| `CODE_OF_CONDUCT.md` | CC BY 4.0 | Adapted from the [Contributor Covenant v2.1](https://github.com/EthicalSource/contributor_covenant/tree/2.1). Not MIT. |
| `departments/sysmgmt/reference/dsd-SKILL.md` | MIT | Separately licensed material from FrozenPepper; see `departments/sysmgmt/reference/dsd-LICENSE`. |
| `shared/skills/_retired/gptscan-prompt-templates.md` | AGPL-3.0-only | Private source only. Carries a provenance marker deriving from GPTScan; pending clean-room rewrite it is **not** MIT, so it is withheld from the projection rather than distributed under an MIT grant it does not have. |
| `tools/radar/templates/positive-control-pda-sharing.yaml` | GPL-3.0-only | Private source only. Unmodified copy from `Auditware/radar`. |
| `tools/radar/compose.yaml` | GPL-3.0-only | Private source only. Modified derivative of the same upstream. |
| `shared/skills/defensive-pattern-discovery.md` | AGPL-3.0-only | Private source only. Tracks OpenZeppelin's `develop-secure-contracts` skill. |
| `.agents/skills/defensive-pattern-discovery/SKILL.md` | AGPL-3.0-only | Private source only. Generated copy of the above. |
| `.claude/skills/defensive-pattern-discovery/SKILL.md` | AGPL-3.0-only | Private source only. Generated copy of the above. |

Solidity fixtures carrying their own `SPDX-License-Identifier` headers keep those identifiers.

## Plugins

The plugins under `plugins/` are original Vibe Squad code, but several declare their own licence in
their `.claude-plugin/plugin.json` manifest instead of taking the root MIT default. That manifest is
the accompanying notice for the whole plugin tree, so the register lists them here even though they are
first-party rather than third-party. Whether each publishes is decided by the export path policy
(`tools/export/policy/path-policy.json`) and was verified per file against that policy.

| Plugin | Licence | Notes |
|---|---|---|
| `plugins/chrono-media-studio/` | AGPL-3.0-or-later | Declared in its `.claude-plugin/plugin.json` (`"license": "AGPL-3.0-or-later"`). Publishes. |
| `plugins/chrono-recon/` | AGPL-3.0-or-later | Declared in its `.claude-plugin/plugin.json` (`"license": "AGPL-3.0-or-later"`). Publishes. |
| `plugins/chrono-research-arsenal/` | AGPL-3.0-or-later | Declared in its `.claude-plugin/plugin.json` (`"license": "AGPL-3.0-or-later"`). Publishes. |
| `plugins/chrono-vault/` | AGPL-3.0-or-later | Declared in its `.claude-plugin/plugin.json` (`"license": "AGPL-3.0-or-later"`). Publishes. |
| `plugins/chrono-dedup/` | AGPL-3.0-or-later | Declared in its `.claude-plugin/plugin.json` (`"license": "AGPL-3.0-or-later"`). Private source only; the export policy withholds the whole tree (`plugins/*-dedup/**`). |
| `plugins/security-mcp-stack/` | MIT (root default) | Has no `plugin.json`, so it declares no licence of its own and is covered by the root MIT licence. Publishes, except three operator-specific inputs the export policy withholds (`held-solodit.json`, `snyk-preactivation-targets.json`, `preactivate-security-stack.sh`). |

`plugins/security-mcp-stack/` is listed for completeness only: with no manifest it carries no licence
exception, so the root MIT licence applies to it as it does to any other original material.

Publication and licensing are separate decisions: the four catalogued plugins publish under their
declared AGPL-3.0-or-later notices, while the same licence does not override the policy withholding
`chrono-dedup`. There are five public plugin directories including `security-mcp-stack`.
No source download for `chrono-dedup` is supplied in this checkout; obtaining a separate source
release requires contacting the maintainer. Public capability declarations retain it only as
requiring operator installation, with manual prior-art searches as the fallback.
