# Formato de los archivos WAL de SQLite — referencia de bytes

Fuentes oficiales:
- https://sqlite.org/fileformat2.html#walformat (formato del `-wal`, la parte que importa para forense/recuperación)
- https://sqlite.org/walformat.html (formato del `-shm` / wal-index — memoria compartida transitoria, no persiste tras un crash y se puede reconstruir siempre a partir del `-wal`)

## 1. Los tres archivos de una base en modo WAL

| Archivo   | Nombre                  | Contiene                                                                 |
| --------- | ------------------------ | ------------------------------------------------------------------------ |
| `X`       | base principal           | páginas confirmadas hasta el último checkpoint                          |
| `X-wal`   | write-ahead log          | frames (páginas modificadas) de transacciones aún no volcadas a `X`     |
| `X-shm`   | wal-index (shared memory) | caché transitoria para ubicar frames rápido; **no hace falta para recovery** |

Si el último cliente cerró limpio, `X-wal` y `X-shm` normalmente no existen (se hizo checkpoint y se borraron). Si quedan en disco, es porque hubo un cierre no limpio, un lector activo, o `SQLITE_FCNTL_PERSIST_WAL` — exactamente los casos donde vale la pena mirarlos.

## 2. Header del WAL (32 bytes, big-endian)

| Offset | Tamaño | Campo                | Notas                                                          |
| ------ | ------ | --------------------- | ---------------------------------------------------------------- |
| 0      | 4      | Magic number          | `0x377f0682` (checksums little-endian) o `0x377f0683` (big-endian) |
| 4      | 4      | Versión de formato    | Siempre `3007000`                                               |
| 8      | 4      | Page size              | Ej. 4096. El valor `1` representa 65536.                        |
| 12     | 4      | Nº de secuencia de checkpoint |                                                            |
| 16     | 4      | Salt-1                | se incrementa en cada reset del WAL                             |
| 20     | 4      | Salt-2                | aleatorio en cada reset del WAL                                 |
| 24     | 4      | Checksum-1 del header  | checksum sobre los bytes 0..23                                  |
| 28     | 4      | Checksum-2 del header  |                                                                    |

El byte 0 del magic number decide el orden de bytes usado **solo para el algoritmo de checksum** (ver más abajo); el resto de los campos del header y del frame-header siempre son big-endian.

## 3. Frame (24 bytes de header + `page_size` bytes de datos), repetido N veces

| Offset relativo | Tamaño | Campo               | Notas                                                                 |
| ---------------- | ------ | --------------------- | ------------------------------------------------------------------------ |
| 0                 | 4      | Número de página      |                                                                            |
| 4                 | 4      | Tamaño de la BD tras el commit | **Distinto de 0 solo si este frame es un commit** (fin de transacción). Si es 0, es un frame intermedio de una transacción más larga. |
| 8                 | 4      | Salt-1                | copiado del header del WAL vigente al momento de escribir el frame       |
| 12                | 4      | Salt-2                | ídem                                                                      |
| 16                | 4      | Checksum-1             | checksum encadenado acumulado hasta este frame inclusive                 |
| 20                | 4      | Checksum-2             |                                                                            |
| 24                | page_size | Contenido de la página |                                                                         |

Un frame es válido si y solo si:
1. Sus salt-1/salt-2 coinciden con los del header del WAL (si no coinciden, es un frame "fantasma" de un checkpoint viejo que todavía no fue sobreescrito físicamente).
2. Su checksum coincide con el checksum acumulado calculado sobre: los primeros 24 bytes del header del WAL, seguidos de los primeros 8 bytes de cada frame-header (número de página + tamaño post-commit, **sin** incluir los propios campos de salt/checksum) y el contenido de cada página, hasta este frame inclusive.

En cuanto aparece el primer frame inválido (checksum o salt no coinciden), ahí termina lo recuperable: SQLite en recovery real también corta ahí. Todo lo que sigue en el archivo se descarta.

## 4. Algoritmo de checksum (sección 4.2 del file format)

Opera sobre pares de enteros de 32 bits (el input debe tener largo múltiplo de 8 bytes). El orden de bytes usado para interpretar esos enteros es big-endian si el magic number del header es `0x377f0683`, o little-endian si es `0x377f0682` — el resultado del checksum en sí siempre se guarda big-endian en el frame.

```
s0 = s1 = 0
for i in range(0, N, 2):
    s0 += x[i]   + s1
    s1 += x[i+1] + s0
# resultado en (s0, s1), cada uno truncado a 32 bits
```

Se arranca con `(s0, s1) = (checksum1, checksum2)` del header del WAL, y se va encadenando frame a frame.

## 5. Cómo leer una página "vigente"

Un mismo número de página puede aparecer en **muchos frames** a lo largo del WAL (cada escritura sucesiva a esa página agrega un frame nuevo al final). El algoritmo de lectura de SQLite (sección 4.5) es: para leer la página P, buscar hacia atrás desde `mxFrame` el **último** frame válido con ese número de página que sea un commit o esté seguido de un commit. Ese es el estado vigente.

Todos los frames anteriores para esa misma página son historial — contenido que existió en algún momento y que, si no fue sobreescrito, sigue físicamente en el archivo. Esto es lo que hace al WAL interesante para forense: puede contener versiones previas de filas que ya fueron actualizadas o borradas en la base principal.

## 6. mxFrame / nBackfill (conceptos, viven en el `-shm`, no en el `-wal`)

- **mxFrame**: número del último frame válido y confirmado del WAL (lo calculamos nosotros mismos al escanear, no hace falta leer el `-shm`).
- **nBackfill**: cuántos frames ya fueron volcados a la base principal por checkpoints previos. Si `nBackfill == mxFrame`, todo el contenido del WAL ya está reflejado en la base y, en teoría, no queda nada "extra" para recuperar del WAL — aunque el contenido físico puede seguir ahí hasta que se sobreescriba.

Esto vive en el `-shm`, que es memoria compartida transitoria (formato dependiente de la arquitectura, no portable, no confiable como evidencia persistente). Para forense, el `-wal` es la fuente de verdad; el `-shm` no aporta nada que no se pueda recalcular escaneando el `-wal`.
