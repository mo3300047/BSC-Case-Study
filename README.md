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

5. `src/analyze_victim_outflows_from_receipts.py`
   Fetches transactions involving the phishing contracts and parses their
   receipts for BEP20 `Transfer` events. This is the current victim-side
   analysis path for observed token outflows from victims.

   ```bash
   .venv/bin/python src/analyze_victim_outflows_from_receipts.py --known-only
   ```

   Outputs are written to `data/victim_receipt_analysis/`.

6. `src/normalize_victim_outflow_amounts.py`
   Reads the observed token transfers, fetches token metadata from RPC, converts
   raw transfer values into token amounts, and writes amount summaries.

   ```bash
   .venv/bin/python src/normalize_victim_outflow_amounts.py
   ```

   Outputs are written to `data/victim_amount_analysis/`.

7. `src/qa_victim_outflows.py`
   Validates normalized outflow rows against cached transaction receipts and
   creates samples for manual review.

   ```bash
   .venv/bin/python src/qa_victim_outflows.py
   ```

   Outputs are written to `data/victim_qa/`.

8. `src/analyze_outflow_receivers.py`
   Aggregates normalized victim outflows by receiving address to show collection
   patterns and concentration.

   ```bash
   .venv/bin/python src/analyze_outflow_receivers.py
   ```

   Outputs are written to `data/receiver_analysis/`.

9. `src/trace_receiver_downstream.py`
   Fetches token transfers for the receiver addresses and aggregates downstream
   recipients.

   ```bash
   .venv/bin/python src/trace_receiver_downstream.py
   ```

   Outputs are written to `data/receiver_downstream/`.

10. `src/trace_downstream_next_hop.py`
    Traces the next hop from the largest downstream recipients to see whether
    funds continue moving or consolidate again.

    ```bash
    .venv/bin/python src/trace_downstream_next_hop.py
    ```

    Outputs are written to `data/downstream_next_hop/`.

11. `src/analyze_creator_candidate_contracts.py`
    Profiles all contracts deployed by the two known phishing creators. It
    compares bytecode, reads source metadata, scans receipts for victim-like
    token outflows, and checks overlap with known receiver infrastructure.

    ```bash
    .venv/bin/python src/analyze_creator_candidate_contracts.py
    ```

    Outputs are written to `data/candidate_contract_analysis/`.

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

- `src/analyze_victim_outflows_from_receipts.py`
  Receipt-based victim outflow analysis. It uses Etherscan transaction lists and
  RPC transaction receipts to produce `spender_transactions.csv`,
  `observed_token_transfers.csv`, `victim_outflow_summary.csv`,
  `token_outflow_summary.csv`, `spender_outflow_summary.csv`, and
  `daily_outflow_summary.csv`.

- `src/normalize_victim_outflow_amounts.py`
  Amount normalization for victim outflows. It produces
  `normalized_victim_outflows.csv`, `overall_summary.csv`,
  `token_amount_summary.csv`, `victim_amount_summary.csv`,
  `spender_amount_summary.csv`, and `daily_amount_summary.csv`.

- `src/qa_victim_outflows.py`
  QA validation for victim outflows. It produces `validated_outflows.csv`,
  `qa_overall_summary.csv`, and `qa_sampled_outflows.csv`.

- `src/analyze_outflow_receivers.py`
  Receiver-side aggregation for victim outflows. It produces
  `receiver_overall_summary.csv`, `receiver_summary.csv`,
  `receiver_token_summary.csv`, `daily_receiver_summary.csv`, and
  `top_receiver_outflows.csv`.

- `src/trace_receiver_downstream.py`
  Downstream tracing for receiver addresses. It produces
  `downstream_overall_summary.csv`, `receiver_flow_summary.csv`,
  `downstream_recipient_summary.csv`,
  `downstream_recipient_all_tokens_summary.csv`, and
  `receiver_token_transfers.csv`.

- `src/trace_downstream_next_hop.py`
  Next-hop tracing for the largest downstream recipients. It produces
  `next_hop_overall_summary.csv`, `target_downstream_flow_summary.csv`,
  `next_hop_recipient_summary.csv`, and `next_hop_token_transfers.csv`.

- `src/analyze_creator_candidate_contracts.py`
  Candidate contract profiling for same-creator deployments. It produces
  `candidate_contract_catalog.csv`, `candidate_contract_transfers.csv`, and
  `risk_summary.csv`.

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
- The two confirmed phishing contracts show 1,632 candidate victim token
  outflows across 458 victim addresses, totaling about 5.66M USDC/USDT.
- Receipt QA found exact matching `Transfer` logs for all 1,632 normalized
  outflows, with 0 failed rows and 0 rows requiring review.
- The 5.66M USDC/USDT flowed into only 7 receiver addresses. The top receiver
  collected about 3.06M, and the top 3 receivers collected about 99.4% of the
  observed victim outflow amount.
- Those 7 receivers subsequently sent about 5.94M USDC/USDT to 108 downstream
  recipients. The downstream total is higher than the observed victim outflow
  because the receiver addresses also had non-victim incoming funds in the same
  tokens.
- The largest downstream recipients also moved funds onward. The traced top
  targets sent about 6.28M USDC/USDT to 93 next-hop recipients, with the largest
  next-hop address receiving about 1.50M USDC.
- The two creators deployed 67 contracts in total, including 2 confirmed
  phishing contracts and 65 same-creator candidates. Automated profiling flagged
  1 high-confidence phishing contract, 22 suspicious contracts, 11 low-signal
  active contracts, and 31 inactive candidates.

## Next Steps

- Produce a simple victim-loss report from the existing CSV summaries.
- Deep-dive the flagged candidate contracts, especially bytecode matches and
  contracts with stablecoin outflows to known receiver infrastructure.
