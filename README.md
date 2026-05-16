# LATTICE

**Layered Agentic Topology with Typed Interactive Channels & Edges**

An opinionated daily-driver AI mesh built on top of the [RAVEN_MESH](https://github.com/coltonkirsten/RAVEN_MESH) protocol. RAVEN_MESH gives us the wire format, identity, routing, and audit. LATTICE adds the actual nodes Colton uses every day: Claude-backed agents, a control panel, and host-specific bootstrap scripts.

## Status

Day 0 — bootstrap. Three workers spinning in parallel to populate `nodes/`, `hosts/`, and the control panel.

## Repo layout

```
manifest.yaml           — declares all nodes + edges in the mesh
nodes/
  edith/                — Sonnet 4.6 Nexus-style agent, Dockerized, runs on Mac mini
  control/              — web app, runs on Colton's machine, visualizes the mesh
hosts/
  mac-mini/             — boot scripts for the Mac mini (core + edith)
  coltons-mac/          — boot scripts for Colton's machine (control)
shared/
  schemas/              — JSON schemas for each surface's input payload
  secrets.yaml.example  — secret naming convention (never commit real secrets)
scripts/
  bootstrap.sh          — generates per-host secrets, fills .env templates
```

## Boot order

1. Mac mini: start RAVEN_MESH Core with this repo's `manifest.yaml`.
2. Mac mini: start EDITH (Docker container).
3. Colton's Mac: start control panel (web app).
4. All three nodes register with Core; control panel renders the live topology.

## Conventions

- **Pull before you push.** This repo is collaborative.
- **Secrets via env vars only.** `manifest.yaml` references secrets as `env:VAR_NAME`. Never commit real secret values.
- **One subfolder per node.** New nodes get their own directory under `nodes/`.
- **One subfolder per host.** New machines joining the mesh get their own directory under `hosts/`.

## Built on

- Protocol layer: [RAVEN_MESH](https://github.com/coltonkirsten/RAVEN_MESH) v0.4
- Agent runtime: Anthropic Claude SDK (Sonnet 4.6)
- Transport: Tailscale-routed HTTP across hosts
