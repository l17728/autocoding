"""Finish CPython Proactor socket cleanup after a peer-reset shutdown error.

Scoped to this application's loop; never patches asyncio or hides read/write
failures. CPython 3.12 calls protocol.connection_lost before shutdown(), whose
WinError 10054 can otherwise skip close(), server detach, and the closed flag.
"""
import asyncio
import logging
import os


def recover_reset(context):
    from asyncio.proactor_events import _ProactorBasePipeTransport
    error = context.get('exception')
    callback = getattr(context.get('handle'), '_callback', None)
    transport = getattr(callback, '__self__', None)
    if (not isinstance(error, ConnectionResetError)
            or getattr(error, 'winerror', None) != 10054
            or getattr(callback, '__func__', None) is not _ProactorBasePipeTransport._call_connection_lost
            or not isinstance(transport, _ProactorBasePipeTransport)
            or transport._called_connection_lost):
        return False
    import linecache
    tb = error.__traceback__
    while tb and tb.tb_frame.f_code is not _ProactorBasePipeTransport._call_connection_lost.__code__:
        tb = tb.tb_next
    # Match the exact failing operation, not merely an exception type. A
    # protocol callback or close() raising the same error must be delegated.
    if not tb or '.shutdown(' not in linecache.getline(tb.tb_frame.f_code.co_filename, tb.tb_lineno):
        return False
    sock = transport._sock
    if sock is None:
        return False
    sock.close()
    transport._sock = None
    if transport._server is not None:
        transport._server._detach()
        transport._server = None
    transport._called_connection_lost = True
    return True


def install(loop=None):
    """Return a restoration callback; leave non-Windows loops untouched."""
    if os.name != 'nt':
        return lambda: None
    loop = loop or asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    def handler(current_loop, context):
        try:
            if recover_reset(context):
                logging.getLogger(__name__).info('Peer reset during socket shutdown; transport released (10054)')
                return
        except Exception:
            logging.getLogger(__name__).exception('Socket shutdown cleanup failed')
        if previous:
            previous(current_loop, context)
        else:
            current_loop.default_exception_handler(context)
    loop.set_exception_handler(handler)
    def restore():
        if loop.get_exception_handler() is handler:
            loop.set_exception_handler(previous)
    return restore
