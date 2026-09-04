"""A stand-in game that starts, prints, and exits cleanly.

Used by the supervisor integration test to prove that control returns to the
launcher when a child game finishes normally (acceptance criterion I4).
"""

import sys

print("fixture child running")
sys.exit(0)
