#!/usr/bin/env python3
"""
parse_wal.py — Parser forense de archivos "-wal" de SQLite.

Implementa el formato descrito en:
  https://sqlite.org/fileformat2.html#walformat  (frames del WAL)
  https://sqlite.org/walformat.html               (wal-index / -shm, referencia)

Uso:
    python3 parse_wal.py <archivo.db-wal> [opciones]

Opciones:
    --page N            Mostrar solo los frames que corresponden a la página N
    --dump-dir DIR       Volcar el contenido crudo de cada frame válido a
                          DIR/frame_<n>_page_<p>.bin (útil para recuperación
                          forense de versiones históricas de una página)
    --show-corrupt        Además del resumen, listar el detalle del/los
                          frame(s) inválidos encontrados (si los hay)
    --json PATH           Además del reporte en texto, guardar un volcado
                          JSON con todos los frames en PATH

Sin ninguna opción, imprime un reporte legible en la terminal con:
  - Datos del header del WAL (magic, versión, page size, salts, etc.)
  - Un listado de todos los frames válidos (página, si es commit, etc.)
  - Un resumen final: mxFrame, cantidad de transacciones (commits),
    páginas tocadas, y si se encontró corrupción / frames descartados.

Notas de diseño:
  - Un WAL puede "reciclarse": frames viejos de checkpoints anteriores
    pueden quedar físicamente en el archivo pero ya no son válidos.
    Por eso la validez de un frame depende de que sus valores de
    salt-1/salt-2 coincidan con los del header actual del WAL Y de que
    su checksum encadenado sea correcto. En cuanto aparece el primer
    frame inválido, el escaneo se detiene ahí (así es como SQLite hace
    recovery): todo lo que sigue se considera basura / transacción
    incompleta.
  - No se requiere el archivo -shm (wal-index) para nada de esto: ese
    archivo es solo una caché transitoria y se puede reconstruir
    íntegramente a partir del -wal, por lo que este script lo ignora.
"""

import argparse
import json
import os
import struct
import sys

WAL_HEADER_SIZE = 32
FRAME_HEADER_SIZE = 24

MAGIC_BE = 0x377F0683  # checksums big-endian
MAGIC_LE = 0x377F0682  # checksums little-endian


class WalFormatError(Exception):
    pass


def checksum(data: bytes, endian: str, s0: int = 0, s1: int = 0):
    """Algoritmo de checksum de SQLite (sección 4.2 de fileformat2.html).

    Opera sobre pares de enteros de 32 bits. `data` debe tener una
    longitud múltiplo de 8 bytes.
    """
    if len(data) % 8 != 0:
        raise WalFormatError("checksum input must be a multiple of 8 bytes")
    fmt = (">" if endian == "big" else "<") + "I"
    mask = 0xFFFFFFFF
    for i in range(0, len(data), 8):
        x0 = struct.unpack_from(fmt, data, i)[0]
        x1 = struct.unpack_from(fmt, data, i + 4)[0]
        s0 = (s0 + x0 + s1) & mask
        s1 = (s1 + x1 + s0) & mask
    return s0, s1


def parse_wal_header(buf: bytes):
    if len(buf) < WAL_HEADER_SIZE:
        raise WalFormatError("archivo demasiado chico para tener un header de WAL válido")

    magic = struct.unpack_from(">I", buf, 0)[0]
    if magic == MAGIC_BE:
        endian = "big"
    elif magic == MAGIC_LE:
        endian = "little"
    else:
        raise WalFormatError(
            f"magic number inválido: 0x{magic:08x} (se esperaba 0x377f0682 o 0x377f0683). "
            "Este archivo no parece ser un -wal de SQLite."
        )

    fmt = ">I"
    file_format_version = struct.unpack_from(fmt, buf, 4)[0]
    page_size = struct.unpack_from(fmt, buf, 8)[0]
    ckpt_seq = struct.unpack_from(fmt, buf, 12)[0]
    salt1 = struct.unpack_from(fmt, buf, 16)[0]
    salt2 = struct.unpack_from(fmt, buf, 20)[0]
    cksum1 = struct.unpack_from(fmt, buf, 24)[0]
    cksum2 = struct.unpack_from(fmt, buf, 28)[0]

    calc_s0, calc_s1 = checksum(buf[0:24], endian)
    header_checksum_ok = (calc_s0 == cksum1 and calc_s1 == cksum2)

    return {
        "magic": magic,
        "endian": endian,
        "file_format_version": file_format_version,
        "page_size": 65536 if page_size == 1 else page_size,
        "checkpoint_seq": ckpt_seq,
        "salt1": salt1,
        "salt2": salt2,
        "checksum1": cksum1,
        "checksum2": cksum2,
        "header_checksum_ok": header_checksum_ok,
    }


