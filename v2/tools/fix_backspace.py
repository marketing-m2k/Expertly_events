"""Repair helper: a regex written through a shell heredoc lost its backslash in '\\b' and became a literal BACKSPACE
character (0x08). Turn every 0x08 in the given files back into the two characters backslash + b."""
import sys

for p in sys.argv[1:]:
    s = open(p, encoding="utf-8").read()
    n = s.count(chr(8))
    open(p, "w", encoding="utf-8").write(s.replace(chr(8), chr(92) + "b"))
    print(p, "replaced", n)
