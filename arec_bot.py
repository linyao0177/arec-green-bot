"""
AREC Green Bot — automated Arkreen renewable-energy greening pipeline
====================================================================
Mints AREC certificates from real solar/wind miner output on Polygon, then
runs them through the full Arkreen greening pipeline:

    mintRECRequest  →  (wait for Issuer certification)  →  liquidizeREC
        Step 1                                                Step 2
      AREC NFT                                            NFT → ART token

    →  convertKWh   →  makeGreenBox
         Step 3            Step 4
     ART → kWh token   kWh → greened Bitcoin blocks (GreenBTC)

The canonical reference for every contract address, ABI quirk, unit, and
hard-won pitfall lives in `SKILL.md` (the Claude Code skill shipped with this
repo). This module is the runnable implementation of that reference.

Arkreen OpenAPI (reverse-engineered from arec.arkreen.com):
    https://openapi.arkreen.com/v1   (JSON-RPC 2.0)
      rec_getOwnerRecDataNew   → query a wallet's mintable power output
      rec_issueOwnerRecNew     → pin energy data to IPFS, return CID/URL
"""

import os
import json
import time
import logging

import requests
from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3

# web3 6.x exposes `geth_poa_middleware`; 7.x renamed it to
# `ExtraDataToPOAMiddleware`. Import whichever exists (see SKILL.md §9).
try:
    from web3.middleware import geth_poa_middleware as POA_MIDDLEWARE
except ImportError:  # web3 >= 7.x
    from web3.middleware import ExtraDataToPOAMiddleware as POA_MIDDLEWARE

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("arec_bot")

# ─── Configuration (environment) ─────────────────────────────────────
RPC_URL     = os.getenv("RPC_URL", "https://polygon-rpc.com")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")   # wallet private key (0x...)
WALLET_ADDR = os.getenv("WALLET_ADDR", "")   # wallet address (checksum)

POLL_INTERVAL_SEC  = int(os.getenv("POLL_INTERVAL_SEC", "300"))   # main loop
CERT_WAIT_SEC      = int(os.getenv("CERT_WAIT_SEC", "300"))       # cert poll
CERT_TIMEOUT_HOURS = float(os.getenv("CERT_TIMEOUT_HOURS", "48")) # cert deadline
GAS_MULTIPLIER     = float(os.getenv("GAS_MULTIPLIER", "1.3"))

# GreenBTC domains: 1/19/20 fully greenized, 4-10 not opened. See SKILL.md §5.
GREENBTC_DOMAIN_IDS = json.loads(
    os.getenv("GREENBTC_DOMAIN_IDS", "[2,3,11,12,13,14,15,16,17,18]")
)

# Token units (SKILL.md §4)
KWH_DECIMALS = 6
KWH_PER_STEP = 10 * (10 ** KWH_DECIMALS)   # 1 GreenBox step = 10 kWh tokens
ART_DECIMALS = 9

# ─── Arkreen OpenAPI ─────────────────────────────────────────────────
OPENAPI_URL = "https://openapi.arkreen.com/v1"

# ─── Contract addresses (Polygon mainnet, SKILL.md §2) ───────────────
ADDR = {
    "AREC":  Web3.to_checksum_address("0x954585adF9425F66a0a2FD8e10682EB7c4F1f1fD"),
    "ART":   Web3.to_checksum_address("0x58E4D14ccddD1E993e6368A8c5EAa290C95caFDF"),
    "KWH":   Web3.to_checksum_address("0x5740A27990d4AaA4FB83044a6C699D435B9BA6F1"),
    "AKRE":  Web3.to_checksum_address("0xE9c21De62C5C5d0cEAcCe2762bF655AfDcEB7ab3"),
    "GREEN": Web3.to_checksum_address("0x3221F5818A5CF99e09f5BE0E905d8F145935e3E0"),
}
ARKREEN_ISSUER = Web3.to_checksum_address("0xFedD52848Cb44dcDBA95df4cf2BCBD71D58df879")

