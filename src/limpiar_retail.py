"""
Pipeline de limpieza para Online Retail II (UCI): transacciones REALES de un
e-commerce mayorista del Reino Unido, dic 2009 – dic 2011.

En vez de borrar lo que "parece raro", el pipeline CLASIFICA cada fila en un tipo
de movimiento (venta, devolución, envío, ajuste contable, ajuste de inventario...),
porque cada uno responde una pregunta de negocio distinta. Nada se descarta en
silencio: el log y la columna `tipo` explican el destino de cada fila.

Uso desde la terminal:
    python src/limpiar_retail.py data/raw/online_retail_II.xlsx data/processed

Genera en la carpeta de salida:
    ventas.csv.gz          líneas de venta de productos (con marcas de calidad)
    devoluciones.csv.gz    devoluciones de productos (facturas "C")
    otros_movimientos.csv.gz  envíos, ajustes, pruebas, vales y datos inválidos
    log_limpieza.csv       qué se hizo, con cuántas filas y por qué
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger("limpiar_retail")

COLUMNAS_ORIGEN = {
    "Invoice": "factura", "StockCode": "codigo", "Description": "descripcion", "Quantity": "cantidad",
    "InvoiceDate": "fecha", "Price": "precio", "Customer ID": "cliente_id", "Country": "pais",
}

# Códigos que no son productos. Se identificaron explorando el dataset (ver notebook, sección 2).
CODIGOS_ENVIO = {"POST", "DOT", "C2"}
CODIGOS_AJUSTE_CONTABLE = {"B", "M", "D", "S", "BANK CHARGES", "AMAZONFEE", "CRUK", "ADJUST", "ADJUST2"}
PREFIJO_PRUEBA = "TEST"
PREFIJO_VALE = "GIFT_"

FACTOR_PRECIO_ATIPICO = 10   # un precio 10 veces mayor o menor que el habitual del producto
MIN_VENTAS_REFERENCIA = 5    # ventas mínimas de un producto para estimar su precio habitual

TIPOS = ("venta", "devolucion", "envio", "ajuste_contable", "ajuste_inventario",
         "sin_cargo", "vale_regalo", "prueba", "dato_invalido")


class ErrorDeDatos(ValueError):
    """La entrada no tiene la estructura esperada."""


@dataclass
class Log:
    pasos: list[dict] = field(default_factory=list)

    def registrar(self, paso: str, filas: int, detalle: str) -> None:
        self.pasos.append({"paso": paso, "filas": filas, "detalle": detalle})
        logger.info("%s: %s filas · %s", paso, f"{filas:,}".replace(",", "."), detalle)

    def a_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(self.pasos)


# ------------------------------------------------------------------ lectura

def leer_excel(ruta: Path) -> pd.DataFrame:
    """Lee todas las hojas del Excel y agrega la columna `hoja` con el origen de cada fila."""
    if not ruta.exists():
        raise FileNotFoundError(f"No existe el archivo: {ruta}")
    hojas = pd.read_excel(ruta, sheet_name=None, dtype={"Invoice": str, "StockCode": str})
    return pd.concat([h.assign(hoja=nombre) for nombre, h in hojas.items()], ignore_index=True)


def validar_columnas(df: pd.DataFrame) -> None:
    faltantes = [c for c in COLUMNAS_ORIGEN if c not in df.columns]
    if faltantes:
        raise ErrorDeDatos(f"Faltan columnas: {faltantes}")
    if df.empty:
        raise ErrorDeDatos("El archivo no tiene filas.")


# ------------------------------------------------------------------ pasos

def quitar_superposicion_de_hojas(df: pd.DataFrame, log: Log) -> pd.DataFrame:
    """Si una hoja repite el período inicial de la siguiente, elimina la repetición.

    En Online Retail II, la hoja 2009-2010 y la 2010-2011 contienen las mismas
    transacciones del 1 al 9 de diciembre de 2010. Concatenarlas sin revisar
    cuenta esas ventas dos veces. Solo se elimina si la ventana es idéntica en
    ambas hojas; si difiere, se avisa y se conserva la hoja más reciente.
    """
    if "hoja" not in df.columns or df["hoja"].nunique() < 2:
        return df
    hojas = list(dict.fromkeys(df["hoja"]))  # orden original
    columnas = [c for c in df.columns if c != "hoja"]
    for anterior, siguiente in pairwise(hojas):
        inicio = df.loc[df["hoja"] == siguiente, "InvoiceDate"].min()
        fin = df.loc[df["hoja"] == anterior, "InvoiceDate"].max()
        if fin < inicio:
            continue
        en_anterior = (df["hoja"] == anterior) & (df["InvoiceDate"] >= inicio)
        en_siguiente = (df["hoja"] == siguiente) & (df["InvoiceDate"] <= fin)
        a = df.loc[en_anterior, columnas].sort_values(columnas).reset_index(drop=True)
        b = df.loc[en_siguiente, columnas].sort_values(columnas).reset_index(drop=True)
        if not a.equals(b):
            logger.warning("La superposición entre '%s' y '%s' no es idéntica; se conserva '%s'", anterior, siguiente, siguiente)
        df = df[~en_anterior]
        log.registrar("Quitar superposición de hojas", int(en_anterior.sum()),
                      f"'{anterior}' repetía {inicio:%d/%m/%Y}–{fin:%d/%m/%Y} de '{siguiente}'"
                      + (" (idéntico)" if a.equals(b) else " (NO idéntico: se conservó la hoja más reciente)"))
    return df.reset_index(drop=True)


def normalizar(df: pd.DataFrame, log: Log) -> pd.DataFrame:
    """Nombres de columna en español, tipos correctos y textos sin espacios ni variantes de mayúsculas."""
    df = df.rename(columns=COLUMNAS_ORIGEN).drop(columns="hoja", errors="ignore").copy()
    df["factura"] = df["factura"].astype(str).str.strip().str.upper()
    codigo_original = df["codigo"].astype(str)
    # "85099b" y "85099B" son el mismo producto (misma descripción): se unifica en mayúsculas
    df["codigo"] = codigo_original.str.strip().str.upper()
    df["descripcion"] = df["descripcion"].astype("string").str.strip().str.replace(r"\s+", " ", regex=True)
    df["cantidad"] = pd.to_numeric(df["cantidad"], errors="coerce")
    df["precio"] = pd.to_numeric(df["precio"], errors="coerce")
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")
    df["cliente_id"] = pd.to_numeric(df["cliente_id"], errors="coerce").astype("Int64")
    df["pais"] = df["pais"].astype(str).str.strip()
    log.registrar("Normalizar códigos", int((codigo_original != df["codigo"]).sum()),
                  "códigos con minúsculas o espacios unificados (ej. '72349b' → '72349B')")
    return df


def clasificar(df: pd.DataFrame) -> pd.Series:
    """Asigna un tipo de movimiento a cada fila. El orden de las reglas importa: gana la primera que aplica."""
    codigo, factura = df["codigo"], df["factura"]
    sin_cliente = df["cliente_id"].isna()
    reglas = [
        ("dato_invalido", df["fecha"].isna() | df["cantidad"].isna() | df["precio"].isna() | (df["cantidad"] == 0)),
        ("prueba", codigo.str.startswith(PREFIJO_PRUEBA)),
        ("ajuste_contable", factura.str.startswith("A") | codigo.isin(CODIGOS_AJUSTE_CONTABLE)),
        ("envio", codigo.isin(CODIGOS_ENVIO)),
        # Precio 0 y sin cliente: movimientos internos de stock ("damaged", "lost", "found"...)
        ("ajuste_inventario", (df["precio"] == 0) & sin_cliente),
        ("sin_cargo", (df["precio"] == 0) & ~sin_cliente),
        ("vale_regalo", codigo.str.startswith(PREFIJO_VALE)),
        ("devolucion", factura.str.startswith("C") & (df["cantidad"] < 0) & (df["precio"] > 0)),
        ("venta", ~factura.str.startswith("C") & (df["cantidad"] > 0) & (df["precio"] > 0)),
    ]
    tipo = pd.Series("dato_invalido", index=df.index)  # lo que ninguna regla explica es inválido
    asignado = pd.Series(False, index=df.index)
    for nombre, condicion in reglas:
        aplicar = condicion.fillna(False) & ~asignado
        tipo[aplicar] = nombre
        asignado |= aplicar
    return tipo


def descripcion_canonica(df: pd.DataFrame) -> pd.Series:
    """Descripción más frecuente de cada código en las ventas.

    Un mismo código aparece a veces con textos de control ("check", "damaged") que
    vienen de ajustes de inventario; la descripción canónica los evita.
    """
    ventas = df[(df["tipo"] == "venta") & df["descripcion"].notna()]
    moda = ventas.groupby("codigo")["descripcion"].agg(lambda s: s.value_counts().index[0])
    return df["codigo"].map(moda).fillna(df["descripcion"])


def marcar_precios_atipicos(ventas: pd.DataFrame) -> pd.DataFrame:
    """Marca ventas cuyo precio está 10 veces por encima o por debajo del habitual del producto.

    Por qué no un z-score: en este negocio conviven precio mayorista y minorista
    (el minorista suele ser ~2 veces el mayorista) y la mayoría de los productos
    tienen un precio casi constante (MAD = 0). Un z-score robusto marca como atípicas
    miles de ventas minoristas legítimas. Una regla de razón contra la mediana,
    en escala logarítmica, separa los errores de carga de la estructura de precios.
    Se marca, no se borra: el análisis decide qué hacer y mide el impacto.
    """
    ventas = ventas.copy()
    mediana = ventas.groupby("codigo")["precio"].transform("median")
    n = ventas.groupby("codigo")["precio"].transform("size")
    razon = ventas["precio"] / mediana
    ventas["precio_referencia"] = mediana.round(2)
    ventas["precio_atipico"] = (n >= MIN_VENTAS_REFERENCIA) & (
        (razon > FACTOR_PRECIO_ATIPICO) | (razon < 1 / FACTOR_PRECIO_ATIPICO))
    return ventas


def marcar_lineas_repetidas(ventas: pd.DataFrame) -> pd.DataFrame:
    """Marca líneas idénticas dentro de una misma factura (mismo producto, cantidad, precio y hora).

    Sin un ID de línea no se puede saber si son un error de exportación o el mismo
    producto cargado dos veces en el pedido. Se conservan y se marcan; el notebook
    mide cuánto cambian los resultados si se eliminan.
    """
    ventas = ventas.copy()
    clave = ["factura", "codigo", "cantidad", "precio", "fecha", "cliente_id"]
    ventas["linea_repetida"] = ventas.duplicated(clave, keep="first")
    return ventas


def emparejar_devoluciones(devoluciones: pd.DataFrame, ventas: pd.DataFrame) -> pd.DataFrame:
    """Para cada devolución, busca la compra más reciente del mismo cliente y producto antes de esa fecha.

    Si se devuelve exactamente la cantidad comprada dentro de las 24 horas, se marca como
    `anulacion_24h`: es un pedido corregido o cancelado, no un producto que
    el cliente probó y devolvió. Mezclar ambos casos distorsiona la tasa de devolución
    (ej. un pedido de 80.995 unidades cargado por error y anulado 12 minutos después).
    """
    dev = devoluciones.copy()
    dev["_orden"] = np.arange(len(dev))
    con_cliente = dev[dev["cliente_id"].notna()].sort_values("fecha")
    compras = (ventas[ventas["cliente_id"].notna()][["fecha", "cliente_id", "codigo", "factura", "cantidad"]]
               .rename(columns={"factura": "factura_compra", "fecha": "fecha_compra", "cantidad": "cantidad_compra"})
               .sort_values("fecha_compra"))
    m = pd.merge_asof(con_cliente, compras, left_on="fecha", right_on="fecha_compra",
                      by=["cliente_id", "codigo"], direction="backward")
    dev = dev.merge(m[["_orden", "factura_compra", "fecha_compra", "cantidad_compra"]], on="_orden", how="left")
    dev = dev.sort_values("_orden").drop(columns="_orden").set_index(devoluciones.index)
    dev["dias_desde_compra"] = (dev["fecha"] - dev["fecha_compra"]).dt.days
    rapida = (dev["fecha"] - dev["fecha_compra"]) <= pd.Timedelta(hours=24)
    dev["anulacion_24h"] = (rapida & (-dev["cantidad"] == dev["cantidad_compra"])).fillna(False).astype(bool)
    return dev


def validar(ventas: pd.DataFrame, devoluciones: pd.DataFrame, total_filas: int, filas_salida: int) -> None:
    problemas = []
    if filas_salida != total_filas:
        problemas.append(f"se perdieron filas: entraron {total_filas} y salieron {filas_salida}")
    if not ((ventas["cantidad"] > 0) & (ventas["precio"] > 0)).all():
        problemas.append("hay ventas con cantidad o precio <= 0")
    if not (devoluciones["cantidad"] < 0).all():
        problemas.append("hay devoluciones con cantidad positiva")
    if ventas["fecha"].isna().any():
        problemas.append("hay ventas sin fecha")
    if problemas:
        raise ErrorDeDatos("Validación final fallida: " + "; ".join(problemas))


# ------------------------------------------------------------------ pipeline

def limpiar(crudo: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Devuelve (ventas, devoluciones, otros_movimientos, log)."""
    validar_columnas(crudo)
    log = Log()
    log.registrar("Filas leídas", len(crudo), f"{crudo['hoja'].nunique() if 'hoja' in crudo else 1} hoja(s)")
    df = quitar_superposicion_de_hojas(crudo, log)
    total = len(df)
    df = normalizar(df, log)
    df["tipo"] = clasificar(df)
    for tipo, n in df["tipo"].value_counts().reindex(TIPOS, fill_value=0).items():
        log.registrar(f"Clasificar: {tipo}", int(n), DESCRIPCION_TIPOS[tipo])
    df["descripcion"] = descripcion_canonica(df)
    df["importe"] = (df["cantidad"] * df["precio"]).round(2)
    df["tiene_cliente"] = df["cliente_id"].notna()

    ventas = marcar_lineas_repetidas(marcar_precios_atipicos(df[df["tipo"] == "venta"]))
    log.registrar("Marcar precios atípicos", int(ventas["precio_atipico"].sum()),
                  f"precio más de {FACTOR_PRECIO_ATIPICO} veces lejos del habitual del producto (se marcan, no se borran)")
    log.registrar("Marcar líneas repetidas", int(ventas["linea_repetida"].sum()),
                  "líneas idénticas en la misma factura (se marcan, no se borran)")
    devoluciones = emparejar_devoluciones(df[df["tipo"] == "devolucion"], ventas)
    log.registrar("Emparejar devoluciones", int(devoluciones["factura_compra"].notna().sum()),
                  "devoluciones con una compra previa del mismo cliente y producto")
    log.registrar("Marcar anulaciones en 24 h", int(devoluciones["anulacion_24h"].sum()),
                  "toda la cantidad comprada, dentro de las 24 h: pedido corregido, no devolución de producto")
    otros = df[~df["tipo"].isin(["venta", "devolucion"])]

    validar(ventas, devoluciones, total, len(ventas) + len(devoluciones) + len(otros))
    def orden(d: pd.DataFrame) -> pd.DataFrame:
        return d.sort_values(["fecha", "factura"]).reset_index(drop=True)

    return orden(ventas), orden(devoluciones), orden(otros), log.a_dataframe()


