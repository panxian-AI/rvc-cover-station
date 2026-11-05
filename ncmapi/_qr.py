# -*- coding: utf-8 -*-
"""极简二维码编码器（纯标准库，无第三方依赖）。

为什么自己写：本产品要在「全新安装、什么都没 pip 装」的机器上生成登录二维码，
不能依赖 qrcode/PIL。这里只实现扫码登录真正需要的那一小块能力 ——
字节模式、纠错等级 M、版本 1~10（最多 213 字节），足够放下
https://music.163.com/login?codekey=<uuid> 这种 60 字节左右的链接。

算法按 ISO/IEC 18004 实现：
  数据编码 → 分块 + RS 纠错 → 交织 → 功能图形 → 数据放置 → 8 种掩码择优 → 格式信息

对外只给两个函数：
  encode(text)                  -> 二维 0/1 列表（True 表示黑块），不含静区
  to_ppm(matrix, scale, border) -> P6 二进制，可直接喂给 tkinter PhotoImage
"""

# ---------------------------------------------------------------- 基础表
# 版本 1~10、纠错等级 M 的分块表：(每块数据码字数, 块数) × 2 组
# 第三项是该版本总码字数，用来反推纠错码字数
_BLOCKS_M = {
    1:  ([(16, 1)], 26),
    2:  ([(28, 1)], 44),
    3:  ([(44, 1)], 70),
    4:  ([(32, 2)], 100),
    5:  ([(43, 2)], 134),
    6:  ([(27, 4)], 172),
    7:  ([(31, 4)], 196),
    8:  ([(38, 2), (39, 2)], 242),
    9:  ([(36, 3), (37, 2)], 292),
    10: ([(43, 4), (44, 1)], 346),
}

# 版本 1~10 的对齐图形中心坐标
_ALIGN = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46],
    10: [6, 28, 50],
}

# 剩余位（数据码字放完后，矩阵里多出来的空位）
_REMAINDER = {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7, 7: 0, 8: 0, 9: 0, 10: 0}

_EC_LEVEL_M_BITS = 0b00


def _gf_tables():
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_EXP, _LOG = _gf_tables()


def _gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(n):
    """生成多项式 (x - a^0)(x - a^1)...(x - a^(n-1))，最高次系数省略。"""
    g = [1]
    for i in range(n):
        # g = g * (x + a^i)
        ng = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            ng[j] ^= c
            ng[j + 1] ^= _gf_mul(c, _EXP[i])
        g = ng
    return g


def _rs_ecc(data, n):
    g = _rs_generator(n)
    rem = list(data) + [0] * n
    for i in range(len(data)):
        coef = rem[i]
        if coef == 0:
            continue
        for j, gc in enumerate(g):
            rem[i + j] ^= _gf_mul(gc, coef)
    return rem[len(data):]


# ---------------------------------------------------------------- 数据编码
def _utf8_bytes(text):
    return text.encode("utf-8")


def _count_bits(ver):
    return 8 if ver <= 9 else 16