# REC status enum (uint8), SKILL.md §3
REC_STATUS = {0: "Pending", 1: "Rejected", 2: "Cancelled",
              3: "Certified", 4: "Retired", 5: "Liquidized"}

# ERC721 Transfer(address,address,uint256) topic signature, SKILL.md §6
TRANSFER_SIG = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_TOPIC   = "0x" + "0" * 64

# ─── ABIs (minimal, verified — SKILL.md §3) ──────────────────────────
AREC_ABI = json.loads("""[
  {"name":"mintRECRequest","type":"function","stateMutability":"nonpayable",
   "inputs":[
     {"name":"recRequest","type":"tuple","components":[
       {"name":"issuer","type":"address"},{"name":"startTime","type":"uint32"},
       {"name":"endTime","type":"uint32"},{"name":"amountREC","type":"uint128"},
       {"name":"cID","type":"string"},{"name":"region","type":"string"},
       {"name":"url","type":"string"},{"name":"memo","type":"string"}
     ]},
     {"name":"permitToPay","type":"tuple","components":[
       {"name":"token","type":"address"},{"name":"value","type":"uint256"},
       {"name":"deadline","type":"uint256"},{"name":"v","type":"uint8"},
       {"name":"r","type":"bytes32"},{"name":"s","type":"bytes32"}
     ]}
   ],"outputs":[{"name":"tokenId","type":"uint256"}]},
  {"name":"liquidizeREC","type":"function","stateMutability":"nonpayable",
   "inputs":[{"name":"tokenId","type":"uint256"}],"outputs":[]},
  {"name":"getRECDataCore","type":"function","stateMutability":"view",
   "inputs":[{"name":"tokenId","type":"uint256"}],
   "outputs":[
     {"name":"issuer","type":"address"},{"name":"amountREC","type":"uint128"},
     {"name":"status","type":"uint8"},{"name":"idAsset","type":"uint16"}
   ]},
  {"name":"balanceOf","type":"function","stateMutability":"view",
   "inputs":[{"name":"owner","type":"address"}],"outputs":[{"name":"","type":"uint256"}]},
  {"name":"tokenOfOwnerByIndex","type":"function","stateMutability":"view",
   "inputs":[{"name":"owner","type":"address"},{"name":"index","type":"uint256"}],
   "outputs":[{"name":"","type":"uint256"}]},
  {"name":"paymentTokenPrice","type":"function","stateMutability":"view",
   "inputs":[{"name":"paymentToken","type":"address"}],
   "outputs":[{"name":"","type":"uint256"}]}
]""")

ERC20_ABI = json.loads("""[
  {"name":"balanceOf","type":"function","stateMutability":"view",
   "inputs":[{"name":"account","type":"address"}],"outputs":[{"name":"","type":"uint256"}]},
  {"name":"approve","type":"function","stateMutability":"nonpayable",
   "inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
   "outputs":[{"name":"","type":"bool"}]},
  {"name":"allowance","type":"function","stateMutability":"view",
   "inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],
   "outputs":[{"name":"","type":"uint256"}]},
  {"name":"nonces","type":"function","stateMutability":"view",
   "inputs":[{"name":"owner","type":"address"}],"outputs":[{"name":"","type":"uint256"}]}
]""")

KWH_ABI = json.loads("""[
  {"name":"convertKWh","type":"function","stateMutability":"nonpayable",
   "inputs":[{"name":"tokenToPay","type":"address"},{"name":"amountPayment","type":"uint256"}],
   "outputs":[]},
  {"name":"balanceOf","type":"function","stateMutability":"view",
   "inputs":[{"name":"account","type":"address"}],"outputs":[{"name":"","type":"uint256"}]},
  {"name":"approve","type":"function","stateMutability":"nonpayable",
   "inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
   "outputs":[{"name":"","type":"bool"}]}
]""")

GREEN_ABI = json.loads("""[
  {"name":"makeGreenBox","type":"function","stateMutability":"nonpayable",
   "inputs":[{"name":"domainID","type":"uint256"},{"name":"boxSteps","type":"uint256"}],
   "outputs":[]}
]""")


