"""
dump_frame.py - encodeToolCall 编码结果解析查看工具（联调前自查用）.

Python 端与嵌入式端未打通期间, 用本工具把 encodeToolCall() 编出的二进制帧
逐层拆开给人看, 并对照嵌入式端 C++ 生成解码器的硬性约束给出告警:
  - 帧格式: adapter/embedded/include/frame.h (protocol v0, 大端)
  - CBOR 约束: key 必须为 unsigned int / 顶层必须 map / 值类型匹配 schema /
    request 侧 default=None 字段必填(缺失 C++ 解码器返回 1012, 路由层
    将其作为响应 code 回给 Python 端)
  - 字段映射: tool_registry.py (opcode<->工具名, key<->字段名, enum<->索引)

帧格式 (protocol v0, 与 codec/frame.py 对齐):
  byte0 = [ver:3|t:2|enc:2|f:1]
  byte1 = [rp:2|reserve:6]
  f=0: [Fixed Header 2B] + [Entry x N]     无 entry 计数, 迭代到帧尾
  f=1: [Fixed Header 2B] + [Fragment Header 8B] + [frag data]
       分片头 = transfer_id(u16) + total_len(u16) + offset(u16)
                + index(u8) + count(u8)
  entry = seq_id(u32 BE) + opcode(u32 BE) + params_len(u16 BE)
          + params(int-key CBOR)

用法 (PowerShell):
  python dump_frame.py <hex字符串>          直接解析 hex 字节流(单帧)
  python dump_frame.py <文件路径>           从文件读 hex 文本(失败则按二进制读);
                                            多行=多帧, 同组 f=1 分片自动重组
  Get-Content frames.hex | python dump_frame.py -
  python dump_frame.py --encode '<MCP JSON>'    内部调 encodeToolCall 再解析
  python dump_frame.py --encode '@req.json'     @ 前缀表示从文件读 JSON
  python dump_frame.py --selftest              黄金用例自检
可选: --json 机器可读输出  --raw 额外整帧 hex dump
      --ver/--enc/--reply/--mtu 仅 --encode 模式有效(默认 0/0/0/512,
      与 encodeToolCall OPTIONS_DEFAULT 一致; mtu<=0 关闭分片输出单帧)
"""

import argparse
import json
import os
import struct
import sys

_here = os.path.dirname(os.path.abspath(__file__))

try:
    import cbor2  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - fallback to bundled codec
    sys.path.insert(0, _here)
    import _cbor2_fallback as cbor2

if __package__:  # package mode: python -m mcp_wire.dump_frame
    from .tool_registry import (
        TOOL_ID_TO_NAME,
        TOOL_ID_TO_RESPONSE_SCHEMA,
        TOOL_ID_TO_REQUEST_SCHEMA,
        ENUM_REGISTRY,
    )
    _mcp_bridge_mod = __package__ + '.mcp_bridge'
    _fragment_mod = __package__ + '.codec.fragment'
else:  # script mode: python mcp_wire/dump_frame.py
    # mcp_bridge/codec 已改为包内相对导入，脚本模式也必须经由包路径加载，
    # 否则顶层加载 mcp_bridge 时其 ``from .codec...`` 相对导入会失败。
    _parent = os.path.dirname(_here)
    if _parent not in sys.path:
        sys.path.insert(0, _parent)
    from mcp_wire.tool_registry import (
        TOOL_ID_TO_NAME,
        TOOL_ID_TO_RESPONSE_SCHEMA,
        TOOL_ID_TO_REQUEST_SCHEMA,
        ENUM_REGISTRY,
    )
    _mcp_bridge_mod = 'mcp_wire.mcp_bridge'
    _fragment_mod = 'mcp_wire.codec.fragment'

# 交叉校验用: _cborToArgs 与 Python 端解码路径同源; encodeToolCall 供 --encode 用
try:
    _bridge = __import__(_mcp_bridge_mod, fromlist=['_cborToArgs', 'encodeToolCall'])
    _cborToArgs = getattr(_bridge, '_cborToArgs', None)
    encodeToolCall = getattr(_bridge, 'encodeToolCall', None)
except Exception:
    _cborToArgs = None
    encodeToolCall = None
# 分片组重组用官方实现(与 Python 生产路径同源)
try:
    _frag = __import__(_fragment_mod, fromlist=['frameIsComplete', 'reassembleWithHeader'])
    frameIsComplete = getattr(_frag, 'frameIsComplete', None)
    reassembleWithHeader = getattr(_frag, 'reassembleWithHeader', None)
except Exception:
    frameIsComplete = None
    reassembleWithHeader = None


# -- Constants (与 frame.h / codec/frame.py 对齐) --

MSG_TYPE_NAMES = {0: 'REQUEST', 1: 'RESPONSE', 2: 'EVENT'}
FIXED_HEADER_SIZE = 2
FRAGMENT_HEADER_SIZE = 8
ENTRY_FIXED_SIZE = 10  # seq_id(4) + opcode(4) + params_len(2)
ATOMIC_MTU_BYTES = 2048  # 单个 payload 硬上限(两端一致)

# schema 字段类型 -> 允许的 CBOR kind (用于类型匹配告警)
SCHEMA_TYPE_EXPECT = {
    'integer': ('uint', 'nint'),
    'boolean': ('false', 'true'),
    'string': ('text',),
    'enum': ('uint',),
    'number': ('float',),
    'float': ('float',),
    'array': ('array',),
}


# -- Hex 输入清洗 --

def cleanHex(text):
    """容忍 0x 前缀/空格/逗号/冒号/换行等杂质的 hex 文本 -> bytes."""
    # 先按整体移除 0x/0X 前缀(逐字符删 x 会把 0x10 错变成 010)
    s = text.replace('0x', '').replace('0X', '')
    drop = set(" \t\r\n,;:_-'\"<>[](){}")
    s = ''.join(ch for ch in s if ch not in drop)
    if not s:
        raise ValueError('输入为空')
    if len(s) % 2 != 0:
        raise ValueError('hex 字符数为奇数(%d), 请检查是否漏抄/多抄字符' % len(s))
    bad = sorted(set(c for c in s if c not in '0123456789abcdefABCDEF'))
    if bad:
        raise ValueError('含非 hex 字符: %s' % ''.join(bad))
    return bytes.fromhex(s)


