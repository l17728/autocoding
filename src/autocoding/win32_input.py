"""ConPTY's negotiated KEY_EVENT_RECORD wire format (Microsoft spec 4999)."""
KEYS={'enter':(13,13,0),'tab':(9,9,0),'backspace':(8,8,0),'escape':(27,27,0),
      'up':(38,0,256),'down':(40,0,256),'left':(37,0,256),'right':(39,0,256),
      'home':(36,0,256),'end':(35,0,256),'delete':(46,0,256),
      'page_up':(33,0,256),'page_down':(34,0,256),'ctrl_c':(67,3,8),'ctrl_p':(80,16,8)}

def key(vk,uc,mod=0):
    return ''.join(f'\x1b[{vk};0;{uc};{down};{mod};1_' for down in (1,0))

def text(value,*,paste=False):
    result=[]
    for ch in value:
        if ch in '\r\n':
            result.append(key(13,13,16 if paste else 0))
        elif ch=='\t':
            result.append(key(9,9))
        else:
            units=ch.encode('utf-16-le')
            vk=ord(ch.upper()) if ch.isascii() and ch.isalnum() else 0
            for index in range(0,len(units),2):
                uc=int.from_bytes(units[index:index+2],'little')
                if len(units)==4:
                    # Crossterm combines consecutive UTF-16 surrogate events
                    # before filtering releases. H-down,H-up,L-down,L-up loses
                    # both pairs; releasing a synthetic pair can duplicate it.
                    # Text packets have no physical key held: emit H/L once.
                    result.append(f'\x1b[0;0;{uc};1;0;1_')
                else:
                    result.append(key(vk,uc))
    return ''.join(result)