# ════════════════════════════════════════════════════════════════════
# Arkreen OpenAPI wrappers
# ════════════════════════════════════════════════════════════════════

def _rpc(method: str, params: dict) -> dict:
    """Call openapi.arkreen.com (JSON-RPC 2.0). params is wrapped in a list."""
    payload = {"jsonrpc": "2.0", "id": "1", "method": method, "params": [params]}
    resp = requests.post(
        OPENAPI_URL, json=payload, timeout=30,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"Arkreen API error [{method}]: {data['error']}")
    return data.get("result", {})


def api_get_owner_rec_data(owner: str) -> dict:
    """rec_getOwnerRecDataNew → {totalREOutput (hex mWh), startDate, endDate, ...}."""
    result = _rpc("rec_getOwnerRecDataNew", {"owner": owner})
    log.info(f"rec_getOwnerRecDataNew: {result}")
    return result


def api_issue_owner_rec(owner: str, start_date: str, end_date: str,
                        total_power_hex: str, value_approval: int,
                        deadline: int, signature_hex: str,
                        by_power: bool = False) -> dict:
    """
    rec_issueOwnerRecNew → pin energy data to IPFS, return {ipfs, url}.

      total_power_hex : totalREOutput as hex (e.g. "0x236e6f40", unit mWh)
      value_approval  : approved AKRE wei (amountREC * paymentTokenPrice)
      signature_hex   : EIP-2612 permit signature, v+r+s concatenated (132 hex)
    """
    params = {
        "owner":             owner,
        "issuer":            ARKREEN_ISSUER,
        "startDate":         start_date,        # "yyyyMMdd"
        "endDate":           end_date,
        "totalARECPower":    total_power_hex,   # hex mWh
        "valueApproval":     hex(value_approval),
        "deadlineApproval":  deadline,
        "signatureApproval": signature_hex,
        "byPower":           by_power,
    }
    result = _rpc("rec_issueOwnerRecNew", params)
    log.info(f"rec_issueOwnerRecNew → ipfs={result.get('ipfs', 'N/A')}")
    return result


# ════════════════════════════════════════════════════════════════════
# Web3 helpers
# ════════════════════════════════════════════════════════════════════

def init_web3() -> Web3:
    w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))
    w3.middleware_onion.inject(POA_MIDDLEWARE, layer=0)
    if not w3.is_connected():
        raise RuntimeError("Cannot connect to Polygon RPC")
    log.info(f"Polygon connected | block {w3.eth.block_number} | chainId {w3.eth.chain_id}")
    return w3


def get_gas_price(w3: Web3) -> int:
    return int(w3.eth.gas_price * GAS_MULTIPLIER)


