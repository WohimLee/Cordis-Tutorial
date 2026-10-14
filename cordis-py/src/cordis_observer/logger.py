"""In-memory exporter for studying Cordis lifecycle and call chains."""

from cordis import Logger, Message


class MemoryExporter:
    """Capture structured messages and expose their formatted text."""

    colors = False
    maxLength = 10240

    def __init__(self) -> None:
        self.levels: dict[str, int] = {}
        self.formatters: dict[str, object] = {}
        self.messages: list[Message] = []

    def export(self, message: Message) -> None:
        self.messages.append(message)

    @property
    def texts(self) -> list[str]:
        return [Logger.format(self, message) for message in self.messages]
