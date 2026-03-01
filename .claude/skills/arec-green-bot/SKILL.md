---
name: arec-green-bot
description: Arkreen AREC green bot development guide. Use when integrating the Arkreen AREC greening pipeline, calling AREC contracts, or debugging AREC-related code on Polygon mainnet.
---

# AREC Green Bot — Developer Reference

This skill documents the complete experience of integrating the Arkreen AREC auto-greening bot on Polygon mainnet, including contract addresses, ABI pitfalls, protocol details, and hard-won lessons.

---

## 1. Protocol Overview

Arkreen AREC (Aggregated Renewable Energy Certificate) is a green energy certificate protocol on Polygon.

**Full 4-step pipeline:**
```
mintRECRequest  →  (wait for Issuer certification)  →  liquidizeREC  →  convertKWh  →  makeGreenBox
    Step 1                                                  Step 2          Step 3          Step 4
  Mint AREC NFT                                        NFT → ART Token  ART → kWh Token  kWh → Green BTC blocks
```

---

## 2. Contract Addresses (Polygon Mainnet)

```python
ADDR = {
    "AREC":   "0x954585adF9425F66a0a2FD8e10682EB7c4F1f1fD",  # AREC NFT (ERC721, Proxy)
    "ART":    "0x58E4D14ccddD1E993e6368A8c5EAa290C95caFDF",  # ART Token (9 decimals)
    "KWH":    "0x5740A27990d4AaA4FB83044a6C699D435B9BA6F1",  # kWh Token (6 decimals)
    "AKRE":   "0xE9c21De62C5C5d0cEAcCe2762bF655AfDcEB7ab3",  # AKRE Token (EIP-2612 permit)
    "GREEN":  "0x3221F5818A5CF99e09f5BE0E905d8F145935e3E0",  # GreenBTC2S (Proxy)
    "ISSUER": "0xFedD52848Cb44dcDBA95df4cf2BCBD71D58df879",  # Arkreen Issuer
}
```

**Implementation contracts (for ABI reference):**
- AREC NFT impl: `0x982a874197b3a4089f8a94b8179e690622b10611` (ArkreenRECIssuance)
- GreenBTC impl: `0x639f0b82ad034ae8fa2f795d960176c1e4e2cd41` (GreenBTC2S)

---

## 3. Critical ABI — Verified Working

### ⚠️ Major Pitfall: `allRECData` Cannot Be Called

`allRECData` is an **internal storage mapping**, not a public function. Calling it always results in `execution reverted`. The public read function is `getRECDataCore`.

```python
AREC_ABI = [
  # Mint AREC NFT (requires AKRE EIP-2612 permit)
  {"name": "mintRECRequest", "type": "function", "stateMutability": "nonpayable",
   "inputs": [
     {"name": "recRequest", "type": "tuple", "components": [
       {"name": "issuer",    "type": "address"}, {"name": "startTime", "type": "uint32"},
       {"name": "endTime",   "type": "uint32"},  {"name": "amountREC", "type": "uint128"},
       {"name": "cID",       "type": "string"},  {"name": "region",    "type": "string"},
       {"name": "url",       "type": "string"},  {"name": "memo",      "type": "string"}
     ]},
     {"name": "permitToPay", "type": "tuple", "components": [
       {"name": "token",    "type": "address"}, {"name": "value",    "type": "uint256"},
       {"name": "deadline", "type": "uint256"}, {"name": "v",        "type": "uint8"},
       {"name": "r",        "type": "bytes32"}, {"name": "s",        "type": "bytes32"}
     ]}
   ], "outputs": [{"name": "tokenId", "type": "uint256"}]},

  # Liquidize NFT into ART tokens
  {"name": "liquidizeREC", "type": "function", "stateMutability": "nonpayable",
   "inputs": [{"name": "tokenId", "type": "uint256"}], "outputs": []},

  # ✅ Correct query function — NOT allRECData!
  {"name": "getRECDataCore", "type": "function", "stateMutability": "view",
   "inputs": [{"name": "tokenId", "type": "uint256"}],
   "outputs": [
     {"name": "issuer",    "type": "address"},
     {"name": "amountREC", "type": "uint128"},
     {"name": "status",    "type": "uint8"},   # index [2]
     {"name": "idAsset",   "type": "uint16"}
   ]},

  {"name": "balanceOf", "type": "function", "stateMutability": "view",
   "inputs": [{"name": "owner", "type": "address"}],
   "outputs": [{"name": "", "type": "uint256"}]},

  {"name": "tokenOfOwnerByIndex", "type": "function", "stateMutability": "view",
   "inputs": [{"name": "owner", "type": "address"}, {"name": "index", "type": "uint256"}],
   "outputs": [{"name": "", "type": "uint256"}]},

  {"name": "paymentTokenPrice", "type": "function", "stateMutability": "view",
   "inputs": [{"name": "paymentToken", "type": "address"}],
   "outputs": [{"name": "", "type": "uint256"}]},
]
```