def send_tx(w3: Web3, tx: dict, private_key: str, timeout: int = 180):
    """
    Sign and send a transaction, wait for receipt.
    Compatible with eth_account 0.11.x (rawTransaction) and 0.12.x
    (raw_transaction). See SKILL.md §7.
    """
    signed = Account.sign_transaction(tx, private_key)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw)
    log.info(f"tx sent: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
    if receipt.status != 1:
        raise RuntimeError(f"tx reverted: {tx_hash.hex()}")
    return receipt


def yyyymmdd_to_unix(date_str: str, end_of_day: bool = False) -> int:
    """Convert a yyyyMMdd string to a UTC unix timestamp."""
    import calendar
    from datetime import datetime, timezone
    dt = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
    ts = calendar.timegm(dt.timetuple())
    if end_of_day:
        ts += 86400 - 1
    return ts


# ════════════════════════════════════════════════════════════════════
# EIP-2612 permit signing (gasless AKRE approval)
# ════════════════════════════════════════════════════════════════════

def sign_permit_akre(w3: Web3, private_key: str, spender: str,
                     value: int, deadline: int):
    """
    Produce an EIP-2612 permit signature for AKRE.
    Returns (v, r_bytes32, s_bytes32, sig_hex) where sig_hex is the
    "0x" + v + r + s concatenation the Arkreen API expects. See SKILL.md §6.
    """
    akre  = w3.eth.contract(address=ADDR["AKRE"], abi=ERC20_ABI)
    owner = Account.from_key(private_key).address
    nonce = akre.functions.nonces(owner).call()

    domain = {
        "name":              "AKRE Token",
        "version":           "1",
        "chainId":           w3.eth.chain_id,
        "verifyingContract": ADDR["AKRE"],
    }
    types = {
        "Permit": [
            {"name": "owner",    "type": "address"},
            {"name": "spender",  "type": "address"},
            {"name": "value",    "type": "uint256"},
            {"name": "nonce",    "type": "uint256"},
            {"name": "deadline", "type": "uint256"},
        ]
    }
    message = {"owner": owner, "spender": spender, "value": value,
               "nonce": nonce, "deadline": deadline}

    signed = Account.sign_typed_data(private_key, domain, types, message)
    # Derive r/s/v from the 65-byte signature so this works regardless of
    # whether eth_account exposes signed.r as int or HexBytes.
    sig = bytes(signed.signature)
    r, s, v = sig[0:32], sig[32:64], sig[64]
    sig_hex = "0x" + bytes([v]).hex() + r.hex() + s.hex()
    log.info(f"EIP-2612 permit signed (v={v}, nonce={nonce})")
    return v, r, s, sig_hex


# ════════════════════════════════════════════════════════════════════
# Step 1: mint AREC NFT
# ════════════════════════════════════════════════════════════════════

def _topic_hex(topic) -> str:
    """Normalize a log topic (HexBytes / bytes / str) to lowercase '0x…' hex.

    HexBytes.hex() has returned values with and without the '0x' prefix across
    versions, so force plain bytes hex and re-prefix for a stable comparison.
    """
    if isinstance(topic, str):
        return "0x" + topic.lower().removeprefix("0x")
    return "0x" + bytes(topic).hex()


def extract_token_id(receipt) -> int | None:
    """Pull the minted tokenId from an ERC721 Transfer event. See SKILL.md §6."""
    for entry in receipt.logs:
        t = entry.topics
        if (len(t) >= 4 and _topic_hex(t[0]) == TRANSFER_SIG
                and int(_topic_hex(t[1]), 16) == 0):       # from == 0x0 (mint)
            return int(_topic_hex(t[3]), 16)               # tokenId is topics[3]
    # Fallback: first small integer found in any indexed topic
    for entry in receipt.logs:
        for t in entry.topics[1:]:
            v = int(_topic_hex(t), 16)
            if 0 < v < 10_000_000:
                return v
    return None


def step1_mint_arec(w3: Web3, private_key: str) -> int | None:
    """Full AREC issuance: query power → fee → permit → IPFS → mintRECRequest."""
    wallet = Account.from_key(private_key).address
    log.info(f"=== Step 1: mint AREC | wallet {wallet} ===")

    # 1a. query mintable power
    rec_data = api_get_owner_rec_data(wallet)
    total_power_hex = rec_data.get("totalREOutput") or rec_data.get("totalPowerOutput")
    start_date = rec_data.get("startDate")
    end_date   = rec_data.get("endDate") or rec_data.get("lastDate")

    if not total_power_hex or int(total_power_hex, 16) == 0:
        log.info("No mintable power output available")
        return None

    amount_rec = int(total_power_hex, 16)  # unit: mWh
    log.info(f"Mintable: {amount_rec} mWh = {amount_rec / 1e6:.4f} kWh "
             f"({start_date} → {end_date})")

    # 1b. issuance fee rate
    arec = w3.eth.contract(address=ADDR["AREC"], abi=AREC_ABI)
    try:
        rate = arec.functions.paymentTokenPrice(ADDR["AKRE"]).call()
    except Exception as e:
        log.warning(f"paymentTokenPrice failed, defaulting to 1: {e}")
        rate = 1
    value_approval = amount_rec * rate
    log.info(f"AKRE to approve: {value_approval} wei = {value_approval / 1e18:.6f} AKRE")

    # balance check
    akre    = w3.eth.contract(address=ADDR["AKRE"], abi=ERC20_ABI)
    balance = akre.functions.balanceOf(wallet).call()
    if balance < value_approval:
        raise ValueError(f"Insufficient AKRE: need {value_approval / 1e18:.6f}, "
                         f"have {balance / 1e18:.6f}")

    # 1c. EIP-2612 permit
    deadline = int(time.time()) + 7200
    v, r, s, sig_hex = sign_permit_akre(w3, private_key, ADDR["AREC"],
                                        value_approval, deadline)

    # 1d. Arkreen API → IPFS CID
    log.info("Requesting IPFS CID from Arkreen API...")
    ipfs_data = api_issue_owner_rec(
        owner=wallet, start_date=start_date, end_date=end_date,
        total_power_hex=total_power_hex, value_approval=value_approval,
        deadline=deadline, signature_hex=sig_hex, by_power=False,
    )
    cid = ipfs_data.get("ipfs") or ipfs_data.get("cid")
    uri = ipfs_data.get("url") or ipfs_data.get("uri", "")
    if not cid:
        raise RuntimeError(f"Arkreen API returned no CID: {ipfs_data}")
    log.info(f"IPFS CID: {cid}")

    # 1e. mintRECRequest on-chain
    rec_request = (
        ARKREEN_ISSUER,
        yyyymmdd_to_unix(start_date),
        yyyymmdd_to_unix(end_date, end_of_day=True),
        amount_rec, cid, "", uri, "",
    )
    permit_to_pay = (ADDR["AKRE"], value_approval, deadline, v, r, s)

    tx = arec.functions.mintRECRequest(rec_request, permit_to_pay).build_transaction({
        "from": wallet,
        "nonce": w3.eth.get_transaction_count(wallet),
        "gasPrice": get_gas_price(w3),
        "gas": 700_000,
    })
    receipt = send_tx(w3, tx, private_key, timeout=180)

    token_id = extract_token_id(receipt)
    log.info(f"✅ AREC NFT minted | tokenId {token_id} | status Pending")
    return token_id


# ════════════════════════════════════════════════════════════════════
# Wait for Arkreen certification
# ════════════════════════════════════════════════════════════════════

def wait_for_certification(w3: Web3, token_id: int,
                           timeout_hours: float = CERT_TIMEOUT_HOURS) -> bool:
    """Poll getRECDataCore until status == Certified (3). See SKILL.md §3."""
    arec     = w3.eth.contract(address=ADDR["AREC"], abi=AREC_ABI)
    deadline = time.time() + timeout_hours * 3600
    attempt  = 0
    while time.time() < deadline:
        status   = arec.functions.getRECDataCore(token_id).call()[2]
        attempt += 1
        log.info(f"AREC #{token_id} status: {REC_STATUS.get(status, status)} "
                 f"(poll #{attempt})")
        if status == 3:
            log.info(f"✅ AREC #{token_id} certified")
            return True
        if status in (1, 2):
            log.error(f"❌ AREC #{token_id} {REC_STATUS.get(status)}, giving up")
            return False
        time.sleep(CERT_WAIT_SEC)
    log.error(f"Certification timed out ({timeout_hours}h)")
    return False


# ════════════════════════════════════════════════════════════════════
# Step 2: liquidizeREC → ART
# ════════════════════════════════════════════════════════════════════

def step2_liquidize(w3: Web3, private_key: str, token_id: int) -> int:
    """Fragment a Certified AREC NFT into ART tokens."""
    wallet = Account.from_key(private_key).address
    arec   = w3.eth.contract(address=ADDR["AREC"], abi=AREC_ABI)

    tx = arec.functions.liquidizeREC(token_id).build_transaction({
        "from": wallet,
        "nonce": w3.eth.get_transaction_count(wallet),
        "gasPrice": get_gas_price(w3),
        "gas": 250_000,
    })
    send_tx(w3, tx, private_key)

    art_bal = w3.eth.contract(address=ADDR["ART"], abi=ERC20_ABI).functions.balanceOf(wallet).call()
    log.info(f"✅ liquidized | ART balance: {art_bal / 10 ** ART_DECIMALS:.9f} ART")
    return art_bal


# ════════════════════════════════════════════════════════════════════
# Step 3: convertKWh → kWh tokens
# ════════════════════════════════════════════════════════════════════

def step3_convert_kwh(w3: Web3, private_key: str, art_amount: int | None = None) -> int:
    """Convert ART tokens into kWh tokens (approve + convertKWh)."""
    wallet = Account.from_key(private_key).address
    art    = w3.eth.contract(address=ADDR["ART"], abi=ERC20_ABI)
    kwh    = w3.eth.contract(address=ADDR["KWH"], abi=KWH_ABI)

    if art_amount is None:
        art_amount = art.functions.balanceOf(wallet).call()
    if art_amount == 0:
        log.warning("ART balance is 0, skipping convertKWh")
        return 0

    # Manage nonce manually across approve + convert (SKILL.md §8)
    nonce = w3.eth.get_transaction_count(wallet)
    if art.functions.allowance(wallet, ADDR["KWH"]).call() < art_amount:
        tx = art.functions.approve(ADDR["KWH"], art_amount).build_transaction({
            "from": wallet, "nonce": nonce,
            "gasPrice": get_gas_price(w3), "gas": 100_000,
        })
        send_tx(w3, tx, private_key, timeout=120)
        nonce += 1
        log.info("ART → kWh approval confirmed")

    tx = kwh.functions.convertKWh(ADDR["ART"], art_amount).build_transaction({
        "from": wallet, "nonce": nonce,
        "gasPrice": get_gas_price(w3), "gas": 150_000,
    })
    send_tx(w3, tx, private_key)

    kwh_bal = kwh.functions.balanceOf(wallet).call()
    log.info(f"✅ convertKWh done | kWh balance: {kwh_bal / 10 ** KWH_DECIMALS:.4f} kWh")
    return kwh_bal


# ════════════════════════════════════════════════════════════════════
# Step 4: makeGreenBox → GreenBTC
# ════════════════════════════════════════════════════════════════════

def find_working_domain(green, wallet: str, box_steps: int) -> int | None:
    """Simulate makeGreenBox across candidate domains, skip greenized/empty ones."""
    for did in GREENBTC_DOMAIN_IDS:
        try:
            green.functions.makeGreenBox(did, box_steps).call({"from": wallet})
            return did
        except Exception as e:
            msg = str(e)
            if "All Greenized" in msg or "Empty Domain" in msg:
                log.info(f"Domain {did} unavailable ({msg.split(':')[-1].strip()}), skipping")
                continue
            log.warning(f"Domain {did} simulate error: {msg}; trying it anyway")
            return did
    return None


def step4_green_btc(w3: Web3, private_key: str, kwh_amount: int | None = None) -> int:
    """Burn kWh tokens to green Bitcoin blocks via GreenBTC."""
    wallet = Account.from_key(private_key).address
    kwh    = w3.eth.contract(address=ADDR["KWH"], abi=KWH_ABI)
    green  = w3.eth.contract(address=ADDR["GREEN"], abi=GREEN_ABI)

    if kwh_amount is None:
        kwh_amount = kwh.functions.balanceOf(wallet).call()
    if kwh_amount < KWH_PER_STEP:
        log.warning(f"kWh below one step ({kwh_amount} < {KWH_PER_STEP}), skipping")
        return 0

    box_steps = kwh_amount // KWH_PER_STEP
    to_burn   = box_steps * KWH_PER_STEP
    log.info(f"Greening {box_steps} block(s), burning {to_burn / 10 ** KWH_DECIMALS:.2f} kWh")

    domain_id = find_working_domain(green, wallet, box_steps)
    if domain_id is None:
        log.error("No available GreenBTC domain")
        return 0
    log.info(f"Using GreenBTC domain {domain_id}")

    # approve + makeGreenBox with manual nonce (SKILL.md §8)
    nonce = w3.eth.get_transaction_count(wallet)
    tx = kwh.functions.approve(ADDR["GREEN"], to_burn).build_transaction({
        "from": wallet, "nonce": nonce,
        "gasPrice": get_gas_price(w3), "gas": 100_000,
    })
    send_tx(w3, tx, private_key, timeout=120)

    tx = green.functions.makeGreenBox(domain_id, box_steps).build_transaction({
        "from": wallet, "nonce": nonce + 1,
        "gasPrice": get_gas_price(w3),
        "gas": min(2_000_000, 150_000 + box_steps * 120_000),
    })
    send_tx(w3, tx, private_key, timeout=300)

    log.info(f"✅ Greened {box_steps} Bitcoin block(s) on domain {domain_id}")
    return box_steps


# ════════════════════════════════════════════════════════════════════
# On-chain scan: find Certified AREC tokens
# ════════════════════════════════════════════════════════════════════

def get_certified_arec_tokens(w3: Web3, wallet: str) -> list[int]:
    """Return token IDs held by wallet whose status == Certified (3)."""
    arec    = w3.eth.contract(address=ADDR["AREC"], abi=AREC_ABI)
    balance = arec.functions.balanceOf(wallet).call()
    certified = []
    log.info(f"Wallet holds {balance} AREC NFT(s)")
    for i in range(balance):
        tid  = arec.functions.tokenOfOwnerByIndex(wallet, i).call()
        core = arec.functions.getRECDataCore(tid).call()
        status = core[2]
        log.info(f"  AREC #{tid}: {REC_STATUS.get(status, status)}, "
                 f"{core[1] / 1e6:.4f} kWh")
        if status == 3:
            certified.append(tid)
    return certified


# ════════════════════════════════════════════════════════════════════
# Pipeline
# ════════════════════════════════════════════════════════════════════

def run_pipeline(w3: Web3, private_key: str, wallet: str, do_mint: bool = True):
    """do_mint=True tries to issue a new AREC first; both modes then process
    every Certified AREC the wallet holds through liquidize → kWh → greening."""
    log.info("=" * 50)
    log.info(f"AREC Bot run | wallet {wallet}")

    if do_mint:
        try:
            token_id = step1_mint_arec(w3, private_key)
            if token_id:
                log.info(f"Waiting for AREC #{token_id} certification "
                         f"(up to {CERT_TIMEOUT_HOURS}h)...")
                wait_for_certification(w3, token_id)
        except Exception as e:
            log.error(f"Step 1 (mint) failed: {e}")

    try:
        for tid in get_certified_arec_tokens(w3, wallet):
            log.info(f"--- liquidize AREC #{tid} ---")
            step2_liquidize(w3, private_key, tid)
            time.sleep(3)
    except Exception as e:
        log.error(f"Step 2 (liquidize) failed: {e}")

    try:
        step3_convert_kwh(w3, private_key)
        time.sleep(3)
    except Exception as e:
        log.error(f"Step 3 (convertKWh) failed: {e}")

    try:
        step4_green_btc(w3, private_key)
    except Exception as e:
        log.error(f"Step 4 (makeGreenBox) failed: {e}")

    log.info("=== pipeline run complete ===")


def main():
    if not PRIVATE_KEY or not WALLET_ADDR:
        raise ValueError("Set PRIVATE_KEY and WALLET_ADDR (see .env.example)")

    w3     = init_web3()
    wallet = Web3.to_checksum_address(WALLET_ADDR)

    log.info(f"AREC Bot started | poll interval {POLL_INTERVAL_SEC}s")
    while True:
        try:
            run_pipeline(w3, PRIVATE_KEY, wallet, do_mint=True)
        except KeyboardInterrupt:
            log.info("Bot stopped")
            break
        except Exception as e:
            log.error(f"Pipeline error: {e}", exc_info=True)
        log.info(f"Sleeping {POLL_INTERVAL_SEC}s...")
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
