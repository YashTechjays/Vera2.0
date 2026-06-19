"""Print a QR code from an otpauth:// URI for local MFA testing.

Usage:
    uv run python scripts/mfa_qr.py 'otpauth://totp/Vera:you@example.com?secret=...&issuer=Vera'
    just mfa-qr 'otpauth://totp/...'
"""

import sys

import pyotp
import qrcode


def main() -> None:
    if len(sys.argv) != 2 or not sys.argv[1].startswith("otpauth://"):
        print(
            "Usage: mfa_qr.py 'otpauth://totp/...'\n"
            "Pass the provisioning_uri returned by POST /auth/mfa/enroll.",
            file=sys.stderr,
        )
        sys.exit(1)

    uri = sys.argv[1]
    totp = pyotp.parse_uri(uri)

    qr = qrcode.QRCode()
    qr.add_data(uri)
    qr.make(fit=True)
    qr.print_ascii(invert=True)

    print(f"\nAccount : {totp.name}")
    print(f"Issuer  : {totp.issuer}")
    print(f"Secret  : {totp.secret}")
    print(f"Current code: {totp.now()}")


if __name__ == "__main__":
    main()