def splitInputFrames(text):
    """hex 文本 -> 帧字节列表.

    多行非空内容按"每行一帧"切分(串口日志惯例); 单行(可含续行杂质)
    整体清洗为一个帧. 能否按帧列表解析由 looksLikeFrameList 复核.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) <= 1:
        return [cleanHex(text)]
    return [cleanHex(ln) for ln in lines]


def looksLikeFrameList(frames):
    """每行都能独立解析为无截断告警的帧, 才认定为帧列表(否则按单帧拼接)."""
    for fb in frames:
        try:
            _, ws = parseFrame(fb)
        except ValueError:
            return False
        if any(('截断' in w) or ('不完整' in w) for w in ws):
            return False
    return True


# -- Mini CBOR 解析器 (仅覆盖本项目编码子集, 逐元素记录字节偏移) --

class CborParseError(Exception):
    pass


_AI_WIDTHS = {24: 1, 25: 2, 26: 4, 27: 8}


def _readArg(data, pos, ai, what):
    """读取整数参数(ai 为 24/25/26/27 时的扩展字节)."""
    w = _AI_WIDTHS.get(ai)
    if w is None:
        if ai == 31:
            raise CborParseError('%s 使用 indefinite-length(ai=31), cbor2 不会产生这种输出, '
                                 '嵌入式生成代码也未按此处理' % what)
        raise CborParseError('%s 使用保留 additional info=%d' % (what, ai))
    end = pos + w
    if end > len(data):
        raise CborParseError('%s 扩展字节不足: 剩余 %d 字节, 需要 %d' % (what, len(data) - pos, w))
    return int.from_bytes(data[pos:end], 'big'), end


def _parseItem(data, pos):
    """解析一个 CBOR 元素, 返回 (elem, nextPos). elem 含 offset/end/kind/value."""
    start = pos
    if pos >= len(data):
        raise CborParseError('数据不足: 期望元素 @%d 但已到末尾' % pos)
    ib = data[pos]
    pos += 1
    mt = ib >> 5
    ai = ib & 0x1F

    if mt in (0, 1):  # uint / nint
        if ai < 24:
            val = ai
        else:
            val, pos = _readArg(data, pos, ai, '整数')
        if mt == 1:
            val = -1 - val
        return {'kind': 'uint' if mt == 0 else 'nint',
                'offset': start, 'end': pos, 'value': val}, pos

    if mt in (2, 3):  # byte string / text string
        if ai < 24:
            ln = ai
        else:
            ln, pos = _readArg(data, pos, ai, '字符串长度')
        end = pos + ln
        if end > len(data):
            raise CborParseError('字符串内容不足: 声明 %d 字节, 剩余 %d' % (ln, len(data) - pos))
        raw = data[pos:end]
        pos = end
        if mt == 2:
            return {'kind': 'bytes', 'offset': start, 'end': pos,
                    'value': raw, 'str_len': ln}, pos
        elem = {'kind': 'text', 'offset': start, 'end': pos,
                'value': raw, 'str_len': ln}
        try:
            elem['value'] = raw.decode('utf-8')
            elem['utf8_ok'] = True
        except UnicodeDecodeError:
            elem['utf8_ok'] = False
        return elem, pos

    if mt in (4, 5):  # array / map (definite length)
        if ai < 24:
            count = ai
        else:
            count, pos = _readArg(data, pos, ai, '容器长度')
        if count > 65535:
            raise CborParseError('容器元素数过大(%d), 疑似乱码' % count)
        if mt == 4:
            items = []
            for _ in range(count):
                item, pos = _parseItem(data, pos)
                items.append(item)
            return {'kind': 'array', 'offset': start, 'end': pos,
                    'count': count, 'items': items}, pos
        pairs = []
        for _ in range(count):
            k, pos = _parseItem(data, pos)
            v, pos = _parseItem(data, pos)
            pairs.append((k, v))
        return {'kind': 'map', 'offset': start, 'end': pos,
                'count': count, 'pairs': pairs}, pos

    if mt == 6:
        raise CborParseError('出现 CBOR tag(major type 6), cbor2 对普通值不会编出 tag')

    # mt == 7: simple values / floats
    if ai == 20:
        return {'kind': 'false', 'offset': start, 'end': pos, 'value': False}, pos
    if ai == 21:
        return {'kind': 'true', 'offset': start, 'end': pos, 'value': True}, pos
    if ai == 22:
        return {'kind': 'null', 'offset': start, 'end': pos, 'value': None}, pos
    if ai == 23:
        return {'kind': 'undefined', 'offset': start, 'end': pos, 'value': None}, pos
    if ai in (25, 26, 27):
        fmt, name = {25: ('>e', 'float16'), 26: ('>f', 'float32'), 27: ('>d', 'float64')}[ai]
        end = pos + struct.calcsize(fmt)
        if end > len(data):
            raise CborParseError('%s 内容不足' % name)
        val = struct.unpack(fmt, data[pos:end])[0]
        return {'kind': 'float', 'offset': start, 'end': end,
                'value': val, 'floatType': name}, end
    if ai == 24:
        if pos >= len(data):
            raise CborParseError('simple value 内容不足')
        val = data[pos]
        return {'kind': 'simple', 'offset': start, 'end': pos + 1, 'value': val}, pos + 1
    raise CborParseError('不支持的 simple/float 编码 (initial byte 0x%02X)' % ib)


def rawValue(elem):
    """解析树 -> 原始 Python 值(与 cbor2.loads 结果可比)."""
    kind = elem['kind']
    if kind == 'map':
        d = {}
        for k, v in elem['pairs']:
            d[rawValue(k)] = rawValue(v)  # 重复 key 保留最后一个, 与 cbor2 一致
        return d
    if kind == 'array':
        return [rawValue(x) for x in elem['items']]
    if kind == 'undefined':
        return getattr(cbor2, 'undefined', None)
    return elem['value']


def valuesEqual(a, b):
    """宽容比较(处理 NaN/bool-vs-int 陷阱)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, float) and isinstance(b, float):
        return a == b or (a != a and b != b)  # NaN
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a.keys()) == set(b.keys()) and all(valuesEqual(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(valuesEqual(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return type(a) is type(b) and a == b


# -- 帧层解析 (protocol v0) --

def parseFrame(data):
    """解析一个线帧: f=0 迭代 entry 至帧尾, f=1 解析分片头+数据切片.

    返回 (frame, warnings). frame['entries'] 仅 f=0 时有内容;
    f=1 时 frame['fragment'] 描述分片元信息(payload 切片无法解析 entry,
    完整组由 tryReassemble 重组后再走 f=0 路径).
    """
    warnings = []
    if len(data) < FIXED_HEADER_SIZE:
        raise ValueError('帧太短: %d 字节, 至少需要 %d 字节定长头' % (len(data), FIXED_HEADER_SIZE))
    b0, b1 = data[0], data[1]
    frame = {
        'size': len(data),
        'flags': b0,
        'reply_byte': b1,
        'ver': (b0 >> 5) & 0x07,
        'type': (b0 >> 3) & 0x03,
        'enc': (b0 >> 1) & 0x03,
        'frag': b0 & 0x01,
        'reply': (b1 >> 6) & 0x03,
        'reserved': b1 & 0x3F,
        'entries': [],
    }
    if frame['reserved'] != 0:
        warnings.append('Header: byte1 低 6 位 reserve=0x%02X 非零, 协议保留位应为 0'
                        % frame['reserved'])
    if frame['type'] not in MSG_TYPE_NAMES:
        warnings.append('Header: t=%d 不是合法消息类型(0/1/2)' % frame['type'])
    if frame['enc'] != 0:
        warnings.append('Header: enc=%d 不受支持(当前协议仅 enc=0 int-key CBOR), '
                        '两端按 CBOR 解析将失败' % frame['enc'])
    if frame['reply'] not in (0, 1):
        warnings.append('Header: rp=%d 超出定义(0=无需应答, 1=需应答, 2/3 保留)'
                        % frame['reply'])

    if frame['frag']:
        _parseFragmentPart(data, frame, warnings)
        return frame, warnings

    if len(data) > FIXED_HEADER_SIZE + ATOMIC_MTU_BYTES:
        warnings.append('帧总长 %d 超过 定长头+ATOMIC_MTU_BYTES(%d): '
                        '编码端 fragmentPayload 不产这种帧, 重组端将拒收'
                        % (len(data), FIXED_HEADER_SIZE + ATOMIC_MTU_BYTES))

    pos = FIXED_HEADER_SIZE
    idx = 0
    while pos < len(data):
        if pos + ENTRY_FIXED_SIZE > len(data):
            warnings.append('Entry[%d]: 头部不完整(帧尾残余 %d 字节, 帧被截断或粘帧), '
                            '已解析 %d 条' % (idx, len(data) - pos, len(frame['entries'])))
            break
        seqId = int.from_bytes(data[pos:pos + 4], 'big')
        opcode = int.from_bytes(data[pos + 4:pos + 8], 'big')
        paramsLen = int.from_bytes(data[pos + 8:pos + 10], 'big')
        paramsOff = pos + ENTRY_FIXED_SIZE
        if paramsOff + paramsLen > len(data):
            warnings.append('Entry[%d]: params_len=%d 但剩余仅 %d 字节(帧被截断), '
                            '已解析 %d 条'
                            % (idx, paramsLen, len(data) - paramsOff, len(frame['entries'])))
            break
        pos = paramsOff + paramsLen
        if seqId == 0:
            warnings.append('Entry[%d]: seq_id=0 是保留值 '
                            '(encodeToolCall 要求 MCP id 为 1..0xFFFFFFFF)' % idx)
        frame['entries'].append({
            'index': idx,
            'offset': pos - ENTRY_FIXED_SIZE - paramsLen,
            'seq_id': seqId,
            'opcode': opcode,
            'params_len': paramsLen,
            'params_offset': paramsOff,
            'params': bytes(data[paramsOff:paramsOff + paramsLen]),
        })
        idx += 1
    return frame, warnings


def _parseFragmentPart(data, frame, warnings):
    """f=1 帧: 解析 8B 分片头并给出重组端同款校验告警."""
    if len(data) < FIXED_HEADER_SIZE + FRAGMENT_HEADER_SIZE:
        warnings.append('Fragment: 分片头不完整(帧 %d 字节 < %d)'
                        % (len(data), FIXED_HEADER_SIZE + FRAGMENT_HEADER_SIZE))
        return
    p = FIXED_HEADER_SIZE
    transferId = int.from_bytes(data[p:p + 2], 'big')
    totalLen = int.from_bytes(data[p + 2:p + 4], 'big')
    offset = int.from_bytes(data[p + 4:p + 6], 'big')
    index = data[p + 6]
    count = data[p + 7]
    chunkOff = p + FRAGMENT_HEADER_SIZE
    chunkLen = len(data) - chunkOff
    frame['fragment'] = {
        'transfer_id': transferId,
        'total_len': totalLen,
        'offset': offset,
        'index': index,
        'count': count,
        'chunk_offset': chunkOff,
        'chunk_len': chunkLen,
        'chunk': bytes(data[chunkOff:]),
    }
    if transferId == 0:
        warnings.append('Fragment: transfer_id=0 是保留值(合法范围 1..65535)')
    if totalLen == 0:
        warnings.append('Fragment: total_len=0, 空载荷不应分片(应发 f=0 帧)')
    elif totalLen > ATOMIC_MTU_BYTES:
        warnings.append('Fragment: total_len=%d 超过 ATOMIC_MTU_BYTES(%d), '
                        '重组端拒收整组(F-I2: 首片即拒绝)' % (totalLen, ATOMIC_MTU_BYTES))
    if count < 2:
        warnings.append('Fragment: count=%d < 2, 单片数据应使用 f=0 帧(编码端不会产出)' % count)
    if index >= count:
        warnings.append('Fragment: index=%d >= count=%d, 非法分片序号(合法 0..%d)'
                        % (index, count, count - 1))
    if offset + chunkLen > totalLen:
        warnings.append('Fragment: offset(%d) + 本片数据(%d) 超出 total_len(%d), '
                        '重组端判为越界' % (offset, chunkLen, totalLen))


def tryReassemble(frames):
    """若为同一 transfer 的完整 f=1 分片组, 用官方实现重组 -> (header2B, payload).

    非 f=1 组/不完整/官方实现不可用时返回 None.
    """
    if reassembleWithHeader is None or not frames:
        return None
    if len(frames) < 1:
        return None
    try:
        fr = [bytes(f) for f in frames]
        if not all(len(f) >= FIXED_HEADER_SIZE and (f[0] & 0x01) == 1 for f in fr):
            return None
        if not frameIsComplete(fr):
            return None
        return reassembleWithHeader(fr)
    except Exception:
        return None


# -- CBOR 层: schema 感知的节点树 + 业务视图 --

def hexSpan(para, elem):
    """元素完整字节的 hex 展示(超 8 字节截断, 内容在 label 中可见)."""
    raw = para[elem['offset']:elem['end']]
    if len(raw) > 8:
        return ' '.join('%02X' % b for b in raw[:8]) + '..'
    return ' '.join('%02X' % b for b in raw)


def mkNode(para, elem, label, note=''):
    return {'offset': elem['offset'], 'hex': hexSpan(para, elem),
            'label': label, 'note': note, 'children': []}


def shortRepr(v):
    s = repr(v)
    return s if len(s) <= 40 else s[:37] + '...'


def buildValueNode(para, elem, field, sourceVal, warnings, ctx):
    """值元素 -> (节点, 业务值). field 为 None 时做纯结构解析."""
    kind = elem['kind']
    ftype = field['type'] if field else None

    if field is not None:
        allowed = SCHEMA_TYPE_EXPECT.get(ftype)
        if allowed is not None and kind not in allowed:
            warnings.append('%s: 字段 "%s"(key=%d) 类型不匹配: CBOR=%s, schema=%s(期望 %s); '
                            'C++ 按 %s 读取且不检查 getter 返回值, 结构体字段为垃圾值(静默数据错误)'
                            % (ctx, field['name'], field['key'], kind,
                               ftype, '/'.join(allowed), ftype))

    if kind in ('uint', 'nint'):
        v = elem['value']
        label = '%s(%d)' % (kind, v)
        biz = v
        note = ''
        if ftype == 'enum':
            ref = field.get('enum_ref')
            nameByWire = {}
            if ref is not None:
                nameByWire = {w: n for n, w in
                              ENUM_REGISTRY.get(ref, {}).get('index', {}).items()}
            if v in nameByWire:
                biz = nameByWire[v]
                note = 'enum -> "%s"' % biz
            else:
                warnings.append('%s: 字段 "%s" 枚举 wire 值 %d 越界(enum %s, '
                                '合法 %s), C++ 端直接强转枚举类型按原始数值使用'
                                % (ctx, field['name'], v, ref if ref is not None else '?',
                                   sorted(nameByWire) if nameByWire else '未知'))
                note = 'enum 越界'
        return mkNode(para, elem, label, note), biz

    if kind == 'text':
        ln = elem.get('str_len', elem['end'] - elem['offset'])
        if elem.get('utf8_ok', True):
            label = 'text(%d) %s' % (ln, json.dumps(elem['value'], ensure_ascii=False))
            biz = elem['value']
            return mkNode(para, elem, label), biz
        warnings.append('%s: text 字节不是合法 UTF-8, C++ cbor_value_copy_text_string '
                        '解码失败(返回值未检查)' % ctx)
        return (mkNode(para, elem, 'text(%d) <非法UTF-8>' % ln,
                       'hex=%s' % elem['value'].hex().upper()),
                elem['value'])

    if kind == 'bytes':
        ln = elem.get('str_len', elem['end'] - elem['offset'])
        return mkNode(para, elem, 'bytes(%d)' % ln, '协议未使用 byte string'), elem['value']

    if kind in ('false', 'true'):
        return mkNode(para, elem, kind), (kind == 'true')

    if kind == 'null':
        return mkNode(para, elem, 'null', '显式 null 值'), None

    if kind == 'undefined':
        return mkNode(para, elem, 'undefined', '协议未使用'), getattr(cbor2, 'undefined', None)

    if kind == 'float':
        note = 'C++ 生成代码按 double 解码(cbor_value_get_double)'
        return mkNode(para, elem, '%s %r' % (elem['floatType'], elem['value']), note), elem['value']

    if kind == 'simple':
        return mkNode(para, elem, 'simple(%d)' % elem['value'], '协议未使用'), elem['value']

    if kind == 'array':
        node = mkNode(para, elem, 'array(%d)' % elem['count'])
        itemFields = field.get('item_fields') if (ftype == 'array' and field) else None
        itemsBiz = []
        srcList = sourceVal if isinstance(sourceVal, list) else None
        for idx, itemElem in enumerate(elem['items']):
            if itemElem['kind'] == 'map':
                srcItem = None
                if isinstance(srcList, list) and idx < len(srcList) \
                        and isinstance(srcList[idx], dict):
                    srcItem = srcList[idx]
                childNode, childBiz = buildMapNode(para, itemElem, itemFields, srcItem,
                                                   warnings, ctx, 'item',
                                                   '[item #%d]' % idx)
                node['children'].append(childNode)
            else:
                warnings.append('%s: array item #%d 不是 map, C++ 端 items 迭代器将解析失败'
                                % (ctx, idx))
                childNode, _ = buildValueNode(para, itemElem, None, None, warnings, ctx)
                node['children'].append(childNode)
            itemsBiz.append(childBiz)
        return node, itemsBiz

    raise AssertionError('unreachable kind %r' % kind)


def buildMapNode(para, mapElem, fields, sourceDict, warnings, ctx, mode, itemTag=None):
    """map 元素 -> (节点, 业务 dict).

    fields: None=无 schema 映射(结构解析); list=request/response/item schema.
    mode: 'request'/'response'/'item'/'raw' (决定必填检查与默认填充标注).
    """
    node = mkNode(para, mapElem, 'map(%d)' % mapElem['count'], itemTag or '')
    keyToField = {}
    if fields is not None:
        for f in fields:
            keyToField.setdefault(f['key'], f)
    biz = {}
    presentKeyInts = set()
    for kElem, vElem in mapElem['pairs']:
        field = None
        if kElem['kind'] == 'uint':
            kv = kElem['value']
            presentKeyInts.add(kv)
            field = keyToField.get(kv)
            if field is not None:
                kDesc = 'key=%d  %s' % (kv, field['name'])
            elif fields is not None:
                kDesc = 'key=%d  (schema 无此字段)' % kv
                warnings.append('%s: key=%d 不在 schema 字段表中, '
                                'C++ 端 default 分支直接跳过(静默忽略)' % (ctx, kv))
            else:
                kDesc = 'key=%d' % kv
        else:
            kv = rawValue(kElem)
            kDesc = 'key(%s)=%s' % (kElem['kind'], shortRepr(kv))
            warnings.append('%s: key 不是 unsigned int(是 %s), C++ 端 '
                            'cbor_value_is_unsigned_integer 检查失败, 解码器返回 -1, '
                            'handle 层将 -1 作为响应 code 回复'
                            % (ctx, kElem['kind']))
        keyNode = mkNode(para, kElem, kDesc)
        defaultFilled = False
        srcVal = None
        if field is not None and isinstance(sourceDict, dict):
            defaultFilled = field['name'] not in sourceDict
            srcVal = sourceDict.get(field['name'])
        valNode, valBiz = buildValueNode(para, vElem, field, srcVal, warnings, ctx)
        if defaultFilled:
            valNode['note'] = ((valNode['note'] + '  ') if valNode['note'] else '') \
                + '(default 填充)'
        node['children'].append(keyNode)
        node['children'].append(valNode)
        if field is not None:
            biz[field['name']] = valBiz
        elif fields is None:
            try:
                biz[kv] = valBiz
            except TypeError:
                pass
    if fields is not None:
        mandatoryCheck(fields, presentKeyInts, mode, warnings, ctx)
    return node, biz


def mandatoryCheck(fields, presentKeyInts, mode, warnings, ctx):
    """对照 C++ 生成解码器的 found_xxx 检查(缺失返回 1012)给出告警."""
    for f in fields:
        if f['key'] in presentKeyInts:
            continue
        if mode == 'request':
            if f['type'] == 'array':
                continue  # C++ 端 array 输入字段缺失时容忍(迭代器直接结束)
            if f.get('default', None) is None:
                warnings.append('%s: 必填字段 "%s"(key=%d) 缺失, '
                                'C++ 解码器 found 检查失败返回 1012, 路由层将其作为响应 code 回复'
                                % (ctx, f['name'], f['key']))
        elif mode == 'response':
            if f.get('presence') == 'mandatory':
                warnings.append('%s: 响应必填字段 "%s"(key=%d) 缺失' % (ctx, f['name'], f['key']))
        # mode == 'item'/'raw': C++ 迭代器无 found 检查, 不告警


# -- Entry 级分析 --

def analyzeEntry(frame, entry, sourceArgs, warnings):
    """解析一条 entry 的 CBOR 并做全部交叉校验, 结果写回 entry."""
    entryLabel = 'Entry[%d]' % entry['index']
    opcode = entry['opcode']
    toolName = TOOL_ID_TO_NAME.get(opcode)
    entry['tool_name'] = toolName
    ctx = '%s(%s)' % (entryLabel, toolName if toolName else 'opcode=%d' % opcode)
    if toolName is None:
        warnings.append('%s: opcode=%d 不在工具注册表(1..48), '
                        'C++ 端路由层 rp=1 时将回 code=1001 (not_supported) 的错误 entry'
                        % (entryLabel, opcode))

    para = bytes(entry['params'])
    entry['crosscheck'] = []
    schema = None
    mode = 'raw'
    if toolName is not None:
        if frame['type'] == 0:
            schema = TOOL_ID_TO_REQUEST_SCHEMA.get(opcode)
            mode = 'request'
        elif frame['type'] == 1:
            schema = TOOL_ID_TO_RESPONSE_SCHEMA.get(opcode)
            mode = 'response'
        # type==2 (EVENT): 注册表无事件 schema, 仅结构解析

    if entry['params_len'] == 0:
        entry['node'] = None
        entry['business'] = None
        entry['business_label'] = None
        warnings.append('%s: params_len=0(空 CBOR). encodeToolCall 最少也会输出 A0(空 map); '
                        'C++ 解码器 bufLen==0 分支: 有必填字段则返回 1012, 否则按默认值解码成功'
                        % ctx)
        if mode == 'request' and schema:
            missing = [f for f in schema
                       if f['type'] != 'array' and f.get('default') is None]
            if missing:
                names = ', '.join('"%s"' % f['name'] for f in missing)
                warnings.append('%s: params 为空且必填字段 %s 缺失, '
                                'C++ 端 bufLen==0 分支返回 1012' % (ctx, names))
        return

    try:
        top, endPos = _parseItem(para, 0)
    except CborParseError as e:
        entry['node'] = None
        entry['business'] = None
        entry['business_label'] = None
        entry['cbor_error'] = str(e)
        warnings.append('%s: CBOR 解析失败: %s' % (ctx, e))
        return
    entry['cbor_error'] = None
    if endPos != len(para):
        warnings.append('%s: CBOR 元素实际消耗 %d 字节 != params_len %d, '
                        '帧内字节与 CBOR 内容不一致' % (ctx, endPos, len(para)))

    if top['kind'] != 'map':
        warnings.append('%s: CBOR 顶层不是 map(是 %s), C++ 端 cbor_value_is_map 检查失败返回 -1, '
                        'handle 层将 -1 作为响应 code 回复'
                        % (ctx, top['kind']))
        node, biz = buildValueNode(para, top, None, None, warnings, ctx)
        entry['node'] = node
        entry['business'] = biz
        entry['business_label'] = '原始值(顶层非 map)'
    else:
        node, biz = buildMapNode(para, top, schema,
                                 sourceArgs if mode == 'request' else None,
                                 warnings, ctx, mode)
        entry['node'] = node
        entry['business'] = biz
        entry['business_label'] = '业务视图' if schema is not None else '原始视图(无 schema 映射)'

    # 交叉校验 1: 自研解析 vs cbor2.loads
    try:
        refRaw = cbor2.loads(para)
    except Exception as e:
        refRaw = None
        entry['crosscheck'].append('交叉校验: cbor2.loads 失败(%s)' % e)
    if refRaw is not None:
        myRaw = rawValue(top)
        if valuesEqual(myRaw, refRaw):
            entry['crosscheck'].append('交叉校验: 自研解析 vs cbor2.loads 一致')
        else:
            warnings.append('%s: 自研解析(%s) 与 cbor2.loads(%s) 不一致 —— '
                            'dump_frame 自身 bug, 请反馈'
                            % (ctx, shortRepr(myRaw), shortRepr(refRaw)))
            entry['crosscheck'].append('交叉校验: 自研解析 vs cbor2.loads 不一致!')

    # 交叉校验 2: 业务视图 vs mcp_bridge._cborToArgs(Python 端解码同源路径)
    if _cborToArgs is not None and schema and top['kind'] == 'map':
        try:
            refBiz = _cborToArgs(para, schema)
        except Exception as e:
            entry['crosscheck'].append('交叉校验: _cborToArgs 抛异常(%s)' % e)
        else:
            if valuesEqual(refBiz, entry['business']):
                entry['crosscheck'].append('交叉校验: 业务视图 vs mcp_bridge._cborToArgs 一致')
            else:
                warnings.append('%s: 业务视图与 _cborToArgs 不一致(dump_frame 与解码路径分歧): '
                                '自研=%s 参考=%s'
                                % (ctx, shortRepr(entry['business']), shortRepr(refBiz)))
                entry['crosscheck'].append('交叉校验: 业务视图 vs _cborToArgs 不一致!')


# -- 文本渲染 --

def hexAt(data, off, n):
    return ' '.join('%02X' % b for b in data[off:off + n])


def renderNode(node, depth, lines):
    desc = node['label']
    if node['note']:
        desc = desc + '    ' + node['note']
    lines.append('%s+0x%04X  %-26s  %s' % ('    ' * (depth + 1),
                                            node['offset'], node['hex'], desc))
    for child in node['children']:
        renderNode(child, depth + 1, lines)


def renderText(frame, warnings, data, title=None):
    lines = []
    if title:
        lines.append('=' * 14 + ' %s ' % title + '=' * 14)
    typeName = MSG_TYPE_NAMES.get(frame['type'], 'UNKNOWN')
    lines.append('=' * 14 + ' Frame (%d bytes) ' % len(data) + '=' * 14)
    lines.append('Header @0x0000: flags=0x%02X  reply_byte=0x%02X'
                 % (frame['flags'], frame['reply_byte']))
    lines.append('  ver=%d  type=%s(%d)  enc=%d  f=%d  rp=%d  reserve=0x%02X'
                 % (frame['ver'], typeName, frame['type'], frame['enc'],
                    frame['frag'], frame['reply'], frame['reserved']))
    if frame['frag']:
        frag = frame.get('fragment')
        if frag:
            lines.append('FragmentHeader @0x%04X (8 bytes):'
                         % FIXED_HEADER_SIZE)
            lines.append('  transfer_id = %-10d (u16 BE: %s)'
                         % (frag['transfer_id'], hexAt(data, FIXED_HEADER_SIZE, 2)))
            lines.append('  total_len   = %-10d (u16 BE: %s)'
                         % (frag['total_len'], hexAt(data, FIXED_HEADER_SIZE + 2, 2)))
            lines.append('  offset      = %-10d (u16 BE: %s)'
                         % (frag['offset'], hexAt(data, FIXED_HEADER_SIZE + 4, 2)))
            lines.append('  index/count = %d/%d'
                         % (frag['index'], frag['count']))
            lines.append('  data        @0x%04X (%d bytes)  # 原始 payload 切片, '
                         '重组后才可解析 entry' % (frag['chunk_offset'], frag['chunk_len']))
        lines.append('=' * 14 + ' 解析完成: 分片帧, %d 告警 ' % len(warnings) + '=' * 14)
        return lines

    for entry in frame['entries']:
        lines.append('Entry[%d] @0x%04X' % (entry['index'], entry['offset']))
        lines.append('  seq_id     = %-10d (u32 BE: %s)'
                     % (entry['seq_id'], hexAt(data, entry['offset'], 4)))
        opHex = hexAt(data, entry['offset'] + 4, 4)
        if entry.get('tool_name'):
            lines.append('  opcode     = %-10d (u32 BE: %s)  -> %s'
                         % (entry['opcode'], opHex, entry['tool_name']))
        else:
            lines.append('  opcode     = %-10d (u32 BE: %s)' % (entry['opcode'], opHex))
        lines.append('  params_len = %-10d (u16 BE: %s)'
                     % (entry['params_len'], hexAt(data, entry['offset'] + 8, 2)))
        if entry['params_len']:
            lines.append('  params     @0x%04X (%d bytes)'
                         % (entry['params_offset'], entry['params_len']))
        lines.append('')
        if entry.get('cbor_error'):
            lines.append('  CBOR 解析失败: %s' % entry['cbor_error'])
            lines.append('')
        if entry.get('node') is not None:
            lines.append('  CBOR 逐元素解析:')
            renderNode(entry['node'], 1, lines)
            lines.append('')
        if entry.get('business') is not None:
            lines.append('  %s: %s' % (entry.get('business_label') or '业务视图',
                                        json.dumps(entry['business'],
                                                   ensure_ascii=False, default=str)))
            lines.append('')
        for cc in entry.get('crosscheck', []):
            lines.append('  %s' % cc)
        if entry.get('crosscheck'):
            lines.append('')
    lines.append('=' * 14 + ' 解析完成: %d entry, %d 告警 '
                 % (len(frame['entries']), len(warnings)) + '=' * 14)
    return lines


def hexDump(data):
    lines = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        hexPart = ' '.join('%02X' % b for b in chunk[:8])
        if len(chunk) > 8:
            hexPart += '  ' + ' '.join('%02X' % b for b in chunk[8:])
        asciiPart = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append('%08X  %-48s  |%s|' % (off, hexPart, asciiPart))
    return lines


# -- JSON 输出 --

def buildJson(frame, warnings, data):
    def nodeToJson(node):
        if node is None:
            return None
        return {'offset': node['offset'], 'hex': node['hex'],
                'label': node['label'], 'note': node['note'],
                'children': [nodeToJson(c) for c in node['children']]}

    out = {
        'frame': {
            'size': frame['size'], 'flags': '0x%02X' % frame['flags'],
            'reply_byte': '0x%02X' % frame['reply_byte'],
            'ver': frame['ver'], 'type': frame['type'],
            'type_name': MSG_TYPE_NAMES.get(frame['type']),
            'enc': frame['enc'], 'frag': frame['frag'],
            'reply': frame['reply'], 'reserved': frame['reserved'],
        },
        'entries': [],
        'warnings': warnings,
    }
    if frame.get('fragment'):
        f = frame['fragment']
        out['fragment'] = {
            'transfer_id': f['transfer_id'], 'total_len': f['total_len'],
            'offset': f['offset'], 'index': f['index'], 'count': f['count'],
            'chunk_len': f['chunk_len'], 'chunk_hex': f['chunk'].hex(),
        }
    for e in frame['entries']:
        out['entries'].append({
            'index': e['index'], 'offset': e['offset'],
            'seq_id': e['seq_id'], 'opcode': e['opcode'],
            'tool_name': e.get('tool_name'),
            'params_len': e['params_len'], 'params_offset': e['params_offset'],
            'params_hex': bytes(e['params']).hex(),
            'cbor_error': e.get('cbor_error'),
            'cbor_tree': nodeToJson(e.get('node')),
            'business': e.get('business'),
        })
    return out


# -- 主流程 --

class EntryFeeder(object):
    """跨多帧/重组 PDU 维持全局 entry 计数, 用于 --encode 源参数对位."""

    def __init__(self, sourceArgsList):
        self.sourceArgsList = sourceArgsList or []
        self.consumed = 0

    def next(self):
        src = None
        if self.consumed < len(self.sourceArgsList):
            src = self.sourceArgsList[self.consumed]
        self.consumed += 1
        return src


def analyzeFrameEntries(frame, warnings, feeder):
    for entry in frame['entries']:
        analyzeEntry(frame, entry, feeder.next(), warnings)


def renderOne(data, sourceArgsList, args, feeder, title=None):
    """解析/渲染单个帧(f=0 或 f=1). 返回 (frame, warnings)."""
    try:
        frame, warnings = parseFrame(data)
    except ValueError as e:
        print('错误: %s' % e, file=sys.stderr)
        return None, None
    analyzeFrameEntries(frame, warnings, feeder)
    if args.json:
        print(json.dumps(buildJson(frame, warnings, data),
                         ensure_ascii=False, indent=2, default=str))
    else:
        if args.raw:
            for line in hexDump(data):
                print(line)
            print('')
        for line in renderText(frame, warnings, data, title=title):
            print(line)
        print('')
    return frame, warnings


def runDump(frames, sourceArgsList, args):
    """帧列表 -> 逐帧解析渲染; 同组完整 f=1 自动重组后再按 f=0 解析.

    frames: [bytes]; 单帧为常见情形. 返回进程退出码.
    """
    feeder = EntryFeeder(sourceArgsList)
    totalWarnings = 0

    reassembled = tryReassemble(frames) if len(frames) > 1 else None
    if reassembled is not None:
        header, payload = reassembled
        # 重组后语义上是完整 PDU: 清掉 f 位按 f=0 帧解析(与 decodeFrame 语义一致)
        wire = bytes([header[0] & 0xFE, header[1]]) + bytes(payload)
        if not args.json:
            print('输入 %d 个分片帧属于同一 transfer 且组完整, 已用 codec.fragment '
                  '官方实现重组为 %d 字节 PDU:' % (len(frames), len(wire)))
            for i, f in enumerate(frames):
                print('  frag[%d] %s' % (i, bytes(f).hex().upper()))
            print('')
        frame, ws = renderOne(wire, sourceArgsList, args, feeder,
                              title='Reassembled PDU')
        if ws is not None:
            totalWarnings += len(ws)
        # 分片帧自身仍逐帧展示(校验分片头合法性)
        if not args.json:
            for i, f in enumerate(frames):
                fr, ws = parseFrame(bytes(f))
                if ws:
                    print('frag[%d] 告警:' % i)
                    for w in ws:
                        print('  [WARN] %s' % w)
                    totalWarnings += len(ws)
        return 0 if totalWarnings == 0 else 0  # 告警不改变退出码(自查工具)

    for i, f in enumerate(frames):
        title = 'Frame %d/%d' % (i + 1, len(frames)) if len(frames) > 1 else None
        frame, ws = renderOne(bytes(f), sourceArgsList, args, feeder, title=title)
        if ws is not None:
            totalWarnings += len(ws)
            if not args.json:
                for w in ws:
                    print('  [WARN] %s' % w)
                print('')
    return 0


def cmdEncode(args):
    if encodeToolCall is None:
        print('错误: 无法从 mcp_bridge 导入 encodeToolCall', file=sys.stderr)
        return 1
    src = args.encode
    if src.startswith('@'):
        try:
            with open(src[1:], 'r', encoding='utf-8-sig') as f:
                src = f.read()
        except OSError as e:
            print('错误: 读取 %s 失败: %s' % (src[1:], e), file=sys.stderr)
            return 1
    try:
        mcpJson = json.loads(src)
    except ValueError as e:
        print('错误: --encode JSON 解析失败: %s' % e, file=sys.stderr)
        return 1
    options = {'version': args.ver, 'encoding': args.enc,
               'reply': args.reply, 'mtu': args.mtu}
    try:
        frames = encodeToolCall(mcpJson, options)
    except Exception as e:
        print('错误: encodeToolCall 抛出 %s: %s' % (type(e).__name__, e),
              file=sys.stderr)
        return 1
    if not frames:
        print('错误: encodeToolCall 返回空帧列表', file=sys.stderr)
        return 1

    items = mcpJson if isinstance(mcpJson, list) else [mcpJson]
    sourceArgsList = []
    for it in items:
        params = it.get('params') if isinstance(it, dict) else None
        argObj = params.get('arguments') if isinstance(params, dict) else None
        sourceArgsList.append(argObj if isinstance(argObj, dict) else {})

    if not args.json:
        print('encodeToolCall 输出 %d 个帧 (options=%s):' % (len(frames), json.dumps(options)))
        for i, f in enumerate(frames):
            print('  frame[%d] (%d bytes) %s' % (i, len(f), bytes(f).hex().upper()))
        print('')
    return runDump([bytes(f) for f in frames], sourceArgsList, args)


# -- 自检: 黄金用例 --

SELFTEST_ENCODE_CASES = [
    {
        'name': 'zone_list 空参数(默认值填充 + enum 索引 + int-key map)',
        'mcp': {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "zone_list", "arguments": {}}},
        'hex': '0000000000010000002C0007A301000200030A',
        'tools': ['zone_list'],
        'biz': [{"mode": "active", "offset": 0, "limit": 10}],
    },
    {
        'name': 'call_start 部分参数(string + enum 索引 + 默认填充)',
        'mcp': {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "call_start",
                           "arguments": {"target_type": "private", "target_id": "1001"}}},
        'hex': '00000000000100000009000DA4010102643130303103000401',
        'tools': ['call_start'],
        'biz': [{"target_type": "private", "target_id": "1001",
                 "priority": "normal", "talk_mode": "half_duplex"}],
    },
    {
        'name': 'system_event_subscribe 嵌套 array of map(item 内默认填充)',
        'mcp': {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "system_event_subscribe",
                           "arguments": {"items": [
                               {"resource": "sms"},
                               {"resource": "radio", "event": "sms_received"}]}}},
        'hex': '00000000000100000029000DA10182A201090200A201020203',
        'tools': ['system_event_subscribe'],
        'biz': [{"items": [{"resource": "sms", "event": "all"},
                           {"resource": "radio", "event": "sms_received"}]}],
    },
    {
        'name': 'sms_location_send float 字段(cbor2 默认 FB float64 编码)',
        'mcp': {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "sms_location_send",
                           "arguments": {"target_type": "private", "target_id": "1001",
                                          "value": "loc",
                                          "latitude": 1.5, "longitude": -2.5}}},
        'hex': '000000000001000000260022A5010102643130303103636C6F63'
               '04FB3FF800000000000005FBC004000000000000',
        'tools': ['sms_location_send'],
        'biz': [{"target_type": "private", "target_id": "1001", "value": "loc",
                 "latitude": 1.5, "longitude": -2.5}],
    },
    {
        'name': 'sms_play 负整数默认值(nint 编码)',
        'mcp': {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "sms_play", "arguments": {}}},
        'hex': '000000000001000000270007A3010002200301',
        'tools': ['sms_play'],
        'biz': [{"filter": "unread", "start": -1, "number": 1}],
    },
    {
        'name': 'hmi_vibration_switch boolean true',
        'mcp': {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "hmi_vibration_switch",
                           "arguments": {"enabled": True}}},
        'hex': '000000000001000000140003A101F5',
        'tools': ['hmi_vibration_switch'],
        'biz': [{"enabled": True}],
    },
    {
        'name': 'sms_speak string + boolean false 默认填充',
        'mcp': {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "sms_speak", "arguments": {"text": "hello"}}},
        'hex': '00000000000100000028000AA2016568656C6C6F02F4',
        'tools': ['sms_speak'],
        'biz': [{"text": "hello", "save": False}],
    },
    {
        'name': 'sms_send 中文 UTF-8 text',
        'mcp': {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "sms_send",
                           "arguments": {"target_type": "private", "target_id": "1001",
                                          "content": "你好世界"}}},
        'hex': '000000000001000000250017A30101026431303031036CE4BDA0E5A5BDE4B896E7958C',
        'tools': ['sms_send'],
        'biz': [{"target_type": "private", "target_id": "1001", "content": "你好世界"}],
    },
    {
        'name': '批量调用(一个 PDU 两个 entry, 无 entry 计数靠帧尾定界)',
        'mcp': [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "zone_list", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "hmi_vibration_switch", "arguments": {"enabled": True}}},
        ],
        'hex': '0000000000010000002C0007A301000200030A'
               '00000002000000140003A101F5',
        'tools': ['zone_list', 'hmi_vibration_switch'],
        'biz': [{"mode": "active", "offset": 0, "limit": 10}, {"enabled": True}],
    },
]

