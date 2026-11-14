"""Encodeur de QR codes sans dépendance (mode octet UTF-8, versions 1 à 40, ECC L/M/Q/H).

Algorithme classique (ISO/IEC 18004), dans l'esprit de l'implémentation de référence de
Project Nayuki : segment octet, Reed–Solomon sur GF(256), entrelacement des blocs, motifs
fonctionnels, informations de format et de version, 8 masques départagés par pénalité.
"""
from __future__ import annotations

from html import escape

# Tables par niveau de correction, indexées par version (index 0 inutilisé).
_ECC_PER_BLOCK = {
    "L": (-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    "M": (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),
    "Q": (-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30, 28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    "H": (-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28, 30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
}
_NUM_BLOCKS = {
    "L": (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25),
    "M": (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49),
    "Q": (-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20, 23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68),
    "H": (-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25, 25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81),
}
_FORMAT_ECC = {"L": 1, "M": 0, "Q": 3, "H": 2}
_MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


def _raw_modules(ver: int) -> int:
    """Nombre de modules disponibles pour les données (hors motifs fonctionnels)."""
    n = (16 * ver + 128) * ver + 64
    if ver >= 2:
        na = ver // 7 + 2
        n -= (25 * na - 10) * na - 55
        if ver >= 7:
            n -= 36
    return n


def _data_codewords(ver: int, ecc: str) -> int:
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK[ecc][ver] * _NUM_BLOCKS[ecc][ver]


def _gf_mul(x: int, y: int) -> int:
    """Produit dans GF(2^8) modulo x^8 + x^4 + x^3 + x^2 + 1 (0x11D)."""
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    """Générateur (x - α^0)(x - α^1)…(x - α^(degree-1)), coefficient dominant omis."""
    res, root = [0] * (degree - 1) + [1], 1
    for _ in range(degree):
        for j in range(degree):
            res[j] = _gf_mul(res[j], root)
            if j + 1 < degree:
                res[j] ^= res[j + 1]
        root = _gf_mul(root, 2)
    return res


def _rs_remainder(data: list[int], div: list[int]) -> list[int]:
    res = [0] * len(div)
    for b in data:
        factor = b ^ res.pop(0)
        res.append(0)
        for i, coef in enumerate(div):
            res[i] ^= _gf_mul(coef, factor)
    return res


def _codewords(data: bytes, ver: int, ecc: str) -> list[int]:
    """Flux de bits (mode octet), bourrage, blocs Reed–Solomon puis entrelacement."""
    cc_bits = 8 if ver < 10 else 16
    bits = [0, 1, 0, 0] + [(len(data) >> i) & 1 for i in reversed(range(cc_bits))]
    bits += [(b >> i) & 1 for b in data for i in reversed(range(8))]
    capacity = _data_codewords(ver, ecc) * 8
    bits += [0] * min(4, capacity - len(bits))  # terminateur
    bits += [0] * (-len(bits) % 8)
    words = [sum(bit << (7 - j) for j, bit in enumerate(bits[i:i + 8])) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(words) < capacity // 8:
        words.append(pad)
        pad ^= 0xEC ^ 0x11
    nblocks, ecc_len = _NUM_BLOCKS[ecc][ver], _ECC_PER_BLOCK[ecc][ver]
    raw = _raw_modules(ver) // 8
    nshort, short_len = nblocks - raw % nblocks, raw // nblocks
    div, blocks, k = _rs_divisor(ecc_len), [], 0
    for i in range(nblocks):
        dat = words[k:k + short_len - ecc_len + (0 if i < nshort else 1)]
        k += len(dat)
        ecc_words = _rs_remainder(dat, div)
        if i < nshort:
            dat = dat + [0]  # emplacement factice, ignoré à l'entrelacement
        blocks.append(dat + ecc_words)
    return [blk[i] for i in range(len(blocks[0])) for j, blk in enumerate(blocks)
            if i != short_len - ecc_len or j >= nshort]


def _penalty(m: list[list[bool]]) -> int:
    n, score = len(m), 0
    for line in m + [list(col) for col in zip(*m)]:
        run = 1
        for i in range(1, n + 1):  # règle 1 : séries de 5+ modules identiques
            if i < n and line[i] == line[i - 1]:
                run += 1
            else:
                score += run - 2 if run >= 5 else 0
                run = 1
        s = "".join("1" if v else "0" for v in line)  # règle 3 : motifs de type viseur
        score += 40 * (s.count("10111010000") + s.count("00001011101"))
    for y in range(n - 1):  # règle 2 : carrés 2×2 unis
        for x in range(n - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(map(sum, m))  # règle 4 : équilibre clair/foncé
    return score + 10 * (abs(dark * 100 // (n * n) - 50) // 5)


def matrix(text: str, ecc: str = "M") -> list[list[bool]]:
    """Matrice du QR code (True = module foncé), sans zone de silence."""
    ecc = ecc.upper()
    if ecc not in _FORMAT_ECC:
        raise ValueError(f"niveau de correction inconnu : {ecc!r}")
    data = text.encode("utf-8")
    for ver in range(1, 41):
        cc_bits = 8 if ver < 10 else 16
        if 4 + cc_bits + 8 * len(data) <= _data_codewords(ver, ecc) * 8:
            break
    else:
        raise ValueError("texte trop long pour un QR code")
    size = 17 + 4 * ver
    mods = [[False] * size for _ in range(size)]
    func = [[False] * size for _ in range(size)]

    def put(x: int, y: int, dark: bool) -> None:
        mods[y][x], func[y][x] = dark, True

    for i in range(size):  # motifs de synchronisation
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):  # viseurs + séparateurs
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                if 0 <= cx + dx < size and 0 <= cy + dy < size:
                    put(cx + dx, cy + dy, max(abs(dx), abs(dy)) not in (2, 4))
    if ver >= 2:  # motifs d'alignement
        na = ver // 7 + 2
        step = (ver * 8 + na * 3 + 5) // (na * 4 - 4) * 2
        pos = [6] + sorted(size - 7 - i * step for i in range(na - 1))
        for i, ay in enumerate(pos):
            for j, ax in enumerate(pos):
                if (i, j) in ((0, 0), (0, na - 1), (na - 1, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        put(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)

    def draw_format(mask: int) -> None:
        d = _FORMAT_ECC[ecc] << 3 | mask
        rem = d
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (d << 10 | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1 == 1  # noqa: E731
        for i in range(6):
            put(8, i, bit(i))
        put(8, 7, bit(6))
        put(8, 8, bit(7))
        put(7, 8, bit(8))
        for i in range(9, 15):
            put(14 - i, 8, bit(i))
        for i in range(8):
            put(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            put(8, size - 15 + i, bit(i))
        put(8, size - 8, True)  # module foncé

    draw_format(0)  # réserve les zones de format
    if ver >= 7:  # informations de version (BCH 18,6)
        rem = ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        vbits = ver << 12 | rem
        for i in range(18):
            a, b, dark = size - 11 + i % 3, i // 3, (vbits >> i) & 1 == 1
            put(a, b, dark)
            put(b, a, dark)

    cw, i = _codewords(data, ver, ecc), 0  # placement en zigzag
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                y = size - 1 - vert if (right + 1) & 2 == 0 else vert
                if not func[y][x] and i < len(cw) * 8:
                    mods[y][x] = (cw[i >> 3] >> (7 - (i & 7))) & 1 == 1
                    i += 1
        right -= 2

    def apply_mask(mask: int) -> None:
        f = _MASKS[mask]
        for y in range(size):
            for x in range(size):
                if not func[y][x] and f(x, y):
                    mods[y][x] = not mods[y][x]

    best, best_score = 0, None
    for mask in range(8):
        apply_mask(mask)
        draw_format(mask)
        score = _penalty(mods)
        if best_score is None or score < best_score:
            best, best_score = mask, score
        apply_mask(mask)  # XOR : annule le masque
    apply_mask(best)
    draw_format(best)
    return mods


def svg(text: str, ecc: str = "M", scale: int = 8, border: int = 4, title: str = "QR code") -> str:
    """QR code en <svg> autonome (couleurs explicites : lisible aussi en thème sombre)."""
    m = matrix(text, ecc)
    n = len(m) + 2 * border
    parts = []
    for y, row in enumerate(m):
        x = 0
        while x < len(row):
            if row[x]:
                start = x
                while x < len(row) and row[x]:
                    x += 1
                parts.append(f"M{start + border},{y + border}h{x - start}v1h-{x - start}z")
            else:
                x += 1
    px = n * scale
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" width="{px}" height="{px}" '
            f'role="img" shape-rendering="crispEdges"><title>{escape(title)}</title>'
            f'<rect width="{n}" height="{n}" fill="#fff"/><path d="{"".join(parts)}" fill="#000"/></svg>')
