# sqlite-wal-forensics

Parser forense para archivos `-wal` (write-ahead log) de SQLite. Lee el
archivo byte a byte siguiendo el [formato oficial](https://sqlite.org/fileformat2.html#walformat),
valida los checksums de cada frame, y te muestra qué transacciones hay
adentro, qué páginas tocó cada una, y — lo más útil para forense —
el **historial completo de versiones de cada página**, incluyendo
contenido que ya no está en la base principal (filas actualizadas o
borradas cuyo contenido anterior sigue físicamente en el WAL).

También se puede usar como [skill de Claude](https://mnt/skills) (`SKILL.md`
incluido) para que Claude analice un WAL directamente en la conversación.

## ¿Por qué el WAL sirve para forense?

Cuando SQLite corre en modo WAL, no escribe los cambios directamente en
la base principal: los va agregando como "frames" al final del archivo
`-wal`, y recién en un *checkpoint* los vuelca a la base. Un mismo
número de página puede aparecer muchas veces en el WAL — una vez por
cada vez que se modificó — y las versiones viejas **no se borran**,
simplemente quedan ahí hasta que el archivo se recicla. Eso significa
que si alguien actualiza o borra una fila, la versión anterior de esa
página (con el dato "borrado" todavía adentro) puede seguir presente
en el `-wal`, aunque ya no aparezca si consultás la base con SQL.

## Instalación

No tiene dependencias más allá de Python 3 estándar (usa `struct`,
`argparse`, `json` de la librería estándar).

```bash
git clone https://github.com/<tu-usuario>/sqlite-wal-forensics.git
cd sqlite-wal-forensics
```

## Uso

```bash
python3 scripts/parse_wal.py <archivo>-wal
```

Esto imprime un reporte con:

- **Header del WAL**: magic number, page size, salts, checksum del header.
- **Todos los frames válidos**, en orden, marcando cuáles son commits
  (fin de una transacción) y de qué página son.
- **Resumen**: cantidad de transacciones confirmadas, páginas distintas
  tocadas, tamaño final de la base según el WAL, y por qué se cortó el
  escaneo (fin de archivo, o el primer frame corrupto/inválido).
- **Historial por página**: qué páginas aparecen en más de un frame, y
  cuál de esas versiones es la vigente.

### Opciones

| Flag | Qué hace |
| --- | --- |
| `--page N` | Muestra solo el historial cronológico de la página N |
| `--dump-dir DIR` | Vuelca el contenido crudo de cada frame válido a `DIR/frame_<n>_page_<p>.bin` — para recuperar/inspeccionar versiones históricas de una página |
| `--show-corrupt` | Si el escaneo se corta por un frame inválido, muestra el detalle de por qué |
| `--json PATH` | Guarda un volcado JSON completo (header + frames) además del reporte en texto |

### Ejemplos

Reporte completo:

```bash
python3 scripts/parse_wal.py app.db-wal
```

Ver todo el historial de la página 7 (por ejemplo, si ahí vivía la fila
que se borró):

```bash
python3 scripts/parse_wal.py app.db-wal --page 7
```

Extraer todas las versiones de la página 7 a archivos individuales para
inspeccionarlas (por ejemplo con `strings` para ver texto plano, o
parseando el b-tree page manualmente):

```bash
python3 scripts/parse_wal.py app.db-wal --page 7 --dump-dir ./recuperado
strings recuperado/frame_00003_page_7.bin
```

Ver directamente el texto legible de **cada versión** de la página 7 y
un **diff** entre versiones consecutivas (para ver exactamente qué se
agregó/cambió/borró en cada transacción, sin tener que comparar los
`.bin` a mano):

```bash
python3 scripts/show_page_versions.py app.db-wal --page 7
```

| Flag | Qué hace |
| --- | --- |
| `--encoding {ascii,utf16le,all}` | cómo interpretar el texto dentro de la página (default: `ascii`) |
| `--min-len N` | largo mínimo de un string para mostrarlo (default: 4) |
| `--no-diff` | solo lista los strings de cada versión, sin el diff |

## Cómo funciona (resumen técnico)

- El header del WAL son 32 bytes: magic number, versión de formato,
  page size, número de checkpoint, dos valores de "salt" y un checksum
  del propio header.
- Cada frame es un header de 24 bytes (número de página, tamaño de la
  base tras el commit si aplica, salts copiados del header del WAL, y
  un checksum encadenado) seguido de `page_size` bytes de contenido de
  esa página.
- Un frame es válido solo si sus salts coinciden con los del header
  (si no, es un frame "fantasma" de un checkpoint anterior que todavía
  no se sobreescribió) **y** si su checksum encadenado coincide con el
  calculado sobre todo lo anterior. El checksum usa el algoritmo
  descrito en la sección 4.2 del formato oficial (pares de enteros de
  32 bits, sumas tipo Fibonacci).
- En cuanto aparece el primer frame inválido, ahí se corta el escaneo
  — es exactamente lo que hace SQLite en su propio proceso de
  recovery: todo lo que sigue después de ese punto se descarta por no
  confiable.

El detalle completo de offsets y el pseudocódigo del checksum está en
[`references/wal_format.md`](references/wal_format.md).

## Qué NO hace

- No necesita ni usa el archivo `-shm` (wal-index): es memoria
  compartida transitoria, específica de la arquitectura de la máquina
  que la escribió, y se reconstruye siempre desde el `-wal`. No aporta
  nada para forense.
- No decodifica el contenido de una página como filas/columnas SQL —
  solo entrega los bytes crudos de cada página. Para ir un paso más
  allá (parsear el b-tree y el record format) hay que aplicar el
  [formato de página de base de datos SQLite](https://sqlite.org/fileformat2.html)
  sobre los `.bin` volcados.

## Estructura del repo

```
sqlite-wal-forensics/
├── SKILL.md                  # skill de Claude (se puede instalar como .skill)
├── scripts/
│   ├── parse_wal.py          # parser principal
│   └── show_page_versions.py # muestra texto + diff entre versiones de una página
└── references/
    └── wal_format.md         # referencia detallada del formato (offsets, checksum)
```

## Licencia

MIT.