SELFTEST_WARN_CASES = [
    {'name': 'CBOR key 非无符号整数',
     'hex': '0000000000010000002B0004A1616101', 'expectWarn': 'unsigned int'},
    {'name': '必填字段缺失(zone_add 缺 zone_name)',
     'hex': '0000000000010000002E0003A10200', 'expectWarn': '1012'},
    {'name': '类型不匹配(offset 传了字符串)',
     'hex': '0000000000010000002B0008A30100026131030A', 'expectWarn': '类型不匹配'},
    {'name': '帧尾多余字节(无 entry 计数, 表现为截断告警)',
     'hex': '0000000000010000002B0007A301000200030AFF', 'expectWarn': '不完整'},
    {'name': 'params 为空(zone_add 必填 zone_name)',
     'hex': '0000000000010000002E0000', 'expectWarn': 'params_len=0'},
    {'name': '未注册 opcode',
     'hex': '000000000001000000630001A0', 'expectWarn': '99'},
    {'name': 'CBOR 消耗与 params_len 不一致',
     'hex': '0000000000010000002B0008A301000200030A00', 'expectWarn': '消耗'},
    {'name': 'seq_id=0 保留值',
     'hex': '0000000000000000002B0007A301000200030A', 'expectWarn': 'seq_id=0'},
    {'name': 'byte1 reserve 位非零',
     'hex': '0001000000010000002B0007A301000200030A', 'expectWarn': 'reserve'},
    {'name': 'enc 不受支持',
     'hex': '0400000000010000002B0007A301000200030A', 'expectWarn': 'enc'},
    {'name': '分片 index >= count',
     'hex': '0100000100080000050201020304', 'expectWarn': 'index'},
    {'name': '分片 total_len 超上限',
     'hex': '0100000108080000000201020304', 'expectWarn': 'total_len'},
]


