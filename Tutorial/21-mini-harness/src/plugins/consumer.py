"""Consumer / agent loop: it only starts once a provider and a heartbeat exist.

The declaration order in ``config/`` puts the consumer before the provider, so
this file is the proof that activation order comes from the dependency scheduler
and not from a manual sort.
"""

inject = {"provider": {}, "heartbeat": {}, "events": {}, "trace": {}}


class AgentLoop:
    """The consumer-side epoch: a new instance per activation."""

    def __init__(self, provider):
        self.provider = provider
        self.ticks = 0
        self.runs = 0

    def run(self, payload):
        self.runs += 1
        return self.provider.complete(payload)

    def on_tick(self, beat):
        self.ticks += 1


def plugin(context, config):
    trace = context.get("trace")
    provider = context.get("provider")
    agent = AgentLoop(provider)
    context.on("heartbeat", agent.on_tick)
    context.provide("agent", agent)
    trace.add("activate:consumer")
    return lambda: trace.add("cleanup:consumer")