### Full RECData Struct (for reference — `getRECData` ABI must include `minter`)

```
struct RECData {
    address issuer;
    string  serialNumber;
    address minter;    // ← this field is missing in most unofficial ABIs — causes decode failure
    uint32  startTime;
    uint32  endTime;
    uint128 amountREC;
    uint8   status;
    string  cID;
    string  region;
    string  url;
    string  memo;
    uint16  idAsset;   // ← at the end
}
```

### RECStatus Enum

```python
# uint8 status values:
STATUS = {
    0: "Pending",    # Minted, awaiting Issuer certification
    1: "Rejected",
    2: "Cancelled",
    3: "Certified",  # ✅ Ready for liquidization
    4: "Retired",
    5: "Liquidized", # Already converted to ART tokens
}
# Read via: arec.functions.getRECDataCore(token_id).call()[2]
```

### kWh Token ABI

```python
KWH_ABI = [
  {"name": "convertKWh", "type": "function", "stateMutability": "nonpayable",
   "inputs": [{"name": "tokenToPay", "type": "address"},
              {"name": "amountPayment", "type": "uint256"}], "outputs": []},
  {"name": "balanceOf", "type": "function", "stateMutability": "view",
   "inputs": [{"name": "account", "type": "address"}],
   "outputs": [{"name": "", "type": "uint256"}]},
  {"name": "approve", "type": "function", "stateMutability": "nonpayable",
   "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
   "outputs": [{"name": "", "type": "bool"}]},
]
```

### GreenBTC2S ABI

```python
GREEN_ABI = [
  {"name": "makeGreenBox", "type": "function", "stateMutability": "nonpayable",
   "inputs": [{"name": "domainID",  "type": "uint256"},
              {"name": "boxSteps",  "type": "uint256"}], "outputs": []},
]
```

---

## 4. Token Units and Conversions

```
ART Token:  9 decimals  →  1 ART (whole) = 1_000_000_000 base units
KWH Token:  6 decimals  →  1 kWh token   = 1_000_000 base units
AKRE Token: 18 decimals →  standard ERC20

Conversion ratios:
  1 ART ≈ 1000 kWh tokens (approximate, based on contract rate)
  1 GreenBox step = 10 kWh tokens (whole) = 10_000_000 kWh base units
  1 step ≈ greens 1 Bitcoin block

⚠️  KWH_PER_STEP must be 10 * (10**6) = 10_000_000 — NOT the raw integer 10!
```

---

## 5. GreenBTC Valid Domain IDs

Domain IDs represent different blockchains/networks for `makeGreenBox(domainID, boxSteps)`:

```
Domain 1:     Bitcoin mainnet — ❌ Fully greenized (All Greenized), cannot use
Domain 2:     ✅ Available
Domain 3:     ✅ Available
Domain 4–10:  ❌ Empty Domain (not opened)
Domain 11–18: ✅ Available
Domain 19–20: ❌ Fully greenized

Recommended default list: [2, 3, 11, 12, 13, 14, 15, 16, 17, 18]
```

**Best practice** — simulate before sending, skip greenized or empty domains:

```python
def find_working_domain(green_contract, domain_ids, wallet, box_steps):
    for did in domain_ids:
        try:
            green_contract.functions.makeGreenBox(did, box_steps).call({"from": wallet})
            return did
        except Exception as e:
            if "All Greenized" in str(e) or "Empty Domain" in str(e):
                continue
            return did  # other errors — try anyway
    raise RuntimeError("No available GreenBTC domain")
```

---

## 6. Step 1 — Mint AREC NFT

