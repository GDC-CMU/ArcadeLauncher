"""A stand-in game that crashes, so the launcher's error path can be tested.

The message on stderr is what the supervisor should surface in its notice.
"""

import sys

print("fixture child failed on purpose", file=sys.stderr)
sys.exit(3)
