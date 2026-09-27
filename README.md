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

Each product ships with a sensible default rule (trigger/exit values) that can be adjusted, or replaced entirely with a custom rule.

## How a payout is decided

```
payout_ratio = clamp((trigger - observed) / (trigger - exit), 0, 1)
```

- The **floor** is the lowest payout ratio any source allows — paid immediately, no review needed.
- The **ceiling** is the highest payout ratio any source allows.
- If the gap between floor and ceiling is within tolerance, the sources count as agreeing and the full amount is paid.
- If the gap exceeds tolerance, the floor amount pays now and the difference is locked in an on-chain escrow account (a program-controlled PDA, not held by the app) until the next reading or a reviewer resolves it.

## Architecture

This is a monorepo with three parts:

```
┌──────────────────┐        ┌───────────────────────┐        ┌──────────────────────────┐
│  AgnetPay          │  HTTP  │  agentpay_backend       │  RPC   │  agent_pay_vault           │
│  React + Vite + TS │ ─────▶ │  FastAPI policy engine, │ ─────▶ │  Anchor program, devnet    │
│  frontend           │        │  data fetch, AI         │        │  escrow PDAs               │
│                     │        │  watchdog, wallet       │        │  release / void authority  │
└──────────────────┘        └───────────────────────┘        └──────────────────────────┘
                                        │
                                        ▼
                        Open-Meteo · ECMWF · Sentinel-2/Landsat
                        (via Agromonitoring) · Ticketmaster
```

- **`AgnetPay/`** — the frontend. Calls the backend over HTTP, shows the Playground (create/evaluate a policy) and Activity (insurer wallet + on-chain history) views.
- **`agentpay_backend/`** — a FastAPI service. Creates and evaluates policies, aggregates weather/satellite/event/delay data, computes the payout formula, and submits transactions from an insurer wallet on Solana devnet. Can escrow disputed amounts either in a JSON ledger (default) or on-chain via `agent_pay_vault` (`AGENT_PAY_ONCHAIN_ESCROW=1`).
- **`agent_pay_vault/`** — an Anchor (Rust) program deployed to Solana devnet. Holds disputed escrow amounts in program-controlled PDAs; only `release_escrow` / `void_escrow`, signed by the insurer authority, can move them.

## Tech stack

- **Frontend**: React 19, TypeScript, Vite 6, `lucide-react` icons, hand-rolled CSS
- **Backend**: Python, FastAPI, `solana-py` / `solders`, httpx, an AI-assisted watchdog (Groq or Claude) for dispute explanations
- **Blockchain**: Solana devnet, Anchor 0.30.1 — a deployed vault program plus an insurer wallet that pays directly

## Deployed program

`agent_pay_vault`, Solana **devnet**, program id `CvE7xMfwbpMmCz9Pvt6RsG9tHxNUpbuG5KwyGuFHSxCk` — [view on Solana Explorer](https://explorer.solana.com/address/CvE7xMfwbpMmCz9Pvt6RsG9tHxNUpbuG5KwyGuFHSxCk?cluster=devnet).

## Setup

Each part is run separately; see each folder's own README for details.

### Frontend (`AgnetPay/`)

```sh
cd AgnetPay
npm install
cp .env.example .env.local   # point VITE_PROXY_TARGET at your backend if not localhost:8000
npm run dev
```

Requires Node.js 20.19+ or 22.12+, and the backend running.

### Backend (`agentpay_backend/`)

```sh
cd agentpay_backend
pip install -r requirements.txt
cp .env.example .env         # fill in optional keys yourself, never commit real values
uvicorn app.main:app --reload --port 8000
```

Swagger docs at `http://localhost:8000/docs`. On first start it generates two devnet wallets (`wallet.json`, `service_wallet.json`) — both gitignored, both contain private keys, never commit or share them.

### Vault program (`agent_pay_vault/`)

Anchor program, already deployed to devnet at the program id above. To rebuild it yourself:

```sh
cd agent_pay_vault
anchor build
anchor deploy --provider.cluster devnet
```

Built and deployed with the `backpackapp/build:v0.30.1` Docker image (pinned `Cargo.lock` for Anchor 0.30.1 compatibility). The backend only needs the program id and the IDL (already included at `agentpay_backend/app/idl/agent_pay_vault.json`) to talk to it — rebuilding this program is only needed if you're changing the on-chain logic itself.

None of these `.env`/`.env.local`/`.env.example` values or keypairs contain real secrets in this repository — every actual key lives only in gitignored local files.

## Status & roadmap

This is a hackathon prototype. Known gaps:

- **Pricing / premiums are not implemented.** Policies are created with a fixed cover amount; there is no premium calculation, underwriting, or payment collection from the policyholder.
- **Travel delay data is simulated.** The other three products read live weather/satellite/event data; travel delay currently uses demo feeds only.
- **On-chain escrow is opt-in.** The default escrow path is a JSON ledger in the backend; the deployed vault program is used only when `AGENT_PAY_ONCHAIN_ESCROW=1` is set.
- **Devnet only.** Nothing here is audited or intended for mainnet funds.

## License

MIT — see [LICENSE](LICENSE).