### Arkreen OpenAPI (JSON-RPC 2.0)

```
Endpoint: https://openapi.arkreen.com/v1
Method:   POST
Content-Type: application/json

1. Get available power output:
   method: "rec_getOwnerRecDataNew"
   params: [{"owner": "0x<wallet>"}]
   returns: {"totalREOutput": "0x<hex mWh>", "startDate": "...", "endDate": "..."}

2. Get IPFS URL for minting:
   method: "rec_issueOwnerRecNew"
   params: [{
     "owner": "0x...", "issuer": "0xFedD...",
     "startDate": "...", "endDate": "...",
     "totalARECPower":   "0x<hex>",
     "valueApproval":    "0x<hex>",   # hex(amountREC * paymentTokenPrice)
     "deadlineApproval": <unix ts>,
     "signatureApproval":"0x<sig>",   # EIP-2612 v+r+s concatenated, 132 hex chars
     "byPower": false
   }]
   returns: {"ipfs": "bafybei...", "url": "https://...ipfs..."}
```

### EIP-2612 Permit Signature (gasless AKRE approval)

```python
from eth_account import Account

def sign_permit_akre(w3, pk, spender, value, deadline):
    owner = Account.from_key(pk).address
    nonce = akre_contract.functions.nonces(owner).call()
    domain = {
        "name": "AKRE Token", "version": "1",
        "chainId": w3.eth.chain_id,          # 137 for Polygon
        "verifyingContract": ADDR["AKRE"],
    }
    types = {"Permit": [
        {"name": "owner",    "type": "address"},
        {"name": "spender",  "type": "address"},
        {"name": "value",    "type": "uint256"},
        {"name": "nonce",    "type": "uint256"},
        {"name": "deadline", "type": "uint256"},
    ]}
    message = {"owner": owner, "spender": spender,
               "value": value, "nonce": nonce, "deadline": deadline}
    signed = Account.sign_typed_data(pk, domain, types, message)
    # signatureApproval = "0x" + v(1 byte) + r(32 bytes) + s(32 bytes) = 66 bytes = 132 hex chars
    return "0x" + signed.v.to_bytes(1, "big").hex() + signed.r.hex() + signed.s.hex()
```

### Extracting token_id from Receipt (ERC721 Transfer Event)

```python
# Transfer(address indexed from, address indexed to, uint256 indexed tokenId)
# topics layout: [0]=event sig, [1]=from (0x0 for mint), [2]=to, [3]=tokenId
# ⚠️  tokenId is at topics[3], NOT topics[2]!

TRANSFER_SIG = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDR    = "0x" + "0" * 64

token_id = None
for log in receipt.logs:
    t = log.topics
    if len(t) >= 4 and t[0].hex() == TRANSFER_SIG and t[1].hex() == ZERO_ADDR:
        token_id = int(t[3].hex(), 16)  # ← t[3], not t[2]!
        break

# Fallback: scan all topics for a small integer
if token_id is None:
    for log in receipt.logs:
        for t in log.topics[1:]:
            v = int(t.hex(), 16)
            if 0 < v < 10_000_000:
                token_id = v
                break
        if token_id:
            break
```

---

## 7. Sending Transactions (eth_account Version Compatibility)

```python
def send_tx(w3, tx_dict, pk, timeout=180):
    """Sign and send tx. Compatible with eth_account 0.11.x and 0.12.x."""
    signed = Account.sign_transaction(tx_dict, pk)
    # 0.11.x uses rawTransaction (camelCase); 0.12.x uses raw_transaction (snake_case)
    raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
    if receipt.status != 1:
        raise RuntimeError(f"tx reverted: {tx_hash.hex()}")
    return receipt
```

---

## 8. Nonce Race Condition (Two Consecutive Transactions)

When sending two transactions from the same wallet in one function (e.g. approve + action), manage nonces manually:

```python
# ❌ Wrong — second get_transaction_count may return the same nonce
send_tx(w3, approve_tx, pk)
nonce = w3.eth.get_transaction_count(wallet)  # could still be the old nonce!
send_tx(w3, action_tx, pk)

# ✅ Correct — fetch once, increment manually
nonce = w3.eth.get_transaction_count(wallet)
approve_tx = contract.functions.approve(...).build_transaction({"nonce": nonce, ...})
send_tx(w3, approve_tx, pk, timeout=120)   # wait for confirmation

action_tx = contract.functions.action(...).build_transaction({"nonce": nonce + 1, ...})
send_tx(w3, action_tx, pk, timeout=300)
```