def selftest():
    if encodeToolCall is None:
        print('错误: 无法导入 mcp_bridge.encodeToolCall, selftest 无法运行',
              file=sys.stderr)
        return 1
    failures = []

    def check(name, cond, detail=''):
        tag = 'PASS' if cond else 'FAIL'
        print('  [%s] %s%s' % (tag, name, (' - ' + detail) if (detail and not cond) else ''))
        if not cond:
            failures.append(name)

    print('== dump_frame selftest ==')
    print('')
    print('-- hex 清洗 --')
    check('容忍 0x 前缀/空格/逗号/引号/换行',
          cleanHex('0x00 01, "aa"\nbb') == bytes([0x00, 0x01, 0xAA, 0xBB]))
    try:
        cleanHex('10A')
        check('奇数长度报错', False)
    except ValueError:
        check('奇数长度报错', True)
    try:
        cleanHex('10ZZ')
        check('非法字符报错', False)
    except ValueError:
        check('非法字符报错', True)

    print('')
    print('-- 黄金用例: encodeToolCall 输出逐字节断言 + 解析断言 --')
    for case in SELFTEST_ENCODE_CASES:
        name = case['name']
        try:
            frames = encodeToolCall(case['mcp'])
        except Exception as e:
            check(name, False, 'encodeToolCall 抛异常: %r' % e)
            continue
        exp = bytes.fromhex(case['hex'])
        check('%s / 帧数(小载荷应为单帧)' % name, len(frames) == 1,
              '期望 1 帧, 实际 %d 帧' % len(frames))
        if not frames:
            continue
        check('%s / 字节流' % name, bytes(frames[0]) == exp,
              '期望 %s 实际 %s' % (exp.hex().upper(), bytes(frames[0]).hex().upper()))
        try:
            frame, warns = parseFrame(bytes(frames[0]))
        except ValueError as e:
            check('%s / 帧解析' % name, False, str(e))
            continue
        analyzeFrameEntries(frame, warns, EntryFeeder([None] * 8))
        check('%s / 无告警' % name, not warns, '告警: %r' % warns)
        check('%s / entry 数' % name,
              len(frame['entries']) == len(case['biz']),
              '期望 %d 实际 %d' % (len(case['biz']), len(frame['entries'])))
        for i, entry in enumerate(frame['entries']):
            if i >= len(case['tools']):
                break
            check('%s / Entry[%d] 工具名' % (name, i),
                  entry.get('tool_name') == case['tools'][i],
                  '期望 %s 实际 %s' % (case['tools'][i], entry.get('tool_name')))
            check('%s / Entry[%d] 业务视图' % (name, i),
                  valuesEqual(entry.get('business'), case['biz'][i]),
                  '期望 %r 实际 %r' % (case['biz'][i], entry.get('business')))
        # 渲染冒烟: 文本与 JSON 输出均不抛异常
        try:
            lines = renderText(frame, warns, bytes(frames[0]))
            json.dumps(buildJson(frame, warns, bytes(frames[0])),
                       ensure_ascii=False, default=str)
            check('%s / 渲染冒烟' % name, bool(lines))
        except Exception as e:
            check('%s / 渲染冒烟' % name, False, '%r' % e)

    print('')
    print('-- 分片重组: 小 mtu 强制 f=1, 与关闭分片输出一致 --')
    fragMcp = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "zone_list", "arguments": {}}}
    whole = encodeToolCall(fragMcp, {'mtu': 0})
    parts = encodeToolCall(fragMcp, {'mtu': 16})
    check('小 mtu 产出多帧', len(parts) >= 2, '实际 %d 帧' % len(parts))
    check('全部为 f=1 分片帧', all((bytes(f)[0] & 0x01) == 1 for f in parts))
    got = tryReassemble([bytes(f) for f in parts])
    check('tryReassemble 重组成功', got is not None)
    if got is not None:
        header, payload = got
        wireR = bytes([header[0] & 0xFE, header[1]]) + bytes(payload)
        check('重组 payload == 单帧 payload', bytes(payload) == bytes(whole[0])[2:],
              '重组 %s vs 单帧 %s'
              % (bytes(payload).hex().upper(), bytes(whole[0])[2:].hex().upper()))
        try:
            frameR, warnsR = parseFrame(wireR)
            analyzeFrameEntries(frameR, warnsR, EntryFeeder([None]))
            check('重组帧解析无告警', not warnsR, '告警: %r' % warnsR)
            check('重组帧 entry 业务视图',
                  valuesEqual(frameR['entries'][0].get('business'),
                              {"mode": "active", "offset": 0, "limit": 10}),
                  '实际 %r' % frameR['entries'][0].get('business'))
            lines = renderText(frameR, warnsR, wireR, title='Reassembled PDU')
            json.dumps(buildJson(frameR, warnsR, wireR), ensure_ascii=False, default=str)
            check('重组帧渲染冒烟', bool(lines))
        except Exception as e:
            check('重组帧解析', False, '%r' % e)

    print('')
    print('-- 编码器缺口演示: 必填字段缺失时 encodeToolCall 不拦截 --')
    # sms_location_send 的 target_type/value 无默认值, 空参数时 _argsToCbor 静默跳过,
    # C++ 解码器 found 检查失败返回 1012, handle 层回 code=1012 —— 工具应能发现这个缺口
    wireGap = encodeToolCall({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "sms_location_send", "arguments": {}}})
    frameGap, warnsGap = parseFrame(bytes(wireGap[0]))
    analyzeFrameEntries(frameGap, warnsGap, EntryFeeder([{}]))
    check('sms_location_send 空参数 -> 告警必填字段缺失(1012)',
          any('1012' in w for w in warnsGap), '告警: %r' % warnsGap)

    print('')
    print('-- 告警路径: 手工构造异常帧 --')
    for case in SELFTEST_WARN_CASES:
        data = bytes.fromhex(case['hex'])
        frame, warns = parseFrame(data)
        analyzeFrameEntries(frame, warns, EntryFeeder([None] * 4))
        hit = any(case['expectWarn'] in w for w in warns)
        check('%s -> 命中告警' % case['name'], hit, '实际告警: %r' % warns)
        try:
            renderText(frame, warns, data)
            json.dumps(buildJson(frame, warns, data), ensure_ascii=False, default=str)
        except Exception as e:
            check('%s / 渲染冒烟' % case['name'], False, '%r' % e)

    print('')
    print('-- 多行输入切分 --')
    fr = splitInputFrames('0000000000010000002B0007A301000200030A\n'
                          '000000000002000000130003A101F5\n')
    check('两行 -> 两帧', len(fr) == 2 and looksLikeFrameList(fr))
    fr2 = splitInputFrames('0000000000010000002B\n0007A301000200030A\n')
    # 续行拆开不是合法帧 -> 回退拼接单帧
    check('续行回退拼接', not looksLikeFrameList(fr2)
          and cleanHex('0000000000010000002B0007A301000200030A') is not None)

    print('')
    if failures:
        print('SELFTEST FAIL (%d 项失败): %s' % (len(failures), '; '.join(failures)))
        return 1
    print('SELFTEST PASS (全部通过)')
    return 0