def parse_frames(buf: bytes, header: dict):
    """Recorre los frames desde el final del header hasta encontrar el
    primer frame inválido o el final del archivo. Devuelve (frames, extra)."""
    page_size = header["page_size"]
    endian = header["endian"]
    frame_size = FRAME_HEADER_SIZE + page_size

    offset = WAL_HEADER_SIZE
    frame_index = 0
    frames = []

    # el checksum se encadena empezando por el checksum del propio header del WAL
    s0, s1 = header["checksum1"], header["checksum2"]

    stopped_reason = None

    while True:
        if offset + frame_size > len(buf):
            if offset < len(buf):
                stopped_reason = (
                    f"quedan {len(buf) - offset} bytes sueltos al final del archivo "
                    f"(menos que un frame completo de {frame_size} bytes) — "
                    "probablemente una escritura interrumpida (crash a mitad de un frame)."
                )
            else:
                stopped_reason = "fin de archivo"
            break

        frame_index += 1
        fh = buf[offset:offset + FRAME_HEADER_SIZE]
        page_number = struct.unpack_from(">I", fh, 0)[0]
        db_size_after_commit = struct.unpack_from(">I", fh, 4)[0]
        f_salt1 = struct.unpack_from(">I", fh, 8)[0]
        f_salt2 = struct.unpack_from(">I", fh, 12)[0]
        f_cksum1 = struct.unpack_from(">I", fh, 16)[0]
        f_cksum2 = struct.unpack_from(">I", fh, 20)[0]

        page_data = buf[offset + FRAME_HEADER_SIZE: offset + frame_size]

        salts_match = (f_salt1 == header["salt1"] and f_salt2 == header["salt2"])

        # el checksum encadenado cubre: header del frame (primeros 8 bytes,
        # es decir SIN los 16 bytes de salt+checksum) + el contenido de la página
        new_s0, new_s1 = checksum(fh[0:8] + page_data, endian, s0, s1)
        checksum_ok = (new_s0 == f_cksum1 and new_s1 == f_cksum2)

        frame_valid = salts_match and checksum_ok

        frame = {
            "frame_index": frame_index,
            "offset": offset,
            "page_number": page_number,
            "is_commit": db_size_after_commit != 0,
            "db_size_after_commit": db_size_after_commit if db_size_after_commit else None,
            "salt1": f_salt1,
            "salt2": f_salt2,
            "checksum1": f_cksum1,
            "checksum2": f_cksum2,
            "salts_match_header": salts_match,
            "checksum_ok": checksum_ok,
            "valid": frame_valid,
            "page_data": page_data,
        }

        if not frame_valid:
            reason = []
            if not salts_match:
                reason.append("salt no coincide con el header del WAL (frame de un checkpoint anterior)")
            if not checksum_ok:
                reason.append("checksum encadenado no coincide (corrupción o escritura incompleta)")
            frame["invalid_reason"] = "; ".join(reason)
            frames.append(frame)
            stopped_reason = f"frame #{frame_index} inválido: {frame['invalid_reason']}"
            break

        # solo avanzamos el checksum encadenado mientras los frames son válidos
        s0, s1 = new_s0, new_s1
        frames.append(frame)
        offset += frame_size

    return frames, stopped_reason


