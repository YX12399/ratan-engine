#!/usr/bin/env python3
"""Generate a 256-bit encryption key for HyperAgg tunnel."""
import base64
import os

key = os.urandom(32)
b64 = base64.b64encode(key).decode()

print("HyperAgg Encryption Key (Base64-encoded 256-bit)")
print("=" * 50)
print(f"\n  {b64}\n")
print("Add this to your config.yaml:")
print(f'  encryption_key: "{b64}"')
print()
print("Or set as environment variable:")
print(f"  export HYPERAGG_KEY='{b64}'")
