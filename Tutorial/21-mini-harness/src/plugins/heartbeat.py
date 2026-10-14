"""Periodic heartbeat driven by the harness TimerService.

The interval handle belongs to this plugin's fiber: unloading the plugin cancels
the interval before the callback can run again.
"""

inject = {"timer": {}, "events": {}, "trace": {}}


class Config:
    """Validated before every activation."""

    @staticmethod
    def validate(value):
        if not isinstance(value, dict):
            raise TypeError("heartbeat config must be a mapping")
        period = value.get("period", 1.0)
        if (
            isinstance(period, bool)
            or not isinstance(period, (int, float))
            or period <= 0
        ):
            raise ValueError("heartbeat period must be a positive number")
        return {"period": float(period)}


class Heartbeat:
    """Counter plus event source; both are observable from the harness."""

    def __init__(self, period, events):
        self.period = period
        self.events = events
        self.ticks = 0

    def tick(self):
        self.ticks += 1
        self.events.emit("heartbeat", self)


def plugin(context, config):
    timer = context.get("timer")
    events = context.get("events")
    trace = context.get("trace")
    heartbeat = Heartbeat(config["period"], events)
    timer.interval(context, heartbeat.period, heartbeat.tick, label="heartbeat")
    context.provide("heartbeat", heartbeat)
    trace.add("activate:heartbeat")
    return lambda: trace.add("cleanup:heartbeat")
