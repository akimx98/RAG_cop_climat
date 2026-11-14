import pytest

from cop_corpus import qr

URL = "https://cop31.akimx.fr/rejoindre/53kymqq"


def _finder_ok(m, x0, y0):
    for dy in range(7):
        for dx in range(7):
            ring = max(abs(dx - 3), abs(dy - 3))
            if m[y0 + dy][x0 + dx] != (ring != 2):
                return False
    return True


@pytest.mark.parametrize("ecc", "LMQH")
@pytest.mark.parametrize("text", ["A", URL, "Négociations COP31 — Antalya", "x" * 200])
def test_structure(text, ecc):
    m = qr.matrix(text, ecc)
    n = len(m)
    assert all(len(row) == n for row in m)
    assert (n - 17) % 4 == 0 and 1 <= (n - 17) // 4 <= 40
    for x0, y0 in ((0, 0), (n - 7, 0), (0, n - 7)):
        assert _finder_ok(m, x0, y0)
    assert m[n - 8][8]  # module foncé
    assert all(m[6][i] == (i % 2 == 0) for i in range(8, n - 8))  # synchronisation


def test_version_choice():
    assert len(qr.matrix("A", "M")) == 21  # version 1
    assert len(qr.matrix("x" * 200, "M")) > 17 + 4 * 7  # infos de version présentes


def test_format_bits_reference():
    # ECC M, masque quelconque : les 15 bits lus doivent correspondre à une entrée valide.
    valid = set()
    for mask in range(8):
        d = qr._FORMAT_ECC["M"] << 3 | mask
        rem = d
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        valid.add((d << 10 | rem) ^ 0x5412)
    assert 0b101010000010010 in valid  # valeur de référence ISO pour M / masque 0
    m = qr.matrix(URL, "M")
    cells = [(8, i) for i in range(6)] + [(8, 7), (8, 8), (7, 8)] + [(14 - i, 8) for i in range(9, 15)]
    bits = sum(int(m[y][x]) << i for i, (x, y) in enumerate(cells))
    assert bits in valid


def test_svg():
    out = qr.svg(URL, title="Rejoindre <COP31> & co")
    assert out.startswith("<svg") and out.endswith("</svg>")
    assert 'shape-rendering="crispEdges"' in out and 'role="img"' in out
    assert "<title>Rejoindre &lt;COP31&gt; &amp; co</title>" in out
    assert out.count("<path ") == 1 and 'fill="#000"' in out and 'fill="#fff"' in out
    n = len(qr.matrix(URL)) + 8
    assert f'viewBox="0 0 {n} {n}"' in out and f'width="{n * 8}"' in out


def test_errors():
    with pytest.raises(ValueError):
        qr.matrix("x" * 3000, "L")
    with pytest.raises(ValueError):
        qr.matrix("a", "Z")
