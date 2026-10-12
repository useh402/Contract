# TaskEscrow

Holds the stablecoin paid for each task and settles it on chain.

| | |
| --- | --- |
| Network | Robinhood Chain mainnet (4663) |
| Address | `0x530C2beDB048a658cB9fFD074eB7A75D68bfBd90` |
| Deployed in | `0xed43df80ea4b5365dbe4b1e7bab4450bc649955f57cbacb229c76c85ef499257`, block 84343746, 2026-10-09 |
| Token | USDG `0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168` |
| Owner, operator, treasury | `0xAd576D84708597616510186DAAc39D4aEF559a14` |
| Limits (set 2026-10-11, tx 0x281170ce…) | 300 USDG paid out per day, 60 USDG per task |
| Old limits | 2,000 USDG to workers per day, 200 USDG per task |
| Compiler | solc 0.8.26, optimizer 200 runs, EVM paris |

## What it does

- `open(taskId, payer, amount)`: the operator records a task whose money is already in the contract. The same money can never back two tasks.
- `settle(taskId, workers, amounts, fee)`: pays the workers, sets the fee aside (at most 30% of the task), returns the rest to the payer, closes the task.
- `refund(taskId)`: everything back to the payer. The operator can at any time; the payer can themselves after 7 days, even while paused.
- `withdrawFees()`: fees go to the treasury, nowhere else. `sweepSurplus()`: only tokens that belong to no task.
- Owner: operator, treasury, limits, unpause. Operator or owner can pause.

It cannot judge whether work was done. That stays off chain. It guarantees where the money can go.

## Status

- Wired into the API (2026-10-09): `api/humanapi_api/escrow_chain.py`. Payments land in the contract, each task is opened there, and completion calls `settle` or `refund`. The task view shows the open and close transactions. Bonus payments (quality checks, report credits, dispute refunds) and the Solana rail still use direct transfers.
- Proven on mainnet with a one-cent round trip on 2026-10-09: fund `0xde72da63…`, open `0x12c21c9d…` and `0x51a2e6f4…`, settle `0x4f485839…`, refund `0x52398d08…`. 0.002 USDG of fee is waiting in the contract.
- Not audited. Six contract tests on a local chain; five API tests against a stand-in that enforces the same rules.
- Source verified on Blockscout on 2026-10-11: https://robinhoodchain.blockscout.com/address/0x530C2beDB048a658cB9fFD074eB7A75D68bfBd90?tab=contract (solc 0.8.26, optimizer 200, EVM paris, MIT).
- Before real money: move `owner` and `treasury` to a wallet whose key has never been shared.

## How rewards flow since 2026-10-11

Platform tasks are listed for free. Taking one makes the service lock its reward in the contract (`open`). When the answer is accepted the reward waits for the worker's Claim; the claim makes the service call `settle`, and the contract pays the worker. Fees accrue in the contract for the treasury. Funding the pool is a plain USDG transfer to the contract address; the service reads the contract's free balance once a minute.

## Switching the API to the contract

```
HA_ENV=mainnet
HA_ROBINHOOD_NETWORK=eip155:4663
HA_ROBINHOOD_ESCROW_ADDRESS=0x530C2beDB048a658cB9fFD074eB7A75D68bfBd90
HA_ESCROW_CONTRACT=0x530C2beDB048a658cB9fFD074eB7A75D68bfBd90
HA_ESCROW_OPERATOR_PRIVATE_KEY=<the operator key>
HA_ESCROW_HOUSE_PAYER=<treasury address; optional>
```

The facilitator must run on mainnet too (`HF_ENV=mainnet`, `HF_ROBINHOOD_NETWORK=eip155:4663`). Platform-posted tasks are funded from the operator wallet's USDG, so keep the pool there. `GET /v1/admin/escrow` shows the contract's balance and task counts.
