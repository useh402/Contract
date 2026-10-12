"""Deploy TaskEscrow. Reads DEPLOYER_PRIVATE_KEY from contracts/.env. Usage: deploy.py [--send]
Without --send it only shows the wallet, the gas estimate and the constructor arguments."""
import json, pathlib, sys, time
from eth_account import Account
from web3 import Web3

ROOT = pathlib.Path(__file__).resolve().parents[1]
RPC = "https://rpc.mainnet.chain.robinhood.com"
USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
DAILY_LIMIT = 2_000 * 10**6   # most that may leave to workers per day
MAX_TASK = 200 * 10**6        # most one task may hold

key = next(l.split("=", 1)[1].strip() for l in (ROOT / ".env").read_text().splitlines() if l.startswith("DEPLOYER_PRIVATE_KEY="))
acct = Account.from_key(key)
w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 30}))
built = json.loads((ROOT / "out/TaskEscrow.json").read_text())
assert w3.eth.chain_id == 4663, "not Robinhood Chain mainnet"
assert len(w3.eth.get_code(USDG)) > 0, "USDG not found"
bal = w3.eth.get_balance(acct.address)
print("deployer:", acct.address, "| balance:", w3.from_wei(bal, "ether"), "ETH | nonce:", w3.eth.get_transaction_count(acct.address))
c = w3.eth.contract(abi=built["abi"], bytecode=built["bytecode"])
args = (USDG, acct.address, acct.address, acct.address, DAILY_LIMIT, MAX_TASK)
tx = c.constructor(*args).build_transaction({"from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address), "chainId": 4663})
gas = w3.eth.estimate_gas(tx); tx["gas"] = int(gas * 1.2)
fee = tx.get("maxFeePerGas") or tx.get("gasPrice")
print("gas estimate:", gas, "| max cost:", w3.from_wei(tx["gas"] * fee, "ether"), "ETH")
if "--send" not in sys.argv:
    sys.exit(0)
if bal < tx["gas"] * fee:
    sys.exit("not enough ETH for gas")
h = w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
print("sent:", h.hex())
rc = w3.eth.wait_for_transaction_receipt(h, timeout=300)
assert rc["status"] == 1, "deployment reverted"
addr = rc["contractAddress"]
time.sleep(3)
e = w3.eth.contract(address=addr, abi=built["abi"])
info = {"network": "eip155:4663", "address": addr, "transaction": "0x" + h.hex().removeprefix("0x"), "block": rc["blockNumber"], "deployer": acct.address, "token": e.functions.token().call(), "owner": e.functions.owner().call(), "operator": e.functions.operator().call(), "treasury": e.functions.treasury().call(), "dailyLimit": e.functions.dailyLimit().call(), "maxTaskAmount": e.functions.maxTaskAmount().call(), "solc": "0.8.26", "optimizerRuns": 200, "evmVersion": "paris", "constructorArgs": [str(a) for a in args]}
(ROOT / "out/deployment.mainnet.json").write_text(json.dumps(info, indent=1))
print(json.dumps(info, indent=1))
