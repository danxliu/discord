from pathlib import Path


class UserMemory:
    def __init__(self, base_dir: str = "data/memory/users"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _file_path(self, user_id: int) -> Path:
        return self.base_dir / f"{user_id}.md"

    @staticmethod
    def _normalize_note(fact: str, category: str) -> tuple[str, str] | str:
        note = fact.strip()
        section = category.strip()
        if not note:
            return "note cannot be blank"
        if "\n" in note or "\r" in note:
            return "note must be a single line"
        if not section or "\n" in section or "\r" in section:
            return "category must be a non-blank single line"
        return note, section

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
        normalized = self._normalize_note(fact, category)
        if isinstance(normalized, str):
            return f"Failed to save memory: {normalized}."
        note, section = normalized
        path = self._file_path(user_id)
        category_header = f"## {section}"
        fact_line = f"- {note}"

        try:
            if not path.is_file():
                content = (
                    f"# Memory Profile: {username} (ID: {user_id})\n\n"
                    f"{category_header}\n{fact_line}\n"
                )
                path.write_text(content, encoding="utf-8")
                return f"Saved new memory: '{note}' under '{section}'."

            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            section_start = next(
                (
                    i
                    for i, line in enumerate(lines)
                    if line.rstrip("\r\n") == category_header
                ),
                None,
            )
            if section_start is not None:
                section_end = next(
                    (
                        i
                        for i in range(section_start + 1, len(lines))
                        if lines[i].startswith("## ")
                    ),
                    len(lines),
                )
                if any(
                    line.strip() == fact_line
                    for line in lines[section_start + 1 : section_end]
                ):
                    return f"Memory already exists: '{note}' under '{section}'."
                insertion = section_end
                if insertion == len(lines):
                    if lines and not lines[-1].endswith("\n"):
                        lines[-1] += "\n"
                    lines.append(f"{fact_line}\n")
                else:
                    lines.insert(insertion, f"{fact_line}\n")
            else:
                content = "".join(lines).rstrip()
                prefix = f"{content}\n\n" if content else ""
                lines = [f"{prefix}{category_header}\n{fact_line}\n"]

            path.write_text("".join(lines), encoding="utf-8")
            return f"Saved memory: '{note}' under '{section}'."
        except Exception as e:
            return f"Failed to save memory: {str(e)}"

    def remove_fact(
        self,
        user_id: int,
        fact: str,
        category: str = "Facts & Preferences",
    ) -> str:
        normalized = self._normalize_note(fact, category)
        if isinstance(normalized, str):
            return f"Failed to remove memory: {normalized}."
        note, section = normalized
        path = self._file_path(user_id)
        if not path.is_file():
            return f"No matching memory found under '{section}'."

        category_header = f"## {section}"
        fact_line = f"- {note}"
        try:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            section_start = next(
                (
                    i
                    for i, line in enumerate(lines)
                    if line.rstrip("\r\n") == category_header
                ),
                None,
            )
            if section_start is None:
                return f"No matching memory found under '{section}'."
            section_end = next(
                (
                    i
                    for i in range(section_start + 1, len(lines))
                    if lines[i].startswith("## ")
                ),
                len(lines),
            )
            matches = [
                i
                for i in range(section_start + 1, section_end)
                if lines[i].strip() == fact_line
            ]
            if not matches:
                return f"No matching memory found under '{section}'."

            match_set = set(matches)
            lines = [line for i, line in enumerate(lines) if i not in match_set]
            path.write_text("".join(lines), encoding="utf-8")
            label = "note" if len(matches) == 1 else "notes"
            return (
                f"Removed {len(matches)} matching {label}: '{note}' from '{section}'."
            )
        except Exception as e:
            return f"Failed to remove memory: {str(e)}"
