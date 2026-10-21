"""Repair a narrowly identified Windows UTF-8/GBK display mismatch."""

def readable_text(value):
    if not isinstance(value, str):
        return value
    # Normal Chinese is not touched. Private-use glyphs are a characteristic
    # of UTF-8 bytes decoded as GBK in captured terminal output.
    if not any('\ue000' <= char <= '\uf8ff' for char in value):
        return value
    try:
        candidate=value.encode('gbk').decode('utf-8')
    except UnicodeError:
        return value
    return candidate if any('\u4e00' <= char <= '\u9fff' for char in candidate) else value
