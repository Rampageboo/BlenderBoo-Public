# Research Policy

## Status

External research is **not adopted** for this project. No ChatGPT Project or
other research container is configured, and the optional
`run-chatgpt-research` Skill is not installed here.

An absent Research Skill is not an installation error. When a question genuinely
requires external evidence, report the capability gap rather than inventing a
research contract.

## Boundary if it is adopted later

This project runs entirely locally today — the bridge is loopback only, the
MCP process is local stdio, and the codebase map is built by local AST
extraction with no external calls. Adopting research would be the first outbound path for
project content, so it needs an explicit Owner decision covering:

- whether private repository content may be transmitted at all;
- which source classes are approved;
- the research container and its memory mode;
- retention and cost limits.

Until those are recorded, `private_content_transmission` stays
`ask-each-time` in `exact-facts.yaml`.

## Active Research Decisions

None.
