"""
Registers a linked signer for the Nado wallet.
Run: python3 register_nado_signer.py
Enter the MAIN wallet private key when prompted (input is hidden).
"""
import getpass
import os
from dotenv import load_dotenv

load_dotenv()

LINKED_SIGNER_KEY = os.getenv("NADO_PRIVATE_KEY")   # our bot's signing key
WALLET_ADDRESS    = os.getenv("NADO_WALLET_ADDRESS") # main wallet

print(f"Wallet:       {WALLET_ADDRESS}")
print(f"Linked signer key: {LINKED_SIGNER_KEY[:6]}...{LINKED_SIGNER_KEY[-4:]}")
print()
print("Registering linked signer on Nado (Ink L2)...")
print("This requires signing with your MAIN wallet.")
print()

main_key = getpass.getpass("Paste MAIN wallet private key (hidden): ").strip()

if not main_key:
    print("ERROR: no key entered")
    exit(1)

if not main_key.startswith("0x"):
    main_key = "0x" + main_key

try:
    from eth_account import Account
    from nado_protocol.client import NadoClientMode, create_nado_client
    from nado_protocol.engine_client.types.execute import LinkSignerParams
    from nado_protocol.utils.subaccount import SubaccountParams

    # Derive addresses
    main_account   = Account.from_key(main_key)
    linked_account = Account.from_key(LINKED_SIGNER_KEY)

    print(f"\nMain wallet address:   {main_account.address}")
    print(f"Linked signer address: {linked_account.address}")

    if main_account.address.lower() != WALLET_ADDRESS.lower():
        print(f"\nWARNING: key address ({main_account.address}) != NADO_WALLET_ADDRESS ({WALLET_ADDRESS})")
        confirm = input("Continue anyway? [y/N]: ").strip().lower()
        if confirm != "y":
            exit(0)

    print("\nConnecting to Nado...")
    client = create_nado_client(
        mode=NadoClientMode.MAINNET,
        signer=main_account,
    )

    print("Sending link_signer transaction...")
    sender_sub = SubaccountParams(
        subaccount_owner=main_account.address,
        subaccount_name="default",
    )
    signer_sub = SubaccountParams(
        subaccount_owner=linked_account.address,
        subaccount_name="default",
    )
    params = LinkSignerParams(sender=sender_sub, signer=signer_sub)
    result = client.subaccount.link_signer(params)
    print(f"\nResult: {result}")

    if getattr(result, "status", None) == "success" or "success" in str(result).lower():
        print("\n✅ Linked signer registered! Bot can now place orders on Nado.")
    else:
        print(f"\n❌ Unexpected result: {result}")

except Exception as e:
    print(f"\nERROR: {e}")
    import traceback
    traceback.print_exc()
