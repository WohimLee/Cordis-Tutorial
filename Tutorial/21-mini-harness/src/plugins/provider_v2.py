"""Provider generation v2: a different real module, not a mutated object.

Using a second file keeps the "module changed" event honest: the Loader sees a
different specifier, so the entry identity changes and the transaction is a
structural replacement rather than a config update.
"""

inject = {"gate": {}, "trace": {}}


class Config:
    """Same validation contract as v1, so only the behaviour differs."""

    @staticmethod
    def validate(value):
        if not isinstance(value, dict):
            raise TypeError("provider config must be a mapping")
        preamble = value.get("preamble")
        if not isinstance(preamble, str) or not preamble:
            raise ValueError("provider preamble must be a non-empty string")
        return {"preamble": preamble}


class Provider:
    """v2 uppercases the payload, which makes the active generation observable."""

    version = "v2"

    def __init__(self, preamble):
        self.preamble = preamble
        self.calls = 0

    def complete(self, payload):
        self.calls += 1
        return f"v2:{self.preamble}:{payload.upper()}"


async def plugin(context, config):
    await context.get("gate").wait()
    trace = context.get("trace")
    provider = Provider(config["preamble"])
    context.provide("provider", provider)
    trace.add("activate:provider")
    return lambda: trace.add("cleanup:provider")
