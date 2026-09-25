from typing import Dict, List


class ChannelHistory:
    def __init__(self, max_turns: int = 20):
        self.max_turns = max_turns
        self._histories: Dict[int, List[Dict[str, str]]] = {}

    def get_history(self, channel_id: int) -> List[Dict[str, str]]:
        return list(self._histories.get(channel_id, []))

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
        if channel_id in self._histories:
            del self._histories[channel_id]
            return True
        return False
