"""Read and rewrite a pica manifest (`key = value`, `#` comments, repeated keys).

Only what the tool needs is interpreted; everything else is preserved verbatim
so a rewrite (`set`) never reformats a hand-written file.
"""

import re

_LINE = re.compile(r"^\s*([A-Za-z0-9_.@]+)\s*=\s*(.*?)\s*$")

DEPENDENCY_KEYS = ("app", "base", "kmod")


class Manifest:
    def __init__(self, text, origin="manifest"):
        self.origin = origin
        self.lines = text.splitlines()
        self.problems = []
        self.entries = []  # (line index, key, value)
        for index, raw in enumerate(self.lines):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = _LINE.match(raw)
            if not match:
                self.problems.append(f"line {index + 1} is not `key = value`: {stripped!r}")
                continue
            self.entries.append((index, match.group(1), match.group(2)))

    @classmethod
    def read(cls, file):
        with open(file, "r", encoding="utf-8") as handle:
            return cls(handle.read(), file)

    # --- reading ---------------------------------------------------------

    def keys(self):
        return [key for _, key, _ in self.entries]

    def has(self, key):
        return any(k == key for _, k, _ in self.entries)

    def get(self, key, default=""):
        for _, k, value in self.entries:
            if k == key:
                return value
        return default

    def get_all(self, key):
        return [value for _, k, value in self.entries if k == key]

    def targets(self):
        found = []
        for value in self.get_all("targets"):
            for part in value.split(","):
                part = part.strip()
                if part and part not in found:
                    found.append(part)
        return found

    def dependencies(self, backend):
        """`app`/`base`/`kmod` as `backend` reads them: `<key>@<backend>` replaces `<key>`."""
        result = {}
        for key in DEPENDENCY_KEYS:
            scoped = f"{key}@{backend}"
            values = self.get_all(scoped) if self.has(scoped) else self.get_all(key)
            result[key] = [value for value in values if value.strip()]
        return result

    def declared_names(self):
        """Every package name any backend may install, constraints and `?` removed."""
        names = set()
        for _, key, value in self.entries:
            base = key.split("@", 1)[0]
            if base in DEPENDENCY_KEYS and value.strip():
                names.add(dependency_name(value))
        return names

    def i18n_patterns(self):
        patterns = []
        for template in self.get_all("app_i18n"):
            escaped = re.escape(template).replace(re.escape("{lang}"), "[A-Za-z0-9_-]+")
            patterns.append(re.compile(f"^{escaped}$"))
        return patterns

    def declares(self, name):
        if name in self.declared_names():
            return True
        return any(pattern.match(name) for pattern in self.i18n_patterns())

    # --- writing ---------------------------------------------------------

    def text(self):
        return "\n".join(self.lines) + "\n"

    def set(self, key, value):
        """Replace the first `key` line (or append one), dropping its repeats."""
        indexes = [index for index, k, _ in self.entries if k == key]
        if indexes:
            self.lines[indexes[0]] = f"{key} = {value}"
            for extra in reversed(indexes[1:]):
                del self.lines[extra]
        else:
            self.lines.append(f"{key} = {value}")
        self.__init__(self.text(), self.origin)

    def remove(self, key):
        for index in reversed([index for index, k, _ in self.entries if k == key]):
            del self.lines[index]
        self.__init__(self.text(), self.origin)


def dependency_name(value):
    """`curl>=7.0?` -> `curl`."""
    value = value.strip().rstrip("?").strip()
    return re.split(r"[<>=≥≤＜＞＝]", value, maxsplit=1)[0].strip()