---

## 9. Common web3.py Pitfalls

```python
# Polygon PoA middleware (web3 6.x)
from web3.middleware import geth_poa_middleware        # ✅ correct
# from web3.middleware import ExtraDataToPOAMiddleware # ❌ does not exist in 6.x

w3 = Web3(Web3.HTTPProvider(RPC_URL))
w3.middleware_onion.inject(geth_poa_middleware, layer=0)

# Zero-check for power_hex — "0x0", "0x00", "0x0000" are all possible
if not power_hex or int(power_hex, 16) == 0:   # ✅ correct
    return None
# if power_hex in ("0x0", "0x00", "0x"):        # ❌ incomplete
```

---

## 10. Step 2 — Find Certified Tokens

```python
def get_certified_tokens(w3, arec_contract, wallet_addr):
    """Return list of token IDs with status == 3 (Certified), ready for liquidization."""
    bal = arec_contract.functions.balanceOf(wallet_addr).call()
    result = []
    for i in range(bal):
        tid    = arec_contract.functions.tokenOfOwnerByIndex(wallet_addr, i).call()
        status = arec_contract.functions.getRECDataCore(tid).call()[2]  # index 2
        if status == 3:  # Certified
            result.append(tid)
    return result
```

---

## 11. Debugging: Simulate to Get Revert Reason

Before sending a real transaction, use `eth_call` to simulate and surface the revert message:

```python
try:
    contract.functions.makeGreenBox(domain_id, box_steps).call({"from": wallet})
except Exception as e:
    # e contains the revert reason string, e.g.:
    # "execution reverted: GBC2: All Greenized"
    # "execution reverted: GBC2: Empty Domain"
    print(str(e))
```

**KWH balance check:**
```python
kwh_bal = kwh.functions.balanceOf(wallet).call()
print(f"{kwh_bal} base units = {kwh_bal / 1e6:.4f} kWh tokens")
print(f"Can green {kwh_bal // 10_000_000} Bitcoin blocks")
```

---

## 12. arec_config Table Schema (key-value)

```sql
CREATE TABLE arec_config (key TEXT PRIMARY KEY, value TEXT);

-- Common keys
INSERT OR REPLACE INTO arec_config VALUES ('domain_ids',           '[2,3,11,12,13,14,15,16,17,18]');
INSERT OR REPLACE INTO arec_config VALUES ('convert_ratio_min',    '0.3');
INSERT OR REPLACE INTO arec_config VALUES ('convert_ratio_max',    '0.8');
INSERT OR REPLACE INTO arec_config VALUES ('box_steps_range_min',  '1');
INSERT OR REPLACE INTO arec_config VALUES ('box_steps_range_max',  '10');
INSERT OR REPLACE INTO arec_config VALUES ('gas_multiplier',       '1.3');
```

---

## 13. Error Quick-Reference

| Error | Cause | Fix |
|-------|-------|-----|
| `execution reverted` on `allRECData` | Internal mapping, not a public function | Use `getRECDataCore` instead |
| `execution reverted: GBC2: All Greenized` | Domain fully greenized | Use another domain (see §5) |
| `execution reverted: GBC2: Empty Domain` | Domain not opened | Use another domain (see §5) |
| `'SignedTransaction' object has no attribute 'raw_transaction'` | eth_account < 0.12 | Use `hasattr` fallback (see §7) |
| `Transaction not in the chain after N seconds` | Gas or nonce issue | Increase timeout; check nonce race |
| `Insufficent Energy` from Arkreen API | No power data available | Skip mint step, continue pipeline |
| Green step tx `status=0`, `gasUsed=29001` | makeGreenBox reverted (not a gas issue) | Simulate with `eth_call` to get reason |
| kWh balance sufficient but allowance fails | `KWH_PER_STEP` missing `10**6` multiplier | Set `KWH_PER_STEP = 10 * (10**6)` |
| `ImportError: cannot import name 'ExtraDataToPOAMiddleware'` | Wrong middleware name in web3 6.x | Use `geth_poa_middleware` |
