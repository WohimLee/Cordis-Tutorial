"""Provider generation v1: the first implementation behind the ``provider`` name."""

inject = {"gate": {}, "trace": {}}


class Config:
    """A provider without a non-empty preamble is not a valid candidate."""

    @staticmethod
    def validate(value):
        if not isinstance(value, dict):
            raise TypeError("provider config must be a mapping")
        preamble = value.get("preamble")
        if not isinstance(preamble, str) or not preamble:
            raise ValueError("provider preamble must be a non-empty string")
        return {"preamble": preamble}


class Provider:
    """Version-tagged service; ``complete`` is the only capability consumers use."""

    version = "v1"

    def __init__(self, preamble):
        self.preamble = preamble
        self.calls = 0

    def complete(self, payload):
        self.calls += 1
        return f"v1:{self.preamble}:{payload}"


async def plugin(context, config):
    await context.get("gate").wait()
    trace = context.get("trace")
    provider = Provider(config["preamble"])
    context.provide("provider", provider)
    trace.add("activate:provider")
    return lambda: trace.add("cleanup:provider")
