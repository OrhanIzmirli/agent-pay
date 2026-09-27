# Agent Pay

Parametric insurance that settles itself: two independent data sources decide the payout, and the money moves on Solana the moment the rule is hit — no adjuster, no claim form, no waiting.

Built at Blockchain Hack Warsaw (Colosseum hackathon).

## The problem

Traditional parametric insurance still relies on a single data feed and a manual claims process: a loss report, an adjuster visit, a paper trail. The policyholder waits — often a full season for a crop, or weeks for a delayed flight — for a payout that a simple formula could have already resolved.

## The solution

Agent Pay reads two independent sources for every policy (plus a satellite crop-health index where relevant) and applies a deterministic formula to decide how much is owed. When the sources agree, the full amount pays out immediately. When they disagree, only the portion every source agrees on is paid at once; the disputed remainder is held in an on-chain escrow account until a fresh reading resolves it or a human reviewer decides. An AI watchdog can explain *why* sources disagree, but it never sets a payout amount or moves funds — only the formula and a human reviewer can do that.

Every payout is a real transaction on Solana devnet, verifiable on [Solana Explorer](https://explorer.solana.com/address/D93HiJbqXdt13pQxmehaqFvYGieRGrvXHxcVXt584N8B?cluster=devnet).

## Products

| Product | Covers | Trigger source(s) | Data |
|---|---|---|---|
| **Crop – Drought** | Rainfall falling below a threshold during the growing window | Two independent rainfall models, optional satellite NDVI | Live |
| **Crop – Excess rain** | Rainfall exceeding a threshold (flooding) | Two independent rainfall models, optional satellite NDVI | Live |
| **Event cancellation** | An outdoor event disrupted by weather | Two independent rainfall models, plus ticketing status as a second opinion | Live |
| **Travel delay** | A flight or journey delayed past a threshold | Two independent delay feeds | Simulated (demo) |

Each product ships with a sensible default rule (trigger/exit values) that can be adjusted, or replaced entirely with a custom rule (direction, thresholds, satellite settings).

## How a payout is decided

```
payout_ratio = clamp((trigger − observed) / (trigger − exit), 0, 1)
```

- The **floor** is the lowest payout ratio any source allows — paid immediately, no review needed.
- The **ceiling** is the highest payout ratio any source allows.
- If the gap between floor and ceiling is within tolerance, the sources count as agreeing and the full amount is paid.
- If the gap exceeds tolerance, the floor amount pays now and the difference is escrowed on-chain (in a program-controlled account, not held by the app) until the next reading or a reviewer resolves it.

## Architecture

This repository contains the **frontend only**: a React + TypeScript single-page app that talks to a separate backend service over HTTP.

```
┌─────────────────┐        ┌──────────────────────┐        ┌────────────────┐
│  React frontend  │  HTTP  │   Backend API         │  RPC   │  Solana devnet  │
│  (this repo)      │ ─────▶ │  (policy engine,      │ ─────▶ │  insurer wallet │
│  Vite + TS         │        │   weather/data fetch, │        │  + escrow PDAs  │
└─────────────────┘        │   AI watchdog)         │        └────────────────┘
                            └──────────────────────┘
                                       │
                                       ▼
                        Open-Meteo · ECMWF · Sentinel-2/Landsat
                        (via Agromonitoring) · Ticketmaster
```

The backend creates and evaluates policies, fetches weather/satellite/delay/event data, computes the payout formula, and submits transactions from an insurer wallet on Solana devnet. It is deployed separately and is not part of this repository.

## Tech stack

- **Frontend**: React 19, TypeScript, Vite 6, `lucide-react` icons, hand-rolled CSS (no UI framework)
- **Backend** (external service, called over HTTP): policy engine, weather/satellite/event/delay data aggregation, AI-assisted dispute explanations
- **Blockchain**: Solana devnet — an insurer wallet pays the floor amount directly and locks the disputed remainder in a program-controlled escrow account (PDA) until it is released or voided

## Setup

Requires Node.js 20.19+ or 22.12+, and a running backend (see [Architecture](#architecture)).

```sh
npm install
cp .env.example .env.local   # edit if your backend runs somewhere other than localhost:8000
npm run dev
```

Open the local URL printed by Vite. To create a production build:

```sh
npm run build
npm run preview
```

### Configuration

| Variable | Purpose | Default |
|---|---|---|
| `VITE_PROXY_TARGET` | Dev-only: where the Vite dev server proxies `/api` requests | `http://localhost:8000` |
| `VITE_API_BASE_URL` | Production: base URL the built app calls directly | — |
| `VITE_SOLANA_CLUSTER` | Cluster used for Explorer links only | `devnet` |

These values are public and get baked into the client bundle at build time — never put secrets in them. `.env.example` documents the shape; copy it to `.env.local` for local development. There are no API keys or private keys anywhere in this frontend.

## API surface (backend)

- `GET /products` — the product catalog (labels, units, default rules, themes)
- `POST /policy`, `GET /policy/{id}`
- `POST /policy/{id}/evaluate` — optional `{simulate: [{mm, label}]}` for demo readings
- `POST /policy/{id}/resolve?release=true|false` — reviewer decision on an escrowed amount
- `GET /wallet/balance` — `{address, balance_sol}`
- `GET /wallet/history` — `{address, transactions: [{signature, slot, err}]}`

Creating and evaluating a policy triggers real devnet payments: the floor amount immediately, the escrowed delta only after resolution. No request is made on page load, duplicate submissions are blocked while one is pending, and POST requests are never retried automatically — a returned signature means "submitted", not "confirmed".

## Project layout

- `src/main.tsx` — the Playground and Activity dashboard
- `src/Landing.tsx` — the marketing/overview page
- `src/api.ts` — the API client and shared types
- `src/styles.css`, `src/workspace.css`, `src/landing.css` — the visual system
- `docs/` — screenshots used during development

## Status & roadmap

This is a hackathon prototype. Known gaps:

- **Pricing / premiums are not implemented.** Policies are created with a fixed cover amount; there is no premium calculation, underwriting, or payment collection from the policyholder.
- **Travel delay data is simulated.** The other three products read live weather/satellite/event data; travel delay currently uses demo feeds only.
- **No persistence guarantees beyond the backend's own storage** — policies and escrow state live in the backend service, not on this frontend.
- **Devnet only.** Nothing here is audited or intended for mainnet funds.

## License

MIT — see the repository root [LICENSE](../LICENSE).