def format_report(path, header, frames, stopped_reason, show_corrupt=False):
    lines = []
    lines.append("=" * 72)
    lines.append(f"WAL file: {path}")
    lines.append("=" * 72)
    lines.append("")
    lines.append("-- Header --")
    lines.append(f"  Magic number       : 0x{header['magic']:08x} "
                  f"(checksums {'big' if header['endian']=='big' else 'little'}-endian)")
    lines.append(f"  Formato de archivo : {header['file_format_version']}")
    lines.append(f"  Page size          : {header['page_size']} bytes")
    lines.append(f"  Checkpoint seq #   : {header['checkpoint_seq']}")
    lines.append(f"  Salt-1 / Salt-2    : {header['salt1']} / {header['salt2']}")
    lines.append(f"  Checksum de header : {'OK' if header['header_checksum_ok'] else 'INVALIDO'}")
    lines.append("")

    valid_frames = [f for f in frames if f["valid"]]
    invalid_frames = [f for f in frames if not f["valid"]]

    lines.append(f"-- Frames ({len(valid_frames)} válidos de {len(frames)} leídos) --")
    for f in valid_frames:
        tag = "COMMIT" if f["is_commit"] else "  ..  "
        extra = f" (tamaño BD tras commit: {f['db_size_after_commit']} páginas)" if f["is_commit"] else ""
        lines.append(f"  frame {f['frame_index']:>5} [{tag}]  página {f['page_number']:>8}"
                      f"  offset 0x{f['offset']:x}{extra}")

    if invalid_frames and show_corrupt:
        lines.append("")
        lines.append("-- Frame(s) inválido(s) --")
        for f in invalid_frames:
            lines.append(f"  frame {f['frame_index']} en offset 0x{f['offset']:x}: {f['invalid_reason']}")

    lines.append("")
    lines.append("-- Resumen --")
    lines.append(f"  mxFrame (último frame válido)        : {len(valid_frames)}")
    commits = [f for f in valid_frames if f["is_commit"]]
    lines.append(f"  Transacciones completas (commits)    : {len(commits)}")
    pages_touched = sorted(set(f["page_number"] for f in valid_frames))
    lines.append(f"  Páginas distintas tocadas             : {len(pages_touched)} -> {pages_touched}")
    if commits:
        lines.append(f"  Tamaño final de la BD (tras último commit): {commits[-1]['db_size_after_commit']} páginas")
    lines.append(f"  Motivo de fin de escaneo              : {stopped_reason}")
    if invalid_frames:
        lines.append(f"  Frames descartados por inválidos      : {len(invalid_frames)}"
                      " (usar --show-corrupt para el detalle)")

    # historial por página: útil para forense (versiones sucesivas de una misma página)
    from collections import defaultdict
    history = defaultdict(list)
    for f in valid_frames:
        history[f["page_number"]].append(f["frame_index"])
    repeated = {p: idxs for p, idxs in history.items() if len(idxs) > 1}
    if repeated:
        lines.append("")
        lines.append("-- Páginas con múltiples versiones en el WAL (historial) --")
        for p, idxs in sorted(repeated.items()):
            lines.append(f"  página {p}: frames {idxs}  (la vigente es la última: frame {idxs[-1]})")

    lines.append("=" * 72)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Parser forense de archivos -wal de SQLite")
    ap.add_argument("wal_path", help="ruta al archivo *-wal")
    ap.add_argument("--page", type=int, default=None,
                     help="mostrar solo los frames de esta página")
    ap.add_argument("--dump-dir", default=None,
                     help="directorio donde volcar el contenido crudo de cada frame válido")
    ap.add_argument("--show-corrupt", action="store_true",
                     help="mostrar detalle de frames inválidos/corruptos encontrados")
    ap.add_argument("--json", default=None,
                     help="además del reporte, guardar un volcado JSON de todos los frames en esta ruta")
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

    frames, stopped_reason = parse_frames(buf, header)

    if args.page is not None:
        frames_to_show = [f for f in frames if f["page_number"] == args.page]
        if not frames_to_show:
            print(f"No se encontraron frames para la página {args.page}.")
        else:
            print(f"Frames encontrados para la página {args.page} (orden cronológico):")
            for f in frames_to_show:
                status = "válido" if f["valid"] else f"INVALIDO ({f.get('invalid_reason')})"
                commit_tag = " [COMMIT]" if f.get("is_commit") else ""
                print(f"  frame {f['frame_index']} @ offset 0x{f['offset']:x} — {status}{commit_tag}")
            last_valid = [f for f in frames_to_show if f["valid"]]
            if last_valid:
                print(f"\nLa versión vigente de la página {args.page} es la del frame "
                      f"{last_valid[-1]['frame_index']} (las anteriores son historial).")
    else:
        print(format_report(args.wal_path, header, frames, stopped_reason, show_corrupt=args.show_corrupt))

    if args.dump_dir:
        os.makedirs(args.dump_dir, exist_ok=True)
        count = 0
        for f in frames:
            if not f["valid"]:
                continue
            if args.page is not None and f["page_number"] != args.page:
                continue
            fname = f"frame_{f['frame_index']:05d}_page_{f['page_number']}.bin"
            with open(os.path.join(args.dump_dir, fname), "wb") as out:
                out.write(f["page_data"])
            count += 1
        print(f"\n{count} página(s) volcada(s) a {args.dump_dir}/")

    if args.json:
        json_frames = []
        for f in frames:
            jf = {k: v for k, v in f.items() if k != "page_data"}
            json_frames.append(jf)
        with open(args.json, "w") as out:
            json.dump({"header": header, "frames": json_frames, "stopped_reason": stopped_reason}, out, indent=2)
        print(f"\nVolcado JSON guardado en {args.json}")


if __name__ == "__main__":
    main()
