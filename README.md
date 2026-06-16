# BSC Case Study

This repository contains small investigation scripts for tracing a BSC phishing
case from known contract addresses to deployers, gas funding wallets, and related
infrastructure.

## Setup

Create a `.env` file with:

```bash
BSCSCAN_API_KEY=your_bscscan_or_etherscan_v2_api_key
BSC_RPC_URL=your_bsc_rpc_url
```

Install dependencies and run scripts from the repository root:

```bash
pip install -r requirements.txt
.venv/bin/python src/get_creator.py
```

## Investigation Flow

Recommended order:

1. `src/get_creator.py`
   Finds the deployer/creator and deployment transaction for the two known
   phishing contracts.

2. `src/find_creator_funding_before_deploy.py`
   For each known phishing contract, finds the creator's last incoming BNB
   transfer before the contract deployment transaction. This identifies the
   first-layer gas funding wallet.

3. `src/check_creator_batch_deployments.py`
   Checks whether each creator deployed contracts in bulk. It lists successful
   contract-creation transactions and marks the original known phishing
   contracts.

4. `src/check_funding_wallet_links.py`
   Analyzes the gas funding wallets found in step 2. It checks upstream funding,
   transfers to known creators, direct transfers between funding wallets, and
   shared counterparties.

5. `src/analyze_victim_approvals.py`
   Scans BEP20 `Approval` logs where the spender is a known phishing contract or
   a same-creator candidate deployment. It writes victim-, token-, spender-, and
   daily-level statistics to `data/victim_analysis/`.

   ```bash
   .venv/bin/python src/analyze_victim_approvals.py
   ```

   Add `--known-only` to scan only the two confirmed contracts, or
   `--trace-transfers` to also scan candidate token outflows after approval. The
   transfer scan is heavier because it queries `Transfer` logs for approved
   victim/token pairs. This global approval scan requires either a strong indexed
   RPC or a responsive Etherscan logs API.

6. `src/analyze_victim_outflows_from_receipts.py`
   Fetches transactions involving the phishing contracts and parses their
   receipts for BEP20 `Transfer` events. This is a lighter victim-side analysis
   path for observed token outflows from victims.

   ```bash
   .venv/bin/python src/analyze_victim_outflows_from_receipts.py --known-only
   ```

   Outputs are written to `data/victim_receipt_analysis/`.

## Files

- `src/etherscan_client.py`
  Shared Etherscan/BscScan v2 API helper. It loads `BSCSCAN_API_KEY`, sets
  `chainid=56`, and rate-limits requests.

- `src/get_creator.py`
  Starting point for the case. It hard-codes the two known phishing contract
  addresses and calls `contract/getcontractcreation` to resolve creator wallets
  and deployment transaction hashes.

- `src/find_creator_funding_before_deploy.py`
  Gas funding attribution script. It identifies the last successful incoming
  BNB transfer to each creator before the known phishing contract deployment.

- `src/check_creator_batch_deployments.py`
  Batch deployment analysis. It proves whether the creator wallets deployed many
  contracts and prints candidate contracts for later victim/approve analysis.

- `src/check_funding_wallet_links.py`
  Funding-wallet relationship analysis. It links the first-layer gas funding
  wallets by upstream funding, direct transfers, and common counterparties.

- `src/analyze_victim_approvals.py`
  Victim-side approval analysis. It uses Etherscan only to discover creators and
  same-creator candidate deployments, then uses `BSC_RPC_URL` for log scanning.
  Outputs include `approvals.csv`, `victim_summary.csv`, `token_summary.csv`,
  `spender_summary.csv`, `daily_summary.csv`, and optionally
  `candidate_transfer_outflows.csv`.

- `src/analyze_victim_outflows_from_receipts.py`
  Receipt-based victim outflow analysis. It uses Etherscan transaction lists and
  RPC transaction receipts to produce `spender_transactions.csv`,
  `observed_token_transfers.csv`, `victim_outflow_summary.csv`,
  `token_outflow_summary.csv`, `spender_outflow_summary.csv`, and
  `daily_outflow_summary.csv`.

## API Limits

`src/etherscan_client.py` enforces a default 0.4 second minimum interval between
uncached Etherscan requests and a default daily budget of 80,000 uncached
requests. Responses are cached under `.cache/etherscan/`, so repeated local runs
do not spend the request budget again.

These defaults can be overridden with environment variables:

```bash
ETHERSCAN_MIN_INTERVAL=0.4
ETHERSCAN_DAILY_REQUEST_LIMIT=80000
ETHERSCAN_CACHE_DIR=.cache/etherscan
```

## Current Findings

- The two known addresses are confirmed contracts.
- Contract creators are confirmed.
- First-layer gas funding wallets are identified.
- Both creators show batch contract deployment behavior.
- The two gas funding wallets have strong links, including direct transfers and
  shared counterparties.

## Next Steps

- Trace stolen fund flow after approvals/drain transactions.
