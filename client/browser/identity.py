"""Stable bundled unpacked-extension identity from the manifest public key.

This pins browser-enforced Origin, not a signature of unpacked source code.
Someone able to install modified unpacked source with the public key can
impersonate it. Local opt-in is still required; store identities need review.
"""

BUNDLED_EXTENSION_ID = "loddgfjblefkjnjddbojblmekfhkfoil"
