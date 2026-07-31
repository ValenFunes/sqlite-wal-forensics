#!/usr/bin/env python3
"""
show_page_versions.py — Muestra el contenido legible (strings) de cada
versión histórica de una página dentro de un WAL, y el diff entre
versiones consecutivas.

Reutiliza el parser de parse_wal.py (mismo directorio) para no duplicar
la lógica de validación de frames/checksums.

Uso:
    python3 show_page_versions.py <archivo>-wal --page N [opciones]

Opciones:
    --min-len N      Longitud mínima de un string para mostrarlo (default: 4)
    --encoding MODE  ascii (default), utf16le, o ambas ("all")
    --no-diff        Solo listar los strings de cada versión, sin diff
    --context N      Líneas de contexto alrededor de cada cambio en el diff (default: 0, o sea todo el diff completo)
"""

import argparse
import difflib
import os
import re
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


def main():
    ap = argparse.ArgumentParser(description="Mostrar y comparar versiones de una página a través del WAL")
    ap.add_argument("wal_path", help="ruta al archivo *-wal")
    ap.add_argument("--page", type=int, required=True, help="número de página a inspeccionar")
    ap.add_argument("--min-len", type=int, default=4, help="longitud mínima de string a mostrar (default 4)")
    ap.add_argument("--encoding", choices=["ascii", "utf16le", "all"], default="ascii",
                     help="cómo interpretar el texto dentro de la página (default: ascii)")
    ap.add_argument("--no-diff", action="store_true", help="no mostrar el diff entre versiones consecutivas")
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

    all_strings = []
    for i, f in enumerate(versions, start=1):
        commit_tag = " [COMMIT]" if f["is_commit"] else ""
        print("=" * 72)
        print(f"Versión {i}/{len(versions)} — frame {f['frame_index']} "
              f"(offset 0x{f['offset']:x}){commit_tag}")
        print("=" * 72)
        strs = strings_for_page_data(f["page_data"], args.min_len, args.encoding)
        all_strings.append(strs)
        if strs:
            for s in strs:
                print(f"  {s}")
        else:
            print("  (sin texto legible con estos parámetros — probar --encoding all o bajar --min-len)")
        print()

    if not args.no_diff and len(versions) > 1:
        print("=" * 72)
        print("DIFF entre versiones consecutivas")
        print("=" * 72)
        for i in range(1, len(versions)):
            prev_f, cur_f = versions[i - 1], versions[i]
            prev_strs, cur_strs = all_strings[i - 1], all_strings[i]
            diff = list(difflib.unified_diff(
                prev_strs, cur_strs,
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
