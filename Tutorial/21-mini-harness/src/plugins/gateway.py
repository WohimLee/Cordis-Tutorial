"""Stable request entry: routes every call to whichever provider is active now.

The gateway deliberately does not declare ``provider`` in ``inject``: that would
stop it from being available while the provider generation is being replaced.
It resolves the provider at call time and answers ``PENDING`` when there is none.
"""

inject = {"trace": {}}


class Gateway:
    """The service behind the ``requests`` name."""

    def __init__(self, context):
        self.context = context
        self.calls = 0

    def request(self, payload):
        self.calls += 1
        provider = self.context.get("provider")
        if provider is None:
            return "PENDING"
        return provider.complete(payload)


def plugin(context, config):
    trace = context.get("trace")
    gateway = Gateway(context)
    context.provide("requests", gateway)
    trace.add("activate:gateway")

    def cleanup():
        trace.add("cleanup:gateway")
        if isinstance(config, dict) and config.get("cleanup_error"):
            raise RuntimeError("gateway cleanup failed")

    return cleanup
