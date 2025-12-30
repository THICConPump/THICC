import sys, os
# local patched version of pumpswapamm for Token-2022 standard
sys.path.insert(0, os.path.abspath("./vendor"))
import time
import asyncio
import requests
from types import SimpleNamespace
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed, Processed
from solders.keypair import Keypair  # type: ignore
from solders.pubkey import Pubkey  # type: ignore
from solders.signature import Signature  # type: ignore
import pumpswapamm.pumpswapamm as pm
from pumpswapamm import PumpSwap, convert_pool_keys, fetch_pool_state, WSOL_MINT
from pumpswapamm.fetch_reserves import fetch_pool_base_price
from spl.token.instructions import get_associated_token_address
from spl.token.constants import TOKEN_2022_PROGRAM_ID  # for your base token ATA reads

load_dotenv()

API_KEY = os.getenv("PUMP_SWAP_API_KEY")
RPC_URL = os.getenv("HELIUS_RPC_URL")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")

# config
CREATOR_FEE_MINT = "6Yk3ykeAzkmWMKoh2pT2rRyggUxF8vsrh5UcL2vopump"  # fees claim mint
BUY_MINT = "Gbu7JAKhTVtGyRryg8cYPiKNhonXpUqbrZuCDjfUpump"        # token to buy
POOL_ADDRESS = "4KfHWqcSJWsrTq19FLzFYm3cGN4oASAj7ZCiUoFx16KS"     # pumpswap pool

SLIPPAGE_PCT = 10.0
PRIORITY_FEE_SOL_PUMPORTAL = 0.001
PRIORITY_FEE_SOL_PUMPSWAP = 0.002

LOOP_SECONDS = 30 * 60

# tx buffers
SOL_GAS_BUFFER = 0.03     # keep this much SOL untouched each cycle
SOL_CAP_BUFFER = 0.03     # keep this much SOL untouched when computing sol_cap
TOKEN_DUST_BUFFER_PCT = 0.001  # deposit 99.9% of bought tokens, keep dust


def pubkey_str_to_bytes(s: str) -> bytes:
    return bytes(Pubkey.from_string(s))


async def wait_for_sig(async_client: AsyncClient, sig_str: str, timeout_s: int = 60) -> bool:
    """Poll signature status until confirmed/finalized or timeout."""
    sig = Signature.from_string(sig_str)
    deadline = time.time() + timeout_s
    backoff = 0.5

    while time.time() < deadline:
        resp = await async_client.get_signature_statuses([sig], search_transaction_history=True)
        st = resp.value[0]
        if st is not None:
            # st.err is None => success
            if st.err is None and st.confirmation_status in ("confirmed", "finalized"):
                return True
            if st.err is not None:
                return False
        await asyncio.sleep(backoff)
        backoff = min(backoff * 1.2, 2.5)
    return False


async def get_sol(async_client: AsyncClient, owner: Pubkey) -> int:
    return (await async_client.get_balance(owner, commitment=Confirmed)).value


async def get_token_ui(async_client: AsyncClient, owner: Pubkey, mint: Pubkey, token_program_id=TOKEN_2022_PROGRAM_ID):
    ata = get_associated_token_address(owner, mint, token_program_id)
    resp = await async_client.get_token_account_balance(ata, commitment=Confirmed)
    return ata, int(resp.value.amount), int(resp.value.decimals)


def pumpportal_trade(payload: dict) -> dict:
    url = f"https://pumpportal.fun/api/trade?api-key={API_KEY}"
    r = requests.post(url=url, data=payload, timeout=30)
    try:
        return r.json()
    except Exception:
        return {"error": "non-json response", "status": r.status_code, "text": r.text[:300]}


def extract_sig(resp: dict) -> str | None:
    """
    PumpPortal responses vary. Common cases:
      {"signature":"..."} or {"txSignature":"..."} or {"data":{"signature":"..."}}
    """
    for k in ("signature", "txSignature", "tx_signature"):
        if isinstance(resp.get(k), str):
            return resp[k]
    data = resp.get("data")
    if isinstance(data, dict):
        for k in ("signature", "txSignature", "tx_signature"):
            if isinstance(data.get(k), str):
                return data[k]
    return None


def collect_creator_fees() -> dict:
    resp = pumpportal_trade({
        "action": "collectCreatorFee",
        "priorityFee": PRIORITY_FEE_SOL_PUMPORTAL,
        "pool": "pump",
        "mint": CREATOR_FEE_MINT,
    })
    print("collect_creator_fees resp:", resp)
    return resp


def buy_tokens(amount_sol: float) -> dict:
    resp = pumpportal_trade({
        "action": "buy",
        "mint": BUY_MINT,
        "amount": amount_sol,
        "denominatedInSol": "true",
        "slippage": SLIPPAGE_PCT,
        "priorityFee": PRIORITY_FEE_SOL_PUMPORTAL,
        "pool": "auto",
    })
    print("buy_tokens resp:", resp)
    return resp


