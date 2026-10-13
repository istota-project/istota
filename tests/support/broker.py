"""The credential broker's defaults from before it shipped on.

A test that drives the vault, OTP or recovery paths with the broker off says
so through this, so its assertions keep the meaning they were written with.
A test about the broker sets the switches itself.
"""


def broker_off(config):
    config.security.credential_broker.enabled = False
    config.security.credential_broker.enforce_reveal = False
    return config
