#!/usr/bin/env python3
"""
show_page_versions.py — Muestra el contenido legible (strings) de cada
versión histórica de una página dentro de un WAL, el diff entre
versiones consecutivas, y opcionalmente posibles pares de coordenadas
(lat/lon) codificados como floats de 8 bytes (REAL de SQLite).

Reutiliza el parser de parse_wal.py (mismo directorio) para no duplicar
la lógica de validación de frames/checksums.

Uso:
    python3 show_page_versions.py <archivo>-wal --page N [opciones]

Opciones:
    --min-len N       Longitud mínima de un string para mostrarlo (default: 4)
    --encoding MODE   ascii (default), utf16le, o ambas ("all")
    --no-diff         Solo listar los strings de cada versión, sin diff
    --find-coords     Además de los strings, buscar posibles pares
                       lat/lon codificados como doubles de 8 bytes
                       (formato REAL de SQLite, big-endian IEEE-754)
"""

import argparse
import difflib
import math
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_wal import parse_wal_header, parse_frames, WalFormatError  # noqa: E402

ASCII_STRING_RE = None  # se arma dinámicamente según --min-len


def extract_ascii_strings(data: bytes, min_len: int):
    """Igual que el comando `strings` de Unix: corridas de bytes
    imprimibles (0x20-0x7e) más algunos espacios en blanco comunes."""
    pattern = re.compile(rb"[\x20-\x7e]{%d,}" % min_len)
    return [m.group().decode("ascii", errors="replace") for m in pattern.finditer(data)]


def extract_utf16le_strings(data: bytes, min_len: int):
    """Busca texto UTF-16LE (común en algunos campos de apps Android),
    aceptando principalmente rango ASCII/Latin-1 intercalado con 0x00."""
    pattern = re.compile(
        rb"(?:[\x20-\x7e]\x00){%d,}" % min_len
    )
    out = []
    for m in pattern.finditer(data):
        try:
            out.append(m.group().decode("utf-16le", errors="replace"))
        except Exception:
            pass
    return out


def strings_for_page_data(data: bytes, min_len: int, encoding: str):
    result = []
    if encoding in ("ascii", "all"):
        result.extend(extract_ascii_strings(data, min_len))
    if encoding in ("utf16le", "all"):
        result.extend(extract_utf16le_strings(data, min_len))
    return result


def find_coordinate_pairs(data: bytes, lat_range=(-90.0, 90.0), lon_range=(-180.0, 180.0)):
    """Escaneo byte a byte (no alineado, porque el contenido de un
    registro SQLite no está alineado a 8 bytes dentro de la página)
    buscando valores REAL (double de 8 bytes, big-endian IEEE-754,
    tal cual el formato de registro de SQLite) que caigan en rango de
    coordenadas, y que además estén seguidos 8 bytes después por otro
    valor también válido — es decir, un candidato a par (lat, lon)
    consecutivo tal como quedarían dos columnas seguidas en un mismo
    registro.

    Devuelve una lista de tuplas (offset, val1, val2) ordenada por
    offset. Son CANDIDATOS: hay que verificarlos a ojo (por ejemplo
    contra un mapa), esto no reemplaza confirmar el dato.
    """
    n = len(data)

    def is_plausible(v, lo, hi):
        # descarta NaN/Inf, exactamente 0, y también los "casi cero"
        # (floats subnormales que aparecen al interpretar como double
        # bytes que en realidad son espacio libre de la página / ceros
        # de relleno — no son coordenadas reales, son ruido)
        return math.isfinite(v) and abs(v) > 1e-4 and lo <= v <= hi

    doubles = {}
    for off in range(0, n - 7):
        val = struct.unpack_from(">d", data, off)[0]
        if is_plausible(val, *lon_range):  # lon_range es el rango más amplio, cubre ambos
            doubles[off] = val

    pairs = []
    seen_starts = set()
    for off, val in sorted(doubles.items()):
        off2 = off + 8
        if off2 in doubles and off not in seen_starts:
            val2 = doubles[off2]
            lat_first = is_plausible(val, *lat_range)
            lat_second = is_plausible(val2, *lat_range)
            if lat_first or lat_second:
                pairs.append((off, val, val2))
                seen_starts.add(off)
                seen_starts.add(off2)
    return pairs


