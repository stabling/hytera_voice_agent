"""
L2.5: Fragmentation / reassembly (protocol v0).

Fragment Header (8B, big-endian, present when fixed-header f=1):
    transfer_id(2B) | total_len(2B) | offset(2B) | index(1B) | count(1B)

Rules (spec 2026-08-28 protocol-fragment-redesign-design.md, section 2):
    fragMax  = mtu - 10        (2B fixed header + 8B fragment header)
    single   : 2 + payloadLen <= mtu  -> one f=0 frame
    count    = ceil(payloadLen / fragMax), 2 <= count <= 255
    transfer_id: module counter 1..65535 wrap, 0 reserved
"""

import struct

from .frame import (
    makeHeader, frameFrag,
    FIXED_HEADER_SIZE, FRAGMENT_HEADER_SIZE, ATOMIC_MTU_BYTES,
    FrameFormatError,
)


class MessageTooLargeError(ValueError):
    """Payload exceeds ATOMIC_MTU_BYTES or count would exceed 255."""


class FrameIncompleteError(ValueError):
    """Fragment group not fully received yet."""


_transferId = 0


def nextTransferId():
    global _transferId
    _transferId = _transferId % 65535 + 1
    return _transferId


def _fragMax(mtu):
    return mtu - FIXED_HEADER_SIZE - FRAGMENT_HEADER_SIZE


def fragmentPayload(payload, ver, msgType, enc, reply, mtu, transferId=None):
    """payload -> list of wire frames (1 element if it fits, else f=1 frags)."""
    payload = bytes(payload)
    if len(payload) > ATOMIC_MTU_BYTES:
        raise MessageTooLargeError(
            'payload %d > ATOMIC_MTU_BYTES %d' % (len(payload), ATOMIC_MTU_BYTES))
    if mtu is None:
        mtu = 0
    if mtu <= 0:
        # no-fragmentation mode: single frame (2048 cap enforced above)
        return [makeHeader(ver, msgType, enc, 0, reply) + payload]
    if mtu < 16:
        raise MessageTooLargeError('mtu too small: %d (min 16)' % mtu)
    if mtu > ATOMIC_MTU_BYTES:
        # F-M4: upper bound mirrors atomicApiInit [16, 2048] (C++ side)
        raise MessageTooLargeError(
            'mtu too large: %d (max %d)' % (mtu, ATOMIC_MTU_BYTES))
    if FIXED_HEADER_SIZE + len(payload) <= mtu:
        return [makeHeader(ver, msgType, enc, 0, reply) + payload]

    capacity = _fragMax(mtu)
    totalLen = len(payload)
    count = (totalLen + capacity - 1) // capacity
    if count > 255:
        raise MessageTooLargeError(
            'fragment count %d > 255 (mtu=%d, payload=%d)'
            % (count, mtu, totalLen))
    tid = nextTransferId() if transferId is None else transferId
    if tid == 0:
        raise MessageTooLargeError('transfer_id 0 is reserved')

    frames = []
    for index in range(count):
        offset = index * capacity
        chunk = payload[offset:offset + capacity]
        header = makeHeader(ver, msgType, enc, 1, reply)
        fragHeader = struct.pack('>HHHBB', tid, totalLen, offset, index, count)
        frames.append(header + fragHeader + chunk)
    return frames


def parseFragmentHeader(frame):
    """Parse the 8B fragment header of an f=1 frame."""
    frame = bytes(frame)
    if len(frame) < FIXED_HEADER_SIZE or frameFrag(frame[:2]) != 1:
        raise FrameFormatError('not a fragment frame')
    if len(frame) < FIXED_HEADER_SIZE + FRAGMENT_HEADER_SIZE:
        raise FrameFormatError('fragment header truncated')
    transferId, totalLen, offset, index, count = struct.unpack_from(
        '>HHHBB', frame, FIXED_HEADER_SIZE)
    return transferId, totalLen, offset, index, count


def _groupKey(frame):
    """(transfer_id, count) of an f=1 frame; None-ish for f=0."""
    frame = bytes(frame)
    if len(frame) < FIXED_HEADER_SIZE:
        raise FrameFormatError('frame shorter than fixed header')
    if frameFrag(frame[:2]) == 0:
        return None
    transferId, _, _, _, count = parseFragmentHeader(frame)
    return transferId, count


def frameIsComplete(frameList):
    """True when frameList holds one complete PDU (single f=0 or full f=1 group)."""
    if not frameList:
        return False
    keys = set()
    for frame in frameList:
        key = _groupKey(bytes(frame))
        if key is None:
            return True                      # any f=0 frame is self-contained
        keys.add(key)
    if len(keys) != 1:
        return False                         # mixed transfers: keep buffering
    transferId, count = keys.pop()
    received = set()
    for frame in frameList:
        _, _, _, index, _ = parseFragmentHeader(frame)
        received.add(index)
    return len(received) == count


def reassemble(frameList):
    """Join a complete group into the original payload bytes."""
    header, payload = reassembleWithHeader(frameList)
    return payload


def reassembleWithHeader(frameList):
    """-> (fixed header 2B, payload bytes). Raises on incomplete/malformed."""
    if not frameList:
        raise FrameIncompleteError('empty frame list')
    frames = [bytes(f) for f in frameList]
    header = frames[0][:FIXED_HEADER_SIZE]
    if frameFrag(header) == 0:
        if len(frames) != 1:
            raise FrameFormatError('f=0 frame must be the only element')
        return header, frames[0][FIXED_HEADER_SIZE:]

    if not frameIsComplete(frames):
        raise FrameIncompleteError('fragment group not complete')

    transferId, totalLen, groupCount = None, None, None
    chunks = {}
    for frame in frames:
        tid, tLen, offset, index, count = parseFragmentHeader(frame)
        if transferId is None:
            transferId, totalLen, groupCount = tid, tLen, count
            if totalLen > ATOMIC_MTU_BYTES:
                # F-I2: mirror of the C++ first-fragment check
                # (atomic_api.cpp:421-423): total_len > 2048 -> reject
                # the whole group (docs/01:80/:89 contract).
                raise FrameFormatError(
                    'total_len %d > ATOMIC_MTU_BYTES %d (group rejected)'
                    % (totalLen, ATOMIC_MTU_BYTES))
        elif tid != transferId or tLen != totalLen or count != groupCount:
            raise FrameFormatError(
                'fragment meta mismatch (tid %d/%d, total %d/%d, count %d/%d)'
                % (transferId, tid, totalLen, tLen, groupCount, count))
        if offset + len(frame) - FIXED_HEADER_SIZE - FRAGMENT_HEADER_SIZE > totalLen:
            raise FrameFormatError(
                'fragment overruns total_len (offset %d)' % offset)
        chunks[index] = frame[FIXED_HEADER_SIZE + FRAGMENT_HEADER_SIZE:]
    if sorted(chunks) != list(range(groupCount)):
        raise FrameFormatError('fragment indices malformed')
    payload = b''.join(chunks[i] for i in range(groupCount))
    if len(payload) != totalLen:
        raise FrameFormatError(
            'reassembled %d bytes, total_len says %d' % (len(payload), totalLen))
    return header, payload
