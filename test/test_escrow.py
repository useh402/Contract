import sys, pathlib
import pytest
from eth_tester import EthereumTester
from eth_tester.exceptions import TransactionFailed
from web3 import Web3, EthereumTesterProvider
from web3.exceptions import ContractCustomError, ContractLogicError

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "script"))
from build import compile_all

BUILT = compile_all()
REVERT = (TransactionFailed, ContractCustomError, ContractLogicError)
U = 10**6
TID = lambda s: Web3.keccak(text=s)


@pytest.fixture
def env():
    tester = EthereumTester()
    w3 = Web3(EthereumTesterProvider(tester))
    owner, operator, treasury, payer, w1, w2, stranger = w3.eth.accounts[:7]
    def deploy(name, *args, sender=owner):
        c = w3.eth.contract(abi=BUILT[name]["abi"], bytecode=BUILT[name]["bytecode"])
        rc = w3.eth.wait_for_transaction_receipt(c.constructor(*args).transact({"from": sender}))
        return w3.eth.contract(address=rc.contractAddress, abi=BUILT[name]["abi"])
    usd = deploy("TestUSD", sender=payer)
    esc = deploy("TaskEscrow", usd.address, owner, operator, treasury, 1000 * U, 100 * U)
    return dict(w3=w3, tester=tester, usd=usd, esc=esc, owner=owner, operator=operator, treasury=treasury, payer=payer, w1=w1, w2=w2, stranger=stranger)


def fund(e, amount):
    e["usd"].functions.transfer(e["esc"].address, amount).transact({"from": e["payer"]})


def bal(e, who):
    return e["usd"].functions.balanceOf(who).call()


def test_open_settle_pays_workers_fee_and_refunds_rest(env):
    e = env; esc = e["esc"]
    fund(e, 18 * U)
    esc.functions.open(TID("t1"), e["payer"], 18 * U).transact({"from": e["operator"]})
    assert esc.functions.allocated().call() == 18 * U
    before = bal(e, e["payer"])
    rc = e["w3"].eth.wait_for_transaction_receipt(esc.functions.settle(TID("t1"), [e["w1"], e["w2"]], [5 * U, 5 * U], 2 * U).transact({"from": e["operator"]}))
    assert bal(e, e["w1"]) == 5 * U and bal(e, e["w2"]) == 5 * U
    assert bal(e, e["payer"]) - before == 6 * U  # the third slot was not earned
    assert esc.functions.feesAccrued().call() == 2 * U and esc.functions.allocated().call() == 0
    ev = esc.events.TaskSettled().process_receipt(rc)[0]["args"]
    assert (ev["paidToWorkers"], ev["fee"], ev["refunded"]) == (10 * U, 2 * U, 6 * U)
    esc.functions.withdrawFees().transact({"from": e["stranger"]})  # anyone may trigger; it only ever goes to the treasury
    assert bal(e, e["treasury"]) == 2 * U and bal(e, esc.address) == 0
    with pytest.raises(REVERT):
        esc.functions.settle(TID("t1"), [e["w1"]], [1], 0).transact({"from": e["operator"]})  # closed


def test_cannot_open_unfunded_or_twice_or_by_stranger(env):
    e = env; esc = e["esc"]
    with pytest.raises(REVERT):
        esc.functions.open(TID("a"), e["payer"], 5 * U).transact({"from": e["operator"]})  # no money in the contract
    fund(e, 5 * U)
    with pytest.raises(REVERT):
        esc.functions.open(TID("a"), e["payer"], 5 * U).transact({"from": e["stranger"]})
    esc.functions.open(TID("a"), e["payer"], 5 * U).transact({"from": e["operator"]})
    with pytest.raises(REVERT):
        esc.functions.open(TID("a"), e["payer"], 5 * U).transact({"from": e["operator"]})
    with pytest.raises(REVERT):
        esc.functions.open(TID("b"), e["payer"], 1).transact({"from": e["operator"]})  # the same money cannot back two tasks
    fund(e, 200 * U)
    with pytest.raises(REVERT):
        esc.functions.open(TID("c"), e["payer"], 101 * U).transact({"from": e["operator"]})  # over the per-task cap


