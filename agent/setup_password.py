"""
setup_password.py
==================
Run this ONCE, as Administrator, after installing the unlock service.
Encrypts your Windows login password with DPAPI (machine scope) and
writes it to unlock_secret.bin, which unlock_helper.exe reads at
unlock time. The password is never stored in plaintext anywhere.

    pip install pywin32
    python setup_password.py

Re-run this any time you change your Windows password.
"""

import getpass
import os

import win32crypt

PASSWORD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "unlock_secret.bin")

# Flag 4 = CRYPTPROTECT_LOCAL_MACHINE: decryptable by any SYSTEM process
# on this machine, not tied to a specific user profile (needed because
# unlock_helper.exe runs as SYSTEM, not as you).
CRYPTPROTECT_LOCAL_MACHINE = 4


def main() -> None:
    pw = getpass.getpass("Enter your Windows login password (input hidden): ")
    if not pw:
        print("No password entered, aborting.")
        return

    blob = win32crypt.CryptProtectData(
        pw.encode("utf-8"), "ConnectUnlockSecret", None, None, None, CRYPTPROTECT_LOCAL_MACHINE
    )
    with open(PASSWORD_FILE, "wb") as f:
        f.write(blob)

    print(f"Encrypted password written to {PASSWORD_FILE}")
    print("Keep this folder secure — anyone with SYSTEM/Administrator access")
    print("on this machine could decrypt it, same as they could reset your")
    print("Windows password outright.")


if __name__ == "__main__":
    main()
