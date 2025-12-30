# THICC

### THICC is an automated Solana liquidity management bot designed for pump.fun / PumpSwap AMM creator tokens.

It runs a continuous strategy loop that:

- Collects creator fees (SOL)
- Buys the token using a portion of the collected SOL
- Adds the purchased tokens + proportional SOL back into the PumpSwap liquidity pool

This cycle repeats automatically every 30 minutes with no manual interaction.

## External Services 
- PumpPortal API, used for collecting pump.fun creator fees and buying tokens
- pumpswapamm python package
- Helius RPC (recommended)

## Configuration 
Create a .env and populate with the following vars:
- HELIUS_RPC_URL (or your preferred rpc provider url)
- PRIVATE_KEY (your pump.fun creator wallet priv key)
- PUMP_SWAP_API_KEY (via https://pumpportal.fun)

Also be sure to populate the following variables in THICC.py to your choosing. In the script uploaded here, they are example values:
- CREATOR_FEE_MINT = "token_address_here_from_which_you_are_collecting_fees"  # fees claim mint
- BUY_MINT = "token_address"        # token to buy
- POOL_ADDRESS = "pool_address"     # pumpswap pool

## Disclaimer 
This software is in beta and interacts with live Solana programs to move real funds. Use at your own risk. 