DESCRIPCION_TIPOS = {
    "venta": "venta de productos (cantidad y precio positivos)",
    "devolucion": "factura 'C' de un producto: devolución o cancelación",
    "envio": "cargos de envío (POST, DOT, C2): ingreso, pero no venta de producto",
    "ajuste_contable": "deudas incobrables, comisiones, cargos bancarios, descuentos y ajustes manuales",
    "ajuste_inventario": "precio 0 y sin cliente: mercadería dañada, perdida o encontrada",
    "sin_cargo": "precio 0 con cliente: entregas sin cargo",
    "vale_regalo": "venta de vales de regalo (no es un producto físico)",
    "prueba": "productos de prueba del sistema (TEST001, TEST002)",
    "dato_invalido": "fecha, cantidad o precio ilegibles, cantidad 0 o combinación imposible",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Limpia Online Retail II y separa ventas, devoluciones y otros movimientos.")
    parser.add_argument("entrada", type=Path, help="online_retail_II.xlsx")
    parser.add_argument("salida", type=Path, help="carpeta donde guardar los resultados")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    try:
        ventas, devoluciones, otros, log = limpiar(leer_excel(args.entrada))
    except (FileNotFoundError, ErrorDeDatos) as e:
        logger.error(str(e))
        return 1
    args.salida.mkdir(parents=True, exist_ok=True)
    ventas.to_csv(args.salida / "ventas.csv.gz", index=False)
    devoluciones.to_csv(args.salida / "devoluciones.csv.gz", index=False)
    otros.to_csv(args.salida / "otros_movimientos.csv.gz", index=False)
    log.to_csv(args.salida / "log_limpieza.csv", index=False)
    logger.info("Listo: %d ventas, %d devoluciones, %d otros movimientos en %s",
                len(ventas), len(devoluciones), len(otros), args.salida)
    return 0


if __name__ == "__main__":
    sys.exit(main())
