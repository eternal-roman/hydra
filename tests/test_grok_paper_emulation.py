"""Offline checks for the reusable Grok paper-session emulation.

Live prepare/apply stays a manual session: it reads Kraken and injects
decisions. This test never does either.
"""
from tools.grok_paper_emulation import self_check


def test_paper_emulation_self_check():
    self_check()