async def add_pumpswap_liquidity(
    async_client: AsyncClient,
    sdk: PumpSwap,
    keypair: Keypair,
    pool_address: str,
    base_amount_ui: float,
    sol_cap_ui: float,
) -> bool:
    # fetch pool state
    res = await fetch_pool_state(pool_address, async_client)
    pool_state = res[0] if isinstance(res, tuple) else res

    if isinstance(pool_state, dict):
        for k in (
            "creator",
            "base_mint",
            "quote_mint",
            "lp_mint",
            "pool_base_token_account",
            "pool_quote_token_account",
            "coin_creator",
        ):
            if k in pool_state and isinstance(pool_state[k], str):
                pool_state[k] = pubkey_str_to_bytes(pool_state[k])
        pool_state = SimpleNamespace(**pool_state)

    # convert keys
    try:
        pool_keys = convert_pool_keys(pool_state, pm.NEW_POOL_TYPE)
    except Exception:
        pool_keys = convert_pool_keys(pool_state, pm.OLD_POOL_TYPE)

    # decimals
    base_mint_str = pool_keys["base_mint"]
    mint_info = await async_client.get_account_info_json_parsed(
        Pubkey.from_string(base_mint_str),
        commitment=Processed
    )
    if not mint_info or not mint_info.value:
        raise RuntimeError("Failed to fetch mint info to read decimals.")
    dec_base = mint_info.value.data.parsed["info"]["decimals"]

    # reserves
    _, base_bal, quote_bal = await fetch_pool_base_price(pool_keys, async_client)

    pool_data = {
        "pool_pubkey": Pubkey.from_string(pool_address),
        "token_base":  Pubkey.from_string(pool_keys["base_mint"]),
        "token_quote": Pubkey.from_string(pool_keys["quote_mint"]),
        "lp_mint":     pool_keys["lp_mint"],
        "pool_base_token_account":  pool_keys["pool_base_token_account"],
        "pool_quote_token_account": pool_keys["pool_quote_token_account"],
        "base_balance_tokens": base_bal,
        "quote_balance_sol":   quote_bal,
        "decimals_base":       dec_base,
        "coin_creator": Pubkey.from_string(pool_keys["coin_creator"]),
        "lp_supply": int(pool_keys["lp_supply"]),  # critical
    }

    ok = await sdk.deposit(
        pool_data,
        base_amount_ui,
        keypair,
        SLIPPAGE_PCT,
        PRIORITY_FEE_SOL_PUMPSWAP,
        sol_cap_ui,
        True,   # debug_prints
    )
    print("add_pumpswap_liquidity success:", ok)
    return ok


async def cycle_once(async_client: AsyncClient, sdk: PumpSwap, keypair: Keypair):
    owner = keypair.pubkey()

    # collect fees: measure SOL delta 
    sol_before = await get_sol(async_client, owner)

    resp = collect_creator_fees()
    sig = extract_sig(resp)
    if not sig:
        print("collect fees: no signature, skipping cycle")
        return

    ok = await wait_for_sig(async_client, sig, timeout_s=90)
    if not ok:
        print("collect fees: tx not confirmed or failed, skipping cycle")
        return

    sol_after = await get_sol(async_client, owner)
    collected_lamports = max(sol_after - sol_before, 0)
    collected_sol = collected_lamports / 1e9
    print(f"Collected SOL delta: {collected_sol:.9f}")

    if collected_sol <= SOL_GAS_BUFFER:
        print("Collected too small; leaving it for next cycle.")
        return

    # buy - spend half of collected SOL minus buffer
    spend_sol = max((collected_sol * 0.5) - SOL_GAS_BUFFER, 0.0)
    if spend_sol <= 0:
        print("Buy spend computed <= 0; skipping buy.")
        return

    # measure token delta
    buy_mint_pk = Pubkey.from_string(BUY_MINT)
    base_ata, tok_before_raw, tok_dec = await get_token_ui(async_client, owner, buy_mint_pk, TOKEN_2022_PROGRAM_ID)

    buy_resp = buy_tokens(spend_sol)
    buy_sig = extract_sig(buy_resp)
    if not buy_sig:
        print("buy: no signature, skipping rest of cycle")
        return

    ok = await wait_for_sig(async_client, buy_sig, timeout_s=120)
    if not ok:
        print("buy: tx not confirmed or failed, skipping rest of cycle")
        return

    _, tok_after_raw, _ = await get_token_ui(async_client, owner, buy_mint_pk, TOKEN_2022_PROGRAM_ID)
    bought_raw = max(tok_after_raw - tok_before_raw, 0)
    bought_ui = bought_raw / (10 ** tok_dec)
    print(f"Bought token delta: {bought_ui:.9f} (raw {bought_raw})")

    if bought_raw == 0:
        print("No tokens bought; skipping LP.")
        return

    # add LP: deposit bought tokens, cap SOL spend safely 
    deposit_ui = bought_ui * (1.0 - TOKEN_DUST_BUFFER_PCT)

    sol_now = (await get_sol(async_client, owner)) / 1e9
    sol_cap = max(sol_now - SOL_CAP_BUFFER, 0.0)  # never drain wallet
    if sol_cap <= 0:
        print("sol_cap <= 0; skipping LP")
        return

    await add_pumpswap_liquidity(
        async_client=async_client,
        sdk=sdk,
        keypair=keypair,
        pool_address=POOL_ADDRESS,
        base_amount_ui=deposit_ui,
        sol_cap_ui=sol_cap,
    )


async def main_loop():
    if not RPC_URL:
        raise RuntimeError("HELIUS_RPC_URL missing")
    if not PRIVATE_KEY:
        raise RuntimeError("PRIVATE_KEY missing")
    if not API_KEY:
        raise RuntimeError("PUMP_SWAP_API_KEY missing")

    keypair = Keypair.from_base58_string(PRIVATE_KEY)
    async_client = AsyncClient(RPC_URL)
    sdk = PumpSwap(async_client)

    try:
        while True:
            print("\n loop start...")
            try:
                await cycle_once(async_client, sdk, keypair)
            except Exception as e:
                import traceback
                print("Cycle error:", e)
                traceback.print_exc()

            print(f"Sleeping {LOOP_SECONDS}s...\n")
            await asyncio.sleep(LOOP_SECONDS)
    finally:
        await async_client.close()


if __name__ == "__main__":
    asyncio.run(main_loop())

