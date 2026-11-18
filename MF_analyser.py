"""
Mutual Fund Portfolio Builder  v3
===================================
Sources: mfapi.in · AMFI NAVAll.txt · mfdata.in · yfinance (Nifty 50, Midcap)
Not SEBI-registered financial advice.

The implementation lives in the `mfa` package; this file is the entry point.

Run:  python MF_analyser.py
Deps: pip install -r requirements.txt
"""
from mfa.cli import main

if __name__ == "__main__":
    main()
