---
name: sqlite-wal-forensics
description: Parsea y analiza archivos "-wal" de SQLite (write-ahead log) a nivel de bytes para recuperación forense de datos — extrae todos los frames, valida sus checksums, identifica qué páginas fueron modificadas, reconstruye el historial de versiones de cada página, y detecta transacciones confirmadas (commits) vs. incompletas/corruptas. Usar siempre que el usuario mencione un archivo "-wal", "write-ahead log", "wal file", "recuperar datos borrados de sqlite", "datos que quedaron en el wal pero no en la base", forense de bases sqlite, o pida explícitamente parsear/analizar el formato WAL de SQLite (incluso si no menciona la palabra "forense"). No usar para leer una base sqlite normal (.db/.sqlite) con datos ya consolidados — para eso alcanza con el módulo sqlite3 estándar.
---

# Análisis forense de archivos WAL de SQLite

Esta skill parsea el archivo `X-wal` que SQLite genera en modo
write-ahead-log, byte a byte, siguiendo el formato oficial documentado en
https://sqlite.org/fileformat2.html#walformat. El caso de uso típico es
forense/recuperación: encontrar contenido que quedó en el WAL pero que
nunca se volcó (checkpointeó) a la base principal, o que corresponde a
versiones anteriores de una página que después fue actualizada o borrada.

**Importante:** el archivo `-shm` (wal-index) NO hace falta para nada de
esto. Es memoria compartida transitoria, se reconstruye siempre desde el
`-wal`, y su formato depende de la arquitectura de la máquina que lo
escribió. Si el usuario solo tiene el `-wal` (sin `-shm`), esta skill
funciona igual.

## Flujo de trabajo

1. **Ubicar el archivo.** Suele llamarse `<nombre_de_la_base>-wal` (por
   ejemplo `app.db-wal`). Si el usuario adjuntó una base `.db`/`.sqlite`
   junto con su `-wal`, ambos son útiles: la base muestra el estado
   consolidado, el WAL muestra lo que todavía no se volcó (o lo que se
   volcó pero además dejó versiones históricas físicamente en el archivo).

2. **Correr el parser** (`scripts/parse_wal.py`) contra el archivo:

   ```bash
   python3 scripts/parse_wal.py <archivo>-wal
   ```

   Esto imprime un reporte con:
   - Header del WAL (magic, page size, salts, etc.)
   - Todos los frames válidos, en orden, marcando cuáles son commits
   - Un resumen: cantidad de transacciones, páginas tocadas, tamaño
     final de la base, y si el escaneo se cortó por corrupción o por
     llegar al final del archivo
   - Qué páginas tienen múltiples versiones en el WAL (historial)

   Si aparece corrupción o un frame inválido, agregar `--show-corrupt`
   para ver el detalle de dónde se cortó exactamente el escaneo (esto
   es normal si el proceso que escribía el WAL crasheó a mitad de una
   transacción — el resto del archivo después de ese punto no es
   confiable, tal como haría SQLite en su propio recovery).

3. **Para forense dirigido a una página específica** (por ejemplo, se
   sabe que la fila borrada vivía en la página 7):

   ```bash
   python3 scripts/parse_wal.py <archivo>-wal --page 7
   ```

   Muestra el historial cronológico completo de esa página dentro del
   WAL — no solo la versión vigente.

4. **Para extraer el contenido crudo de páginas** (recuperar bytes que
   ya no están en la base principal, o cada versión histórica de una
   página):

   ```bash
   python3 scripts/parse_wal.py <archivo>-wal --dump-dir /ruta/salida
   # o filtrando por página:
   python3 scripts/parse_wal.py <archivo>-wal --page 7 --dump-dir /ruta/salida
   ```

   Cada archivo volcado (`frame_NNNNN_page_P.bin`) es el contenido
   crudo de una página de SQLite en ese momento — un b-tree page. Se
   puede inspeccionar con `strings` para texto plano, o parsear como
   página de b-tree (ver `references/wal_format.md` y, si hace falta
   ir más allá del WAL, la skill de formato de base de datos SQLite
   completa en https://sqlite.org/fileformat2.html para decodificar
   celdas/records dentro de esa página).

5. **Para guardar todo el análisis en JSON** (por ejemplo para
   correlacionar con otra herramienta o timeline forense):

   ```bash
   python3 scripts/parse_wal.py <archivo>-wal --json salida.json
   ```

6. **Para ver el contenido legible de cada versión de una página, y el
   diff entre versiones consecutivas** (esto es lo más directo para
   responder "¿qué decía esto antes de que lo cambiaran/borraran?"):

   ```bash
   python3 scripts/show_page_versions.py <archivo>-wal --page 7
   ```

   Extrae los strings imprimibles de cada versión histórica de esa
   página (igual que haría `strings`, pero por versión) y muestra un
   diff (formato unificado, como `git diff`) entre cada versión y la
   siguiente, para que salte a la vista qué texto se agregó y qué
   texto desapareció en cada transacción. Opciones:
   - `--encoding {ascii,utf16le,all}` — por si el texto está en
     UTF-16LE en vez de ASCII/UTF-8 (default: ascii)
   - `--min-len N` — largo mínimo de string a mostrar (default: 4)
   - `--no-diff` — solo listar los strings de cada versión, sin diff

## Cosas a explicarle siempre al usuario en el reporte

- **mxFrame** = cantidad de frames válidos encontrados. Frames más allá
  de ese punto (si los hay) son basura/corrupción y se descartan, igual
  que en el recovery real de SQLite.
- Un mismo número de página puede aparecer en varios frames: solo el
  **último frame válido de esa página** (idealmente seguido de o siendo
  un commit) es el que SQLite consideraría "vigente". Los anteriores
  son historial — y es ahí donde suele aparecer contenido "borrado".
- Que el checksum de un frame sea válido no significa que esa
  transacción haya sido efectivamente aplicada a la base principal —
  eso depende de `nBackfill`, que vive en el `-shm` y no es necesario
  para extraer el contenido.
- Si el usuario quiere decodificar el contenido de una página volcada
  (columnas, valores) más allá de lo que se ve con `strings`, seguir el
  formato de b-tree page + record format de
  `references/wal_format.md` / https://sqlite.org/fileformat2.html —
  eso ya es formato de página de base de datos normal, no específico
  del WAL.

## Detalles del formato

Ver `references/wal_format.md` para la tabla completa de offsets del
header del WAL, el header de cada frame, el algoritmo de checksum
(con pseudocódigo), y cómo determinar la versión vigente de una
página. Consultarlo si hace falta escribir lógica custom más allá de
lo que ya cubre `scripts/parse_wal.py`, o para explicarle al usuario
por qué un frame se consideró inválido.