def _settleFrames(frames):
    """多行输入形态复核: 每行成帧则保持列表, 否则拼接为单帧(兼容续行写法)."""
    if len(frames) <= 1:
        return frames
    if looksLikeFrameList(frames):
        return frames
    return [b''.join(frames)]


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='dump_frame.py',
        description='encodeToolCall 编码结果解析查看工具(protocol v0): 输入二进制帧 hex, '
                    '分层解析帧头/分片头/entry/CBOR 字段并给出对齐嵌入式端的告警',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例 (PowerShell):\n"
               "  python dump_frame.py 0000000000010000002B0007A301000200030A\n"
               "  python dump_frame.py frame.hex\n"
               "  Get-Content frames.hex | python dump_frame.py -\n"
               "  python dump_frame.py --encode '{\"jsonrpc\":\"2.0\",\"id\":1,"
               "\"method\":\"tools/call\",\"params\":{\"name\":\"zone_list\","
               "\"arguments\":{}}}'\n"
               "  python dump_frame.py --encode '@req.json'\n"
               "  python dump_frame.py --selftest")
    ap.add_argument('data', nargs='?',
                    help="hex 字符串 / hex 文件路径 / '-'(从 stdin 读); "
                         "多行文件按每行一帧, 同组 f=1 分片自动重组")
    ap.add_argument('--encode', metavar='MCP_JSON',
                    help='内部调用 encodeToolCall 编码后再解析; @file 表示从文件读 JSON')
    ap.add_argument('--ver', type=int, default=0,
                    help='--encode 模式帧头 ver (默认 0)')
    ap.add_argument('--enc', type=int, default=0,
                    help='--encode 模式帧头 enc, 仅支持 0=int-key CBOR (默认 0)')
    ap.add_argument('--reply', type=int, default=0,
                    help='--encode 模式帧头 rp 应答标志 0/1 (默认 0)')
    ap.add_argument('--mtu', type=int, default=512,
                    help='--encode 模式 MTU, 超出则 f=1 分片; <=0 关闭分片 (默认 512)')
    ap.add_argument('--json', action='store_true',
                    help='输出机器可读 JSON(字段树 + 告警)')
    ap.add_argument('--raw', action='store_true',
                    help='额外输出整帧 hex dump (仅文本模式)')
    ap.add_argument('--selftest', action='store_true',
                    help='运行内置黄金用例自检')
    args = ap.parse_args(argv)

    # 输出统一 UTF-8(中文在重定向/管道/CI 下不乱码), 无法重配置时容忍替换
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    if args.selftest:
        return selftest()

    if args.encode is not None:
        if args.data is not None:
            print('错误: --encode 与位置参数不能同时使用', file=sys.stderr)
            return 2
        return cmdEncode(args)

    if args.data is None:
        ap.print_help()
        return 2

    if args.data == '-':
        try:
            frames = splitInputFrames(sys.stdin.read())
        except ValueError as e:
            print('错误: %s' % e, file=sys.stderr)
            return 1
        return runDump(_settleFrames(frames), None, args)

    if os.path.isfile(args.data):
        try:
            with open(args.data, 'r', encoding='utf-8-sig', errors='replace') as f:
                frames = splitInputFrames(f.read())
            return runDump(_settleFrames(frames), None, args)
        except ValueError:
            # 非 hex 文本: 尝试按原始二进制读取
            with open(args.data, 'rb') as f:
                data = f.read()
            if not data:
                print('错误: 文件 %s 为空' % args.data, file=sys.stderr)
                return 1
            return runDump([data], None, args)

    try:
        frames = [cleanHex(args.data)]
    except ValueError as e:
        print('错误: %s (输入既不是合法 hex, 也不是存在的文件)' % e,
              file=sys.stderr)
        return 1
    return runDump(frames, None, args)


if __name__ == '__main__':
    sys.exit(main())