def main():
    ap = argparse.ArgumentParser(description="Mostrar y comparar versiones de una página a través del WAL")
    ap.add_argument("wal_path", help="ruta al archivo *-wal")
    ap.add_argument("--page", type=int, required=True, help="número de página a inspeccionar")
    ap.add_argument("--min-len", type=int, default=4, help="longitud mínima de string a mostrar (default 4)")
    ap.add_argument("--encoding", choices=["ascii", "utf16le", "all"], default="ascii",
                     help="cómo interpretar el texto dentro de la página (default: ascii)")
    ap.add_argument("--no-diff", action="store_true", help="no mostrar el diff entre versiones consecutivas")
    ap.add_argument("--find-coords", action="store_true",
                     help="además del texto, buscar posibles pares lat/lon (doubles de 8 bytes)")
    args = ap.parse_args()

    if not os.path.exists(args.wal_path):
        print(f"Error: no existe el archivo {args.wal_path}", file=sys.stderr)
        sys.exit(1)

    with open(args.wal_path, "rb") as fh:
        buf = fh.read()

    try:
        header = parse_wal_header(buf)
    except WalFormatError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    frames, _stopped_reason = parse_frames(buf, header)
    versions = [f for f in frames if f["valid"] and f["page_number"] == args.page]

    if not versions:
        print(f"No se encontraron versiones válidas de la página {args.page} en este WAL.")
        return

    print(f"Página {args.page}: {len(versions)} versión(es) encontrada(s) en el WAL "
          f"(orden cronológico, la última es la vigente)\n")

    all_entries = []  # strings + (si --find-coords) descripciones de pares de coordenadas, para el diff
    for i, f in enumerate(versions, start=1):
        commit_tag = " [COMMIT]" if f["is_commit"] else ""
        print("=" * 72)
        print(f"Versión {i}/{len(versions)} — frame {f['frame_index']} "
              f"(offset 0x{f['offset']:x}){commit_tag}")
        print("=" * 72)
        strs = strings_for_page_data(f["page_data"], args.min_len, args.encoding)
        entries = list(strs)

        if strs:
            for s in strs:
                print(f"  {s}")
        else:
            print("  (sin texto legible con estos parámetros — probar --encoding all o bajar --min-len)")

        if args.find_coords:
            pairs = find_coordinate_pairs(f["page_data"])
            if pairs:
                print("\n  Posibles coordenadas (candidatas — verificar antes de dar por buenas):")
                for off, v1, v2 in pairs:
                    desc = f"[coord] offset 0x{off:x}: {v1:.6f}, {v2:.6f}"
                    print(f"    {desc}")
                    entries.append(desc)
            elif strs:
                print("\n  (sin candidatos a coordenadas en esta versión)")

        all_entries.append(entries)
        print()

    if not args.no_diff and len(versions) > 1:
        print("=" * 72)
        print("DIFF entre versiones consecutivas")
        print("=" * 72)
        for i in range(1, len(versions)):
            prev_f, cur_f = versions[i - 1], versions[i]
            prev_entries, cur_entries = all_entries[i - 1], all_entries[i]
            diff = list(difflib.unified_diff(
                prev_entries, cur_entries,
                fromfile=f"frame {prev_f['frame_index']}",
                tofile=f"frame {cur_f['frame_index']}",
                lineterm="",
            ))
            print(f"\n--- versión {i} (frame {prev_f['frame_index']}) -> "
                  f"versión {i+1} (frame {cur_f['frame_index']}) ---")
            if diff:
                for line in diff:
                    print(line)
            else:
                print("  (sin diferencias en el texto extraído)")


if __name__ == "__main__":
    main()
