"""
Unit tests for arec_bot.

All tests are offline: no RPC, no chain, no Arkreen API. Network-facing code
paths are exercised with mocks/monkeypatch so the suite runs in CI in seconds.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import arec_bot


# ─── pure helpers ────────────────────────────────────────────────────

def test_unit_constants():
    # kWh has 6 decimals; one GreenBox step = 10 kWh tokens (SKILL.md §4).
    assert arec_bot.KWH_DECIMALS == 6
    assert arec_bot.KWH_PER_STEP == 10_000_000
    assert arec_bot.ART_DECIMALS == 9
    assert arec_bot.TRANSFER_SIG.startswith("0x")
    assert len(arec_bot.TRANSFER_SIG) == 66  # 0x + 64 hex


def test_yyyymmdd_to_unix():
    assert arec_bot.yyyymmdd_to_unix("19700101") == 0
    assert arec_bot.yyyymmdd_to_unix("19700101", end_of_day=True) == 86_399
    assert arec_bot.yyyymmdd_to_unix("20240101") == 1_704_067_200  # UTC midnight


# ─── token_id extraction (SKILL.md §6) ───────────────────────────────

def _topic(value: int) -> bytes:
    return value.to_bytes(32, "big")


def _sig_topic() -> bytes:
    return bytes.fromhex(arec_bot.TRANSFER_SIG[2:])


def test_extract_token_id_from_transfer():
    mint_log = SimpleNamespace(topics=[
        _sig_topic(),          # event signature
        _topic(0),             # from == 0x0 → mint
        _topic(0xABCD),        # to
        _topic(12_345),        # tokenId  ← topics[3], not [2]
    ])
    receipt = SimpleNamespace(logs=[mint_log])
    assert arec_bot.extract_token_id(receipt) == 12_345


def test_extract_token_id_non_mint_falls_back_to_token_id():
    # A non-mint Transfer (from != 0) skips the primary path. The fallback scans
    # indexed topics and returns the first small integer; real from/to are
    # 20-byte addresses (huge ints, skipped), so it correctly lands on tokenId.
    addr_a = 0xABCDEF0123456789ABCDEF0123456789ABCDEF01
    addr_b = 0x1111111122222222333333334444444455555555
    transfer = SimpleNamespace(topics=[
        _sig_topic(), _topic(addr_a), _topic(addr_b), _topic(42),
    ])
    receipt = SimpleNamespace(logs=[transfer])
    assert arec_bot.extract_token_id(receipt) == 42


def test_extract_token_id_none_when_empty():
    assert arec_bot.extract_token_id(SimpleNamespace(logs=[])) is None


def test_topic_hex_accepts_bytes_and_str():
    assert arec_bot._topic_hex(b"\x00" * 32) == "0x" + "00" * 32
    assert arec_bot._topic_hex("0xAB") == "0xab"
    assert arec_bot._topic_hex("AB") == "0xab"


# ─── Arkreen OpenAPI wrappers ────────────────────────────────────────

def test_rpc_wraps_params_in_list(monkeypatch):
    captured = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"result": {"ok": 1}}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["payload"] = kwargs["json"]
        return Resp()

    monkeypatch.setattr(arec_bot.requests, "post", fake_post)
    out = arec_bot._rpc("rec_getOwnerRecDataNew", {"owner": "0xabc"})

    assert out == {"ok": 1}
    assert captured["url"] == arec_bot.OPENAPI_URL
    assert captured["payload"]["method"] == "rec_getOwnerRecDataNew"
    assert captured["payload"]["params"] == [{"owner": "0xabc"}]  # JSON-RPC list


def test_rpc_raises_on_error(monkeypatch):
    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"error": {"message": "boom"}}

    monkeypatch.setattr(arec_bot.requests, "post", lambda *a, **k: Resp())
    with pytest.raises(RuntimeError, match="boom"):
        arec_bot._rpc("rec_x", {})


def test_api_issue_owner_rec_encodes_value_and_signature(monkeypatch):
    captured = {}

    def fake_rpc(method, params):
        captured["method"] = method
        captured["params"] = params
        return {"ipfs": "bafytest", "url": "https://ipfs/u"}

    monkeypatch.setattr(arec_bot, "_rpc", fake_rpc)
    out = arec_bot.api_issue_owner_rec(
        owner="0xowner", start_date="20240101", end_date="20240102",
        total_power_hex="0x10", value_approval=1000, deadline=123,
        signature_hex="0xdeadbeef",
    )

    assert out["ipfs"] == "bafytest"
    assert captured["method"] == "rec_issueOwnerRecNew"
    assert captured["params"]["valueApproval"] == hex(1000)      # "0x3e8"
    assert captured["params"]["signatureApproval"] == "0xdeadbeef"
    assert captured["params"]["byPower"] is False


# ─── GreenBTC domain selection (SKILL.md §5) ─────────────────────────

def test_find_working_domain_skips_unavailable(monkeypatch):
    monkeypatch.setattr(arec_bot, "GREENBTC_DOMAIN_IDS", [2, 3, 11])
    green = MagicMock()

    def make_box(did, steps):
        call = MagicMock()
        if did == 2:
            call.call.side_effect = Exception("execution reverted: GBC2: All Greenized")
        elif did == 3:
            call.call.side_effect = Exception("execution reverted: GBC2: Empty Domain")
        else:
            call.call.return_value = None
        return call

    green.functions.makeGreenBox.side_effect = make_box
    assert arec_bot.find_working_domain(green, "0xwallet", 1) == 11


def test_find_working_domain_none_when_all_blocked(monkeypatch):
    monkeypatch.setattr(arec_bot, "GREENBTC_DOMAIN_IDS", [2, 3])
    green = MagicMock()

    def make_box(did, steps):
        call = MagicMock()
        call.call.side_effect = Exception("GBC2: All Greenized")
        return call

    green.functions.makeGreenBox.side_effect = make_box
    assert arec_bot.find_working_domain(green, "0xwallet", 1) is None


# ─── certified-token scan (SKILL.md §10) ─────────────────────────────

def test_get_certified_arec_tokens_filters_by_status():
    w3 = MagicMock()
    arec = w3.eth.contract.return_value
    arec.functions.balanceOf.return_value.call.return_value = 3

    ids = [10, 11, 12]
    statuses = {10: 3, 11: 0, 12: 3}  # only 10 and 12 are Certified

    arec.functions.tokenOfOwnerByIndex.side_effect = \
        lambda owner, i: SimpleNamespace(call=lambda: ids[i])
    arec.functions.getRECDataCore.side_effect = \
        lambda tid: SimpleNamespace(call=lambda: ["0xissuer", 5_000_000, statuses[tid], 0])

    assert arec_bot.get_certified_arec_tokens(w3, "0xwallet") == [10, 12]


# ─── EIP-2612 permit signing (SKILL.md §6) ───────────────────────────

def test_sign_permit_akre_is_valid_and_recoverable():
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    # Well-known throwaway test key (Hardhat account #0) — never used for funds.
    pk = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
    owner = Account.from_key(pk).address
    spender = arec_bot.ADDR["AREC"]
    value, deadline = 1_000_000, 1_900_000_000

    w3 = MagicMock()
    w3.eth.chain_id = 137
    w3.eth.contract.return_value.functions.nonces.return_value.call.return_value = 0

    v, r, s, sig_hex = arec_bot.sign_permit_akre(w3, pk, spender, value, deadline)

    # API signature format: 0x + v(1) + r(32) + s(32) = 132 hex chars.
    assert sig_hex.startswith("0x") and len(sig_hex) == 132
    assert len(r) == 32 and len(s) == 32 and v in (27, 28)

    # Rebuild the typed data and confirm the signature recovers to `owner`.
    domain = {"name": "AKRE Token", "version": "1", "chainId": 137,
              "verifyingContract": arec_bot.ADDR["AKRE"]}
    types = {"Permit": [
        {"name": "owner", "type": "address"},
        {"name": "spender", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint256"},
    ]}
    message = {"owner": owner, "spender": spender, "value": value,
               "nonce": 0, "deadline": deadline}
    signable = encode_typed_data(domain, types, message)
    recovered = Account.recover_message(signable, signature=r + s + bytes([v]))
    assert recovered == owner
