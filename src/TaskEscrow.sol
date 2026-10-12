// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// @title HumanAPI TaskEscrow
/// @notice Holds the stablecoin paid for each task and settles it on chain: workers are paid, the service
///         fee is set aside, and whatever was not earned goes back to the payer. One record per task.
/// @dev    Whether work was done is decided off chain (people agreeing, proof accepted, disputes). This
///         contract cannot check that. What it guarantees is the money: a task can never pay out more than
///         was put in for it, a refund can only go to the payer recorded for that task, the fee can only go
///         to the treasury, and there is no function that moves task money anywhere else.
interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function transfer(address to, uint256 value) external returns (bool);
}

contract TaskEscrow {
    enum Status { None, Open, Settled, Refunded }

    struct Task {
        address payer;      // who is refunded
        uint96 amount;      // total held for the task (rewards + fee)
        uint40 openedAt;
        Status status;
    }

    IERC20 public immutable token;      // USDG
    address public owner;               // can change roles, limits, pause; cannot touch task money
    address public pendingOwner;
    address public operator;            // the HumanAPI backend: opens and settles tasks
    address public treasury;            // receives the service fee and unallocated surplus
    bool public paused;

    uint256 public allocated;           // sum held for open tasks
    uint256 public feesAccrued;         // fee set aside, waiting to be withdrawn to the treasury
    uint256 public dailyLimit;          // most that may leave to workers per UTC day
    uint256 public spentToday;
    uint256 public day;
    uint256 public maxTaskAmount;       // most a single task may hold
    uint256 public constant MAX_FEE_BPS = 3000; // the fee can never exceed 30% of a task
    uint256 public constant REFUND_AFTER = 7 days; // after this, the payer can reclaim an unsettled task themselves

    mapping(bytes32 => Task) public tasks;
    uint256 private locked = 1;

    event TaskOpened(bytes32 indexed taskId, address indexed payer, uint256 amount);
    event WorkerPaid(bytes32 indexed taskId, address indexed worker, uint256 amount);
    event TaskSettled(bytes32 indexed taskId, uint256 paidToWorkers, uint256 fee, uint256 refunded);
    event TaskRefunded(bytes32 indexed taskId, address indexed payer, uint256 amount);
    event FeesWithdrawn(address indexed treasury, uint256 amount);
    event SurplusSwept(address indexed treasury, uint256 amount);
    event OperatorChanged(address indexed operator);
    event TreasuryChanged(address indexed treasury);
    event LimitsChanged(uint256 dailyLimit, uint256 maxTaskAmount);
    event Paused(bool paused);
    event OwnershipTransferStarted(address indexed pendingOwner);
    event OwnershipTransferred(address indexed owner);

    error NotOwner();
    error NotOperator();
    error IsPaused();
    error BadInput();
    error TaskExists();
    error TaskNotOpen();
    error NotFunded();
    error OverTask();
    error FeeTooHigh();
    error OverDailyLimit();
    error TooEarly();
    error TransferFailed();
    error Reentrancy();

    modifier onlyOwner() { if (msg.sender != owner) revert NotOwner(); _; }
    modifier onlyOperator() { if (msg.sender != operator) revert NotOperator(); _; }
    modifier whenNotPaused() { if (paused) revert IsPaused(); _; }
    modifier nonReentrant() { if (locked != 1) revert Reentrancy(); locked = 2; _; locked = 1; }

    constructor(address token_, address owner_, address operator_, address treasury_, uint256 dailyLimit_, uint256 maxTaskAmount_) {
        if (token_ == address(0) || owner_ == address(0) || operator_ == address(0) || treasury_ == address(0)) revert BadInput();
        token = IERC20(token_);
        owner = owner_;
        operator = operator_;
        treasury = treasury_;
        dailyLimit = dailyLimit_;
        maxTaskAmount = maxTaskAmount_;
    }

    // ---------------------------------------------------------------- tasks

    /// @notice Record a task whose money is already in this contract (sent by the payer's transfer).
    function open(bytes32 taskId, address payer, uint256 amount) external onlyOperator whenNotPaused {
        if (payer == address(0) || amount == 0 || amount > maxTaskAmount || amount > type(uint96).max) revert BadInput();
        if (tasks[taskId].status != Status.None) revert TaskExists();
        if (allocated + feesAccrued + amount > token.balanceOf(address(this))) revert NotFunded();
        allocated += amount;
        tasks[taskId] = Task(payer, uint96(amount), uint40(block.timestamp), Status.Open);
        emit TaskOpened(taskId, payer, amount);
    }

    /// @notice Close a task: pay the workers, set the fee aside, send the rest back to the payer.
    function settle(bytes32 taskId, address[] calldata workers, uint256[] calldata amounts, uint256 fee) external onlyOperator whenNotPaused nonReentrant {
        Task storage t = tasks[taskId];
        if (t.status != Status.Open) revert TaskNotOpen();
        if (workers.length != amounts.length || workers.length > 20) revert BadInput();
        uint256 held = t.amount;
        if (fee * 10_000 > held * MAX_FEE_BPS) revert FeeTooHigh();
        uint256 toWorkers;
        for (uint256 i; i < amounts.length; ++i) {
            if (workers[i] == address(0) || workers[i] == address(this)) revert BadInput();
            toWorkers += amounts[i];
        }
        if (toWorkers + fee > held) revert OverTask();
        _spend(toWorkers);

        t.status = Status.Settled;
        allocated -= held;
        feesAccrued += fee;
        uint256 back = held - toWorkers - fee;

        for (uint256 i; i < workers.length; ++i) {
            if (amounts[i] == 0) continue;
            _send(workers[i], amounts[i]);
            emit WorkerPaid(taskId, workers[i], amounts[i]);
        }
        if (back > 0) _send(t.payer, back);
        emit TaskSettled(taskId, toWorkers, fee, back);
    }

    /// @notice Send everything held for a task back to its payer (expired, cancelled, refused).
    ///         The operator may do this at any time; the payer may do it themselves after REFUND_AFTER,
    ///         so money can never be stranded if the operator disappears. Works while paused.
    function refund(bytes32 taskId) external nonReentrant {
        Task storage t = tasks[taskId];
        if (t.status != Status.Open) revert TaskNotOpen();
        if (msg.sender != operator) {
            if (msg.sender != t.payer) revert NotOperator();
            if (block.timestamp < uint256(t.openedAt) + REFUND_AFTER) revert TooEarly();
        }
        uint256 held = t.amount;
        t.status = Status.Refunded;
        allocated -= held;
        _send(t.payer, held);
        emit TaskRefunded(taskId, t.payer, held);
    }

    // ---------------------------------------------------------------- treasury

    function withdrawFees() external nonReentrant {
        uint256 amount = feesAccrued;
        feesAccrued = 0;
        _send(treasury, amount);
        emit FeesWithdrawn(treasury, amount);
    }

    /// @notice Tokens in the contract that belong to no task and are not fees (for example a transfer that
    ///         was never opened as a task). Only this surplus can be moved, and only to the treasury.
    function sweepSurplus() external onlyOwner nonReentrant {
        uint256 surplus = token.balanceOf(address(this)) - allocated - feesAccrued;
        _send(treasury, surplus);
        emit SurplusSwept(treasury, surplus);
    }

    function unallocated() external view returns (uint256) {
        return token.balanceOf(address(this)) - allocated - feesAccrued;
    }

    // ---------------------------------------------------------------- admin

    function setOperator(address operator_) external onlyOwner { if (operator_ == address(0)) revert BadInput(); operator = operator_; emit OperatorChanged(operator_); }
    function setTreasury(address treasury_) external onlyOwner { if (treasury_ == address(0)) revert BadInput(); treasury = treasury_; emit TreasuryChanged(treasury_); }
    function setLimits(uint256 dailyLimit_, uint256 maxTaskAmount_) external onlyOwner { dailyLimit = dailyLimit_; maxTaskAmount = maxTaskAmount_; emit LimitsChanged(dailyLimit_, maxTaskAmount_); }
    function setPaused(bool paused_) external { if (msg.sender != owner && msg.sender != operator) revert NotOwner(); if (!paused_ && msg.sender != owner) revert NotOwner(); paused = paused_; emit Paused(paused_); }
    function transferOwnership(address next) external onlyOwner { pendingOwner = next; emit OwnershipTransferStarted(next); }
    function acceptOwnership() external { if (msg.sender != pendingOwner) revert NotOwner(); owner = pendingOwner; pendingOwner = address(0); emit OwnershipTransferred(owner); }

    // ---------------------------------------------------------------- internals

    function _spend(uint256 amount) private {
        uint256 today = block.timestamp / 1 days;
        if (today != day) { day = today; spentToday = 0; }
        spentToday += amount;
        if (spentToday > dailyLimit) revert OverDailyLimit();
    }

    function _send(address to, uint256 amount) private {
        if (amount == 0) return;
        (bool ok, bytes memory data) = address(token).call(abi.encodeCall(IERC20.transfer, (to, amount)));
        if (!ok || (data.length != 0 && !abi.decode(data, (bool)))) revert TransferFailed();
    }
}
