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

## Current Findings

- The two known addresses are confirmed contracts.
- Contract creators are confirmed.
- First-layer gas funding wallets are identified.
- Both creators show batch contract deployment behavior.
- The two gas funding wallets have strong links, including direct transfers and
  shared counterparties.

## Next Steps

- Analyze victim `approve` activity against the known and candidate contracts.
- Trace stolen fund flow after approvals/drain transactions.
