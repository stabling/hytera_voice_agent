"""
L2: Frame codec - binary frame encode/decode (protocol v0, fragment-capable).

Frame format (big-endian on wire):
    f=0: [Fixed Header 2B] + [Payload: Entry x N]
    f=1: [Fixed Header 2B] + [Fragment Header 8B] + [frag data]  (see fragment.py)

    Fixed header byte0: [ver:3 | t:2 | enc:2 | f:1]
    Fixed header byte1: [rp:2 | reserve:6]
    Entry: seq_id(u32 BE) + opcode(u32 BE) + params_len(u16 BE) + params(bytes)
"""

import struct
from collections import namedtuple

# -- Message types (header t field) --
MSG_TYPE_REQUEST = 0
MSG_TYPE_RESPONSE = 1
MSG_TYPE_EVENT = 2

# -- Frame constants --
FIXED_HEADER_SIZE = 2
FRAGMENT_HEADER_SIZE = 8
ENTRY_FIXED_SIZE = 10        # seq_id(4) + opcode(4) + params_len(2)
ATOMIC_MTU_BYTES = 2048      # hard cap of one payload, both sides


class FrameFormatError(ValueError):
    """Malformed payload: trailing bytes / truncated entry / bad length."""


# -- Header bit manipulation (byte0: ver|t|enc|f, byte1: rp|reserve) --

def makeHeader(ver, msgType, enc, frag, reply):
    """Pack the 2-byte fixed header. Reserve bits are zero."""
    b0 = ((ver & 0x07) << 5) | ((msgType & 0x03) << 3) \
        | ((enc & 0x03) << 1) | (frag & 0x01)
    b1 = (reply & 0x03) << 6
    return bytes([b0, b1])


def frameVer(header):
    return (header[0] >> 5) & 0x07


def frameType(header):
    return (header[0] >> 3) & 0x03


def frameEnc(header):
    return (header[0] >> 1) & 0x03


def frameFrag(header):
    return header[0] & 0x01


def frameReply(header):
    return (header[1] >> 6) & 0x03


# -- Frame entry --
FrameEntry = namedtuple('FrameEntry', ['seq_id', 'opcode', 'params'])


def encodePayload(entries):
    """Encode payload only (no fixed header). Input for the fragmenter."""
    body = bytearray()
    for item in entries:
        if isinstance(item, FrameEntry):
            seqId, opcode, params = item.seq_id, item.opcode, item.params
        else:
            seqId, opcode, params = item['seq_id'], item['opcode'], item['params']
        paramsBytes = bytes(params)
        if len(paramsBytes) > 0xFFFF:
            raise FrameFormatError('params too long: %d' % len(paramsBytes))
        body += struct.pack('>IIH', seqId, opcode, len(paramsBytes))
        body += paramsBytes
    return bytes(body)


def encodeFrame(entries, ver=0, msgType=MSG_TYPE_REQUEST, enc=0, reply=0):
    """Encode one complete frame (f=0). No mtu check here."""
    return makeHeader(ver, msgType, enc, 0, reply) + encodePayload(entries)


def decodePayload(payload):
    """Decode payload bytes (no header) into entry dicts.

    Raises FrameFormatError on residual bytes or truncated entry.
    """
    if not isinstance(payload, (bytes, bytearray)):
        raise FrameFormatError('payload must be bytes')
    payload = bytes(payload)
    entries = []
    pos = 0
    total = len(payload)
    while pos < total:
        if pos + ENTRY_FIXED_SIZE > total:
            raise FrameFormatError(
                'truncated entry header at %d (total %d)' % (pos, total))
        seqId, opcode, paramsLen = struct.unpack_from('>IIH', payload, pos)
        pos += ENTRY_FIXED_SIZE
        if pos + paramsLen > total:
            raise FrameFormatError(
                'truncated params at %d (need %d, total %d)'
                % (pos, paramsLen, total))
        entries.append({
            'seq_id': seqId,
            'opcode': opcode,
            'params_len': paramsLen,
            'params': payload[pos:pos + paramsLen],
        })
        pos += paramsLen
    return entries


def decodeFrame(wireBytes):
    """Decode one complete (f=0) frame.

    Returns dict {ver, type, enc, reply, frag, entries} or None if too short.
    Raises FrameFormatError on malformed payload.
    """
    wireBytes = bytes(wireBytes)
    if len(wireBytes) < FIXED_HEADER_SIZE:
        return None
    header = wireBytes[:FIXED_HEADER_SIZE]
    return {
        'ver': frameVer(header),
        'type': frameType(header),
        'enc': frameEnc(header),
        'frag': frameFrag(header),
        'reply': frameReply(header),
        'entries': decodePayload(wireBytes[FIXED_HEADER_SIZE:]),
    }
