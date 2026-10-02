"""One lock shared by the heavy native libraries.

Windows OCR (a WinRT component) and Whisper (CTranslate2) are each fine on
their own, but starting them at the same moment in different threads crashes
the process with an access violation inside CTranslate2. Everything that calls
into either of them takes this lock, so they can never overlap. Each call is
short - an OCR read is milliseconds and a spoken question transcribes in under
a second - so the wait is not noticeable.
"""

import threading

LOCK = threading.RLock()
