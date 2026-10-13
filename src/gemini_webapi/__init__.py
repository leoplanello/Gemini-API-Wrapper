# flake8: noqa
import sys

if sys.platform.startswith('win'):
    try:
        from asyncio.proactor_events import _ProactorBasePipeTransport
        def silence_event_loop_closed(func):
            def wrapper(self, *args, **kwargs):
                try:
                    return func(self, *args, **kwargs)
                except ConnectionResetError as e:
                    if e.winerror == 10054:
                        pass
                    else:
                        raise
            return wrapper
        _ProactorBasePipeTransport._call_connection_lost = silence_event_loop_closed(
            _ProactorBasePipeTransport._call_connection_lost
        )
    except ImportError:
        pass


from .client import GeminiClient, ChatSession
from .exceptions import *
from .types import *
from .utils import set_log_level, logger
