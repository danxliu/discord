from pathlib import Path


class UserMemory:
    def __init__(self, base_dir: str = "data/memory/users"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _file_path(self, user_id: int) -> Path:
        return self.base_dir / f"{user_id}.md"

    def read_memory(self, user_id: int) -> str:
        path = self._file_path(user_id)
        if not path.is_file():
            return "No previous memory stored for this user."
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception as e:
            return f"Error reading user memory: {str(e)}"

    def save_fact(
        self,
        user_id: int,
        username: str,
        fact: str,
        category: str = "Facts & Preferences",
    ) -> str:
        path = self._file_path(user_id)
        category_header = f"## {category}"
        fact_line = f"- {fact.strip()}"

        try:
            if not path.is_file():
                content = (
                    f"# Memory Profile: {username} (ID: {user_id})\n\n"
                    f"{category_header}\n{fact_line}\n"
                )
                path.write_text(content, encoding="utf-8")
                return f"Saved new memory: '{fact.strip()}' under '{category}'."

            content = path.read_text(encoding="utf-8")
            if category_header in content:
                parts = content.split(category_header, 1)
                after = parts[1]
                next_header_idx = after.find("\n## ")
                if next_header_idx != -1:
                    new_after = (
                        after[:next_header_idx]
                        + f"\n{fact_line}"
                        + after[next_header_idx:]
                    )
                else:
                    new_after = after.rstrip() + f"\n{fact_line}\n"
                new_content = parts[0] + category_header + new_after
            else:
                new_content = content.rstrip() + f"\n\n{category_header}\n{fact_line}\n"

            path.write_text(new_content, encoding="utf-8")
            return f"Saved memory: '{fact.strip()}' under '{category}'."
        except Exception as e:
            return f"Failed to save memory: {str(e)}"
