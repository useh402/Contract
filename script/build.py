"""Compile TaskEscrow with solc 0.8.26 (optimizer on) and write the ABI and bytecode to out/."""
import json
import pathlib

import solcx

ROOT = pathlib.Path(__file__).resolve().parents[1]
VERSION = "0.8.26"


def compile_all() -> dict:
    solcx.install_solc(VERSION)
    sources = {"TaskEscrow.sol": {"content": (ROOT / "src/TaskEscrow.sol").read_text()}, "TestUSD.sol": {"content": (ROOT / "test/TestUSD.sol").read_text()}}
    out = solcx.compile_standard(
        {"language": "Solidity", "sources": sources, "settings": {"optimizer": {"enabled": True, "runs": 200}, "evmVersion": "paris", "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object"]}}}},
        solc_version=VERSION,
    )
    return {name: {"abi": c["abi"], "bytecode": "0x" + c["evm"]["bytecode"]["object"]} for f in out["contracts"].values() for name, c in f.items()}


if __name__ == "__main__":
    built = compile_all()
    (ROOT / "out").mkdir(exist_ok=True)
    (ROOT / "out/TaskEscrow.json").write_text(json.dumps(built["TaskEscrow"], indent=1))
    print("TaskEscrow bytecode bytes:", len(built["TaskEscrow"]["bytecode"]) // 2 - 1)
