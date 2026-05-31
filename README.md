# AREC Green Bot

[![CI](https://github.com/linyao0177/arec-green-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/linyao0177/arec-green-bot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

Automated client for the **Arkreen AREC** renewable-energy greening pipeline on
Polygon. It turns real solar/wind miner output into greened Bitcoin blocks by
running the full Arkreen flow end to end, unattended.

```
 mintRECRequest ──▶ (Issuer certifies) ──▶ liquidizeREC ──▶ convertKWh ──▶ makeGreenBox
   Step 1                                     Step 2          Step 3          Step 4
 AREC NFT                                  NFT → ART       ART → kWh       kWh → greened
 (from miner output)                         token           token         Bitcoin blocks
```

Arkreen's AREC certificate-to-GreenBTC flow has no official programmatic
client. This repo fills that gap: a runnable bot **plus** `SKILL.md`, a
detailed developer reference of every contract address, ABI quirk, token unit,
and revert reason — reverse-engineered from the Arkreen dApp and verified on
Polygon mainnet.

## Features

- **One-shot issuance** — queries mintable miner output, computes the AKRE fee,
  signs an EIP-2612 permit (gasless approval), pins energy data to IPFS via the
  Arkreen OpenAPI, and mints the AREC NFT on-chain.
- **Certification polling** — waits for the Arkreen Issuer to certify the NFT.
- **Greening pipeline** — liquidizes Certified NFTs to ART, converts ART to kWh
  tokens, and burns kWh to green Bitcoin blocks on an available GreenBTC domain.
- **Safety rails** — simulates `makeGreenBox` to skip fully-greenized/unopened
  domains, manages nonces across `approve`+action pairs, and is compatible with
  both `eth_account` 0.11/0.12 and `web3` 6/7.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env        # then fill in RPC_URL, PRIVATE_KEY, WALLET_ADDR
python arec_bot.py
```

### Configuration (`.env`)

| Variable | Description | Default |
|----------|-------------|---------|
| `RPC_URL` | Polygon RPC endpoint | `https://polygon-rpc.com` |
| `PRIVATE_KEY` | Wallet private key (`0x…`) | — (required) |
| `WALLET_ADDR` | Wallet address (checksum) | — (required) |
| `POLL_INTERVAL_SEC` | Main loop interval | `300` |
| `CERT_TIMEOUT_HOURS` | Max wait for certification | `48` |
| `GREENBTC_DOMAIN_IDS` | Candidate GreenBTC domains | `[2,3,11,12,…,18]` |
| `GAS_MULTIPLIER` | Gas price multiplier | `1.3` |

## How it works

The four pipeline stages map directly to functions in
[`arec_bot.py`](./arec_bot.py): `step1_mint_arec`, `step2_liquidize`,
`step3_convert_kwh`, and `step4_green_btc`, orchestrated by `run_pipeline`.
The contract addresses, ABIs, status enum, and unit conversions all come from
[`SKILL.md`](./SKILL.md), which doubles as a Claude Code skill and as
standalone integration documentation for anyone building on Arkreen AREC.

## Contracts (Polygon mainnet)

| Contract | Address |
|----------|---------|
| AREC NFT | `0x954585adF9425F66a0a2FD8e10682EB7c4F1f1fD` |
| ART token | `0x58E4D14ccddD1E993e6368A8c5EAa290C95caFDF` |
| kWh token | `0x5740A27990d4AaA4FB83044a6C699D435B9BA6F1` |
| AKRE token | `0xE9c21De62C5C5d0cEAcCe2762bF655AfDcEB7ab3` |
| GreenBTC2S | `0x3221F5818A5CF99e09f5BE0E905d8F145935e3E0` |

## Development

```bash
pip install -r requirements-dev.txt
ruff check .     # lint
pytest           # offline unit tests (no RPC / chain / API needed)
```

Tests cover the pure logic and every network-facing path via mocks: unit
conversions, `yyyyMMdd`→unix, ERC721 `tokenId` extraction, JSON-RPC param
encoding, GreenBTC domain selection, the Certified-token scan, and a full
EIP-2612 permit round-trip (sign → recover). CI runs them on Python 3.10–3.12.

## ⚠️ Disclaimer

This bot signs and broadcasts **real transactions on Polygon mainnet** using
your private key, and spends AKRE and gas. It is unofficial software, not
affiliated with or endorsed by Arkreen, built from reverse-engineered API and
contract interfaces. Review the code, test with a low-value wallet first, and
**never commit your `.env` or private key.** Use at your own risk; no warranty.

## License

[MIT](./LICENSE)