def _bitstream(data, ver):
    """字节模式：0100 + 字符数 + 数据 + 终止符 + 补齐到字节。"""
    bits = []

    def put(val, n):
        for i in range(n - 1, -1, -1):
            bits.append((val >> i) & 1)

    put(0b0100, 4)
    put(len(data), _count_bits(ver))
    for b in data:
        put(b, 8)
    capacity = _data_capacity(ver) * 8
    if len(bits) > capacity:
        raise ValueError("内容过长：%d 位 > 版本 %d 的 %d 位" % (len(bits), ver, capacity))
    put(0, min(4, capacity - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    pad = [0xEC, 0x11]
    k = 0
    while len(bits) < capacity:
        put(pad[k % 2], 8)
        k += 1
    return bits


def _data_capacity(ver):
    groups, _ = _BLOCKS_M[ver]
    return sum(cnt * num for cnt, num in groups)


def _pick_version(nbytes):
    for ver in range(1, 11):
        need = 4 + _count_bits(ver) + nbytes * 8
        if need <= _data_capacity(ver) * 8:
            return ver
    raise ValueError("内容过长：%d 字节超出本编码器支持范围（版本 1~10）" % nbytes)


def _codewords(text, ver):
    """返回交织后的最终码字序列（数据块 + 纠错块按规范交织）。"""
    data = _utf8_bytes(text)
    bits = _bitstream(data, ver)
    total = _BLOCKS_M[ver][1]
    data_total = _data_capacity(ver)
    ecc_per_block = (total - data_total) // sum(num for _, num in _BLOCKS_M[ver][0])

    # 切成字节
    words = []
    for i in range(0, len(bits), 8):
        v = 0
        for b in bits[i:i + 8]:
            v = (v << 1) | b
        words.append(v)

    # 分块
    blocks, pos = [], 0
    for cnt, num in _BLOCKS_M[ver][0]:
        for _ in range(num):
            blocks.append(words[pos:pos + cnt])
            pos += cnt

    eccs = [_rs_ecc(blk, ecc_per_block) for blk in blocks]

    out = []
    for i in range(max(len(b) for b in blocks)):
        for b in blocks:
            if i < len(b):
                out.append(b[i])
    for i in range(ecc_per_block):
        for e in eccs:
            out.append(e[i])
    return out


# ---------------------------------------------------------------- 矩阵构建
class _Matrix:
    def __init__(self, ver):
        self.ver = ver
        self.size = ver * 4 + 17
        self.m = [[0] * self.size for _ in range(self.size)]
        self.fn = [[False] * self.size for _ in range(self.size)]   # 功能图形占位

    def _set_fn(self, r, c, dark):
        if 0 <= r < self.size and 0 <= c < self.size:
            self.m[r][c] = 1 if dark else 0
            self.fn[r][c] = True

    def finder(self, r0, c0):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = r0 + dr, c0 + dc
                if not (0 <= r < self.size and 0 <= c < self.size):
                    continue
                if 0 <= dr <= 6 and 0 <= dc <= 6:
                    dark = (dr in (0, 6) or dc in (0, 6)
                            or (2 <= dr <= 4 and 2 <= dc <= 4))
                else:
                    dark = False
                self._set_fn(r, c, dark)

    def alignment(self, cr, cc):
        for dr in range(-2, 3):
            for dc in range(-2, 3):
                dark = max(abs(dr), abs(dc)) != 1
                self._set_fn(cr + dr, cc + dc, dark)

    def patterns(self):
        s = self.size
        self.finder(0, 0)
        self.finder(0, s - 7)
        self.finder(s - 7, 0)
        # 定时图形
        for i in range(8, s - 8):
            self._set_fn(6, i, i % 2 == 0)
            self._set_fn(i, 6, i % 2 == 0)
        # 对齐图形（跳过与定位图形重叠的三个）
        centers = _ALIGN[self.ver]
        last = centers[-1] if centers else None
        for r in centers:
            for c in centers:
                if (r, c) in ((6, 6), (6, last), (last, 6)):
                    continue
                self.alignment(r, c)
        # 先占位格式信息区，避免数据写进去（真实值稍后填）
        # 注意：第 6 行/列是定时图形，不能当格式区清零
        for i in range(9):
            if i != 6:
                self._set_fn(8, i, False)
                self._set_fn(i, 8, False)
        for i in range(8):
            self._set_fn(8, s - 1 - i, False)
            self._set_fn(s - 1 - i, 8, False)
        # 版本信息区（版本 >= 7）
        if self.ver >= 7:
            for i in range(6):
                for j in range(3):
                    self._set_fn(i, s - 11 + j, False)
                    self._set_fn(s - 11 + j, i, False)

    def place(self, codewords):
        bits = []
        for w in codewords:
            for i in range(7, -1, -1):
                bits.append((w >> i) & 1)
        bits += [0] * _REMAINDER[self.ver]
        s = self.size
        idx = 0
        right = s - 1
        upward = True
        while right >= 1:
            if right == 6:
                right -= 1
            for vert in range(s):
                for j in range(2):
                    c = right - j
                    r = (s - 1 - vert) if upward else vert
                    if not self.fn[r][c]:
                        self.m[r][c] = bits[idx] if idx < len(bits) else 0
                        idx += 1
            upward = not upward
            right -= 2

    def format_info(self, mask):
        data = (_EC_LEVEL_M_BITS << 3) | mask
        d = data << 10
        g = 0x537
        while d.bit_length() >= 11:
            d ^= g << (d.bit_length() - 11)
        val = ((data << 10) | d) ^ 0x5412

        def bit(i):
            return (val >> i) & 1

        s = self.size
        # 竖条（列 8）：bit0~5 在上方，bit6/7 跳过第 6 行，bit8~14 在左下
        for i in range(15):
            r = i if i < 6 else (i + 1 if i < 8 else s - 15 + i)
            self._set_fn(r, 8, bit(i))
        # 横条（行 8）：bit0~7 在右上，bit8 在第 7 列，bit9~14 在左上（跳过第 6 列）
        for i in range(15):
            c = s - 1 - i if i < 8 else (7 if i < 9 else 15 - i - 1)
            self._set_fn(8, c, bit(i))
        # 固定黑模块（规范要求恒黑，必须最后落笔，否则会被 bit7 覆盖）
        self._set_fn(s - 8, 8, True)

    def version_info(self):
        if self.ver < 7:
            return
        d = self.ver << 12
        g = 0x1F25
        while d.bit_length() >= 13:
            d ^= g << (d.bit_length() - 13)
        val = (self.ver << 12) | d
        s = self.size
        for i in range(18):
            b = (val >> i) & 1
            r, c = i // 3, i % 3
            self._set_fn(r, s - 11 + c, b)
            self._set_fn(s - 11 + c, r, b)

    def apply_mask(self, mask):
        s = self.size
        out = [[0] * s for _ in range(s)]
        for r in range(s):
            for c in range(s):
                keep = self.m[r][c]
                if not self.fn[r][c] and _mask_bit(mask, r, c):
                    keep ^= 1
                out[r][c] = keep
        return out


def _mask_bit(mask, i, j):
    if mask == 0:
        return (i + j) % 2 == 0
    if mask == 1:
        return i % 2 == 0
    if mask == 2:
        return j % 3 == 0
    if mask == 3:
        return (i + j) % 3 == 0
    if mask == 4:
        return (i // 2 + j // 3) % 2 == 0
    if mask == 5:
        return (i * j) % 2 + (i * j) % 3 == 0
    if mask == 6:
        return ((i * j) % 2 + (i * j) % 3) % 2 == 0
    return ((i + j) % 2 + (i * j) % 3) % 2 == 0


def _penalty(m):
    """规范里的四条扣分规则，分数越低越好扫。"""
    s = len(m)
    score = 0
    # 规则 1：行/列上连续同色 >= 5
    for line in list(m) + [list(col) for col in zip(*m)]:
        run, prev = 1, line[0]
        for v in line[1:]:
            if v == prev:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run, prev = 1, v
        if run >= 5:
            score += 3 + (run - 5)
    # 规则 2：2x2 同色块
    for r in range(s - 1):
        row0, row1 = m[r], m[r + 1]
        for c in range(s - 1):
            if row0[c] == row0[c + 1] == row1[c] == row1[c + 1]:
                score += 3
    # 规则 3：类似定位图形的 1:1:3:1:1
    pat_a = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    pat_b = pat_a[::-1]
    for line in list(m) + [list(col) for col in zip(*m)]:
        for i in range(len(line) - 10):
            seg = line[i:i + 11]
            if seg == pat_a or seg == pat_b:
                score += 40
    # 规则 4：黑白比例偏离 50%
    dark = sum(sum(row) for row in m)
    ratio = dark * 100 // (s * s)
    score += 10 * (abs(ratio - 50) // 5)
    return score


def encode(text):
    """把文本编成二维码矩阵，返回 (matrix, version, mask)。

    matrix 是 0/1 的二维列表，不含静区；True/1 表示黑块。
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    ver = _pick_version(len(_utf8_bytes(text)))
    codewords = _codewords(text, ver)

    best = None
    for mask in range(8):
        mx = _Matrix(ver)
        mx.patterns()
        mx.place(codewords)
        mx.format_info(mask)
        mx.version_info()
        grid = mx.apply_mask(mask)
        # 格式信息本身也要跟着掩码一起重画（apply_mask 保留功能位，这里已正确）
        p = _penalty(grid)
        if best is None or p < best[0]:
            best = (p, grid, mask)
    return best[1], ver, best[2]


def to_ppm(matrix, scale=6, border=4):
    """转成 P6 PPM 字节串，tkinter 可直接 PhotoImage(file=...)。

    scale 是每个模块的边长（像素），border 是四周留白（模块数，规范建议 4）。
    """
    s = len(matrix)
    m = s + border * 2
    dim = m * scale
    white = b"\xff" * (3 * scale)          # 一个放大后的白像素
    black = b"\x00" * (3 * scale)
    blank_row = white * m
    out = bytearray()
    for r in range(m):
        if r < border or r >= s + border:
            row = blank_row
        else:
            cells = []
            src = matrix[r - border]
            for c in range(m):
                if c < border or c >= s + border:
                    cells.append(white)
                else:
                    cells.append(black if src[c - border] else white)
            row = b"".join(cells)
        out += row * scale                  # 纵向放大 = 整行重复 scale 次
    return b"P6\n%d %d\n255\n" % (dim, dim) + bytes(out)


def to_svg(matrix, border=3, dark="#111827", light="#ffffff"):
    """转成 SVG 字符串，直接塞进网页 <div> 里就能显示（不用生成图片文件）。

    每个黑模块画成一个 1x1 的方块，用 path 一次性画完 —— 版本 10 是 57x57，
    逐格 <rect> 会有三千多个节点，path 只有一条。
    """
    s = len(matrix)
    n = s + border * 2
    parts = []
    for r in range(s):
        row = matrix[r]
        c = 0
        while c < s:
            if row[c]:
                start = c
                while c < s and row[c]:
                    c += 1
                parts.append("M%d %dh%dv1H%dZ" % (start + border, r + border,
                                                  c - start, start + border))
            else:
                c += 1
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
        'width="100%%" height="100%%" shape-rendering="crispEdges" '
        'role="img" aria-label="登录二维码">'
        '<rect width="%d" height="%d" fill="%s"/>'
        '<path d="%s" fill="%s"/></svg>'
        % (n, n, n, n, light, "".join(parts), dark)
    )


if __name__ == "__main__":
    import sys
    txt = sys.argv[1] if len(sys.argv) > 1 else "https://music.163.com/login?codekey=test"
    mat, ver, mask = encode(txt)
    print("version=%d mask=%d size=%d" % (ver, mask, len(mat)))
    for row in mat:
        print("".join("##" if v else "  " for v in row))
