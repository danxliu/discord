from datetime import UTC, datetime


class ChannelHistory:
    def __init__(self, max_turns: int = 20):
        self.max_turns = max_turns
        self._histories: dict[int, list[dict[str, str]]] = {}
        self._cleared_at: dict[int, datetime] = {}

    def get_history(self, channel_id: int) -> list[dict[str, str]]:
        return [turn.copy() for turn in self._histories.get(channel_id, [])]

    def add_turn(self, channel_id: int, role: str, content: str) -> None:
        if not content:
            return
        if channel_id not in self._histories:
            self._histories[channel_id] = []

        self._histories[channel_id].append({"role": role, "content": content})

        if len(self._histories[channel_id]) > self.max_turns * 2:
            self._histories[channel_id] = self._histories[channel_id][
                -self.max_turns * 2 :
            ]

    def clear(self, channel_id: int) -> bool:
        self._cleared_at[channel_id] = datetime.now(UTC)
        if channel_id in self._histories:
            del self._histories[channel_id]
            return True
        return False

    def get_cleared_at(self, channel_id: int) -> datetime | None:
        return self._cleared_at.get(channel_id)
