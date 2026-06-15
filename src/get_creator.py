from etherscan_client import etherscan_get

contracts = [
    "0xCf0e498f4b33b2581bfd5091BB3E75283E57F488",
    "0x4f8817c31c71f0666e27c02621cb6e2a6ce1f864",
]

data = etherscan_get({
    "module": "contract",
    "action": "getcontractcreation",
    "contractaddresses": ",".join(contracts),
})

for item in data["result"]:
    print("-" * 60)
    print("Contract:", item["contractAddress"])
    print("Creator:", item["contractCreator"])
    print("Tx:", item["txHash"])


# source .venv/bin/activate