def test_settle_cannot_overpay_or_take_a_big_fee(env):
    e = env; esc = e["esc"]
    fund(e, 20 * U)
    esc.functions.open(TID("x"), e["payer"], 10 * U).transact({"from": e["operator"]})
    esc.functions.open(TID("y"), e["payer"], 10 * U).transact({"from": e["operator"]})
    with pytest.raises(REVERT):
        esc.functions.settle(TID("x"), [e["w1"]], [11 * U], 0).transact({"from": e["operator"]})  # would dip into task y
    with pytest.raises(REVERT):
        esc.functions.settle(TID("x"), [e["w1"]], [6 * U], 4 * U).transact({"from": e["operator"]})  # fee over 30%
    with pytest.raises(REVERT):
        esc.functions.settle(TID("x"), [e["w1"]], [1 * U], 0).transact({"from": e["stranger"]})
    esc.functions.settle(TID("x"), [e["w1"]], [7 * U], 3 * U).transact({"from": e["operator"]})
    assert esc.functions.allocated().call() == 10 * U  # task y untouched


def test_refund_rules(env):
    e = env; esc = e["esc"]
    fund(e, 10 * U)
    esc.functions.open(TID("r"), e["payer"], 10 * U).transact({"from": e["operator"]})
    with pytest.raises(REVERT):
        esc.functions.refund(TID("r")).transact({"from": e["stranger"]})
    with pytest.raises(REVERT):
        esc.functions.refund(TID("r")).transact({"from": e["payer"]})  # too early for the payer
    e["tester"].time_travel(e["w3"].eth.get_block("latest")["timestamp"] + 7 * 86400 + 5)
    esc.functions.setPaused(True).transact({"from": e["operator"]})
    before = bal(e, e["payer"])
    esc.functions.refund(TID("r")).transact({"from": e["payer"]})  # even while paused, with the operator gone
    assert bal(e, e["payer"]) - before == 10 * U
    with pytest.raises(REVERT):
        esc.functions.refund(TID("r")).transact({"from": e["operator"]})


def test_daily_limit_pause_and_roles(env):
    e = env; esc = e["esc"]
    esc.functions.setLimits(15 * U, 100 * U).transact({"from": e["owner"]})
    fund(e, 40 * U)
    for name in ("d1", "d2"):
        esc.functions.open(TID(name), e["payer"], 20 * U).transact({"from": e["operator"]})
    esc.functions.settle(TID("d1"), [e["w1"]], [10 * U], 0).transact({"from": e["operator"]})
    with pytest.raises(REVERT):
        esc.functions.settle(TID("d2"), [e["w1"]], [10 * U], 0).transact({"from": e["operator"]})  # over the day's limit
    e["tester"].time_travel(e["w3"].eth.get_block("latest")["timestamp"] + 86400 + 5)
    esc.functions.setPaused(True).transact({"from": e["operator"]})
    with pytest.raises(REVERT):
        esc.functions.settle(TID("d2"), [e["w1"]], [10 * U], 0).transact({"from": e["operator"]})
    with pytest.raises(REVERT):
        esc.functions.setPaused(False).transact({"from": e["operator"]})  # only the owner unpauses
    esc.functions.setPaused(False).transact({"from": e["owner"]})
    esc.functions.settle(TID("d2"), [e["w1"]], [10 * U], 0).transact({"from": e["operator"]})
    with pytest.raises(REVERT):
        esc.functions.setOperator(e["stranger"]).transact({"from": e["operator"]})
    esc.functions.transferOwnership(e["stranger"]).transact({"from": e["owner"]})
    assert esc.functions.owner().call() == e["owner"]
    esc.functions.acceptOwnership().transact({"from": e["stranger"]})
    assert esc.functions.owner().call() == e["stranger"]


def test_surplus_only_and_only_to_treasury(env):
    e = env; esc = e["esc"]
    fund(e, 10 * U)
    esc.functions.open(TID("s"), e["payer"], 10 * U).transact({"from": e["operator"]})
    fund(e, 3 * U)  # a stray transfer nobody opened
    assert esc.functions.unallocated().call() == 3 * U
    with pytest.raises(REVERT):
        esc.functions.sweepSurplus().transact({"from": e["operator"]})
    esc.functions.sweepSurplus().transact({"from": e["owner"]})
    assert bal(e, e["treasury"]) == 3 * U and bal(e, esc.address) == 10 * U  # task money stays
