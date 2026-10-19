"""Keep child Codex processes independent of the invoking agent lifecycle."""
import os


def child_environment(env=None):
    child=dict(os.environ if env is None else env)
    for name in ('CODEX_THREAD_ID','CODEX_SESSION_ID','CODEX_TOOL_CALL_ID',
                 'CODEX_DAEMON_SHUTDOWN_SOCKET','CODEX_DAEMON_SHUTDOWN_FILE','CODEX_CI'):
        child.pop(name,None)
    return child
