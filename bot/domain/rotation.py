"""Compare-and-swap only the rotation array, preserving unrelated config text."""
import re

SECTION = re.compile(r'(?ms)^(\[/Script/WDGame\.WDServerMapRotationSettings\][^\n]*\n)(.*?)(?=^\[|\Z)')
ENTRY = re.compile(r'^\s*[.+]RotationEntries\s*=')
CLEAR = re.compile(r'^\s*!RotationEntries\s*=')


class RotationConflict(RuntimeError):
    pass


def entries(text):
    match = SECTION.search(text)
    if not match:
        raise RotationConflict('Rotation section missing')
    return [line.strip() for line in match[2].splitlines() if ENTRY.match(line)]


def replace_entries(text, expected, desired):
    current = entries(text)
    if current == desired:
        return text
    if current != expected:
        raise RotationConflict('Rotation was changed externally')
    match = SECTION.search(text)
    newline = '\r\n' if '\r\n' in match[0] else '\n'
    other = [line for line in match[2].splitlines() if not ENTRY.match(line) and not CLEAR.match(line)]
    body = newline.join(other + ['!RotationEntries=ClearArray'] + desired) + newline
    return text[:match.start(2)] + body + text[match.end(2):]


def format_entry(option):
    return f'.RotationEntries=(Map="{option["Map"]}",Experience="{option["Experience"]}",Lighting="{option["Lighting"]}")'
