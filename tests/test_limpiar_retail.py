"""Tests del pipeline de limpieza. Ejecutar con:  pytest -q

Se usan datos chicos armados a mano para que cada regla se pueda verificar sin el Excel real.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from limpiar_retail import (
    ErrorDeDatos,
    Log,
    clasificar,
    limpiar,
    marcar_precios_atipicos,
    normalizar,
    quitar_superposicion_de_hojas,
)


def fila(**cambios) -> dict:
    """Una línea de venta válida en el formato original del Excel."""
    base = {"Invoice": "500001", "StockCode": "85123A", "Description": "WHITE HANGING HEART T-LIGHT HOLDER",
            "Quantity": 6, "InvoiceDate": pd.Timestamp("2010-03-01 10:00"), "Price": 2.55,
            "Customer ID": 17850.0, "Country": "United Kingdom", "hoja": "H1"}
    base.update(cambios)
    return base


def tipos(*filas) -> list[str]:
    df = normalizar(pd.DataFrame(list(filas)), Log())
    return list(clasificar(df))


# ---- clasificación: cada tipo de movimiento

@pytest.mark.parametrize("cambios, esperado", [
    ({}, "venta"),
    ({"Invoice": "C500002", "Quantity": -6}, "devolucion"),
    ({"StockCode": "POST", "Description": "POSTAGE", "Price": 18.0, "Quantity": 1}, "envio"),
    ({"StockCode": "DOT", "Price": 150.0, "Quantity": 1}, "envio"),
    ({"Invoice": "A506401", "StockCode": "B", "Price": -53594.36, "Quantity": 1, "Customer ID": np.nan}, "ajuste_contable"),
    ({"StockCode": "AMAZONFEE", "Invoice": "C500003", "Quantity": -1, "Price": 5942.57}, "ajuste_contable"),
    ({"StockCode": "M", "Description": "Manual", "Invoice": "C496350", "Quantity": 1, "Price": 373.57}, "ajuste_contable"),
    ({"Quantity": -96, "Price": 0.0, "Customer ID": np.nan, "Description": "damaged"}, "ajuste_inventario"),
    ({"Quantity": 10, "Price": 0.0, "Customer ID": np.nan, "Description": None}, "ajuste_inventario"),
    ({"Price": 0.0}, "sin_cargo"),
    ({"StockCode": "gift_0001_20", "Price": 17.02, "Quantity": 1}, "vale_regalo"),
    ({"StockCode": "TEST001", "Price": 4.5}, "prueba"),
])
def test_clasifica_cada_tipo(cambios, esperado):
    assert tipos(fila(**cambios)) == [esperado]


# ---- casos límite: valores imposibles o ilegibles

@pytest.mark.parametrize("cambios", [
    {"Quantity": 0},
    {"Quantity": "seis"},                      # texto donde va un número
    {"Price": "N/A"},
    {"InvoiceDate": "no es fecha"},
    {"Price": -2.55},                          # precio negativo fuera de un ajuste contable
    {"Quantity": -6},                          # cantidad negativa sin factura "C" y con precio: imposible
    {"Invoice": "C500002", "Quantity": 6},     # devolución con cantidad positiva
])
def test_combinaciones_imposibles_son_dato_invalido(cambios):
    assert tipos(fila(**cambios)) == ["dato_invalido"]


def test_codigo_en_minuscula_y_con_espacios_se_unifica():
    df = normalizar(pd.DataFrame([fila(StockCode="85099b"), fila(StockCode=" 85099B ")]), Log())
    assert set(df["codigo"]) == {"85099B"}


# ---- superposición entre hojas

def test_quita_superposicion_identica_entre_hojas():
    comun = [fila(Invoice="536365", InvoiceDate=pd.Timestamp("2010-12-01 08:26"))]
    h1 = [fila(Invoice="500000", hoja="H1")] + [dict(f, hoja="H1") for f in comun]
    h2 = [dict(f, hoja="H2") for f in comun] + [fila(Invoice="540000", InvoiceDate=pd.Timestamp("2011-01-05"), hoja="H2")]
    log = Log()
    df = quitar_superposicion_de_hojas(pd.DataFrame(h1 + h2), log)
    assert len(df) == 3
    assert df["Invoice"].value_counts()["536365"] == 1
    assert "idéntico" in log.pasos[0]["detalle"]


def test_sin_superposicion_no_toca_nada():
    df = pd.DataFrame([fila(hoja="H1"), fila(hoja="H2", InvoiceDate=pd.Timestamp("2011-05-01"))])
    assert len(quitar_superposicion_de_hojas(df, Log())) == 2


# ---- precios atípicos: errores de carga sí, estructura mayorista/minorista no

def ventas_de(precios: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"codigo": "X", "precio": precios})


def test_precio_minorista_doble_no_es_atipico():
    v = marcar_precios_atipicos(ventas_de([1.25] * 20 + [2.55] * 8))   # distribución bimodal legítima
    assert not v["precio_atipico"].any()


def test_precio_cien_veces_mayor_es_atipico():
    v = marcar_precios_atipicos(ventas_de([0.42] * 20 + [1157.15]))
    assert v["precio_atipico"].sum() == 1


def test_producto_con_pocas_ventas_no_se_evalua():
    v = marcar_precios_atipicos(ventas_de([1.0, 1.0, 500.0]))
    assert not v["precio_atipico"].any()


def test_distribucion_muy_sesgada_marca_solo_extremos():
    rng = np.random.default_rng(0)
    precios = list(np.round(rng.lognormal(mean=1, sigma=0.4, size=500), 2)) + [400.0, 0.01]
    v = marcar_precios_atipicos(ventas_de(precios))
    assert set(v.loc[v["precio_atipico"], "precio"]) == {400.0, 0.01}


# ---- pipeline completo

def test_pipeline_no_pierde_filas_y_separa_salidas():
    crudo = pd.DataFrame([
        fila(), fila(Invoice="500002", Quantity=2),
        fila(Invoice="C500003", Quantity=-2, InvoiceDate=pd.Timestamp("2010-03-05")),
        fila(StockCode="POST", Price=18.0, Quantity=1),
        fila(Quantity=-10, Price=0.0, **{"Customer ID": np.nan}),
    ])
    ventas, devoluciones, otros, _log = limpiar(crudo)
    assert len(ventas) + len(devoluciones) + len(otros) == len(crudo)
    assert len(ventas) == 2 and len(devoluciones) == 1
    assert devoluciones.loc[0, "factura_compra"] == "500002"   # se empareja con la compra previa
    assert set(otros["tipo"]) == {"envio", "ajuste_inventario"}


def test_anulacion_en_24h_se_distingue_de_devolucion():
    crudo = pd.DataFrame([
        fila(Invoice="581483", StockCode="23843", Quantity=80995, Price=2.08, InvoiceDate=pd.Timestamp("2011-12-09 09:15")),
        fila(Invoice="C581484", StockCode="23843", Quantity=-80995, Price=2.08, InvoiceDate=pd.Timestamp("2011-12-09 09:27")),
        fila(Invoice="500010", Quantity=12, InvoiceDate=pd.Timestamp("2010-03-01")),
        fila(Invoice="C500011", Quantity=-3, InvoiceDate=pd.Timestamp("2010-03-15")),   # devolución parcial días después
    ])
    _, devoluciones, *_ = limpiar(crudo)
    marcas = devoluciones.set_index("factura")["anulacion_24h"]
    assert marcas["C581484"] and not marcas["C500011"]


def test_anulacion_al_dia_siguiente_dentro_de_24h():
    crudo = pd.DataFrame([
        fila(Invoice="567423", Quantity=756, InvoiceDate=pd.Timestamp("2011-09-20 11:05")),
        fila(Invoice="C567527", Quantity=-756, InvoiceDate=pd.Timestamp("2011-09-21 09:16")),   # 22 h después
    ])
    _, devoluciones, *_ = limpiar(crudo)
    assert devoluciones["anulacion_24h"].all()


def test_devolucion_sin_cliente_no_se_empareja():
    crudo = pd.DataFrame([fila(), fila(Invoice="C500002", Quantity=-1, **{"Customer ID": np.nan},
                                       InvoiceDate=pd.Timestamp("2010-03-02"))])
    _, devoluciones, *_ = limpiar(crudo)
    assert devoluciones["factura_compra"].isna().all() and not devoluciones["anulacion_24h"].any()


def test_lineas_identicas_se_marcan_pero_no_se_borran():
    ventas, *_ = limpiar(pd.DataFrame([fila(), fila()]))
    assert len(ventas) == 2
    assert ventas["linea_repetida"].sum() == 1


def test_falla_con_columnas_faltantes():
    with pytest.raises(ErrorDeDatos):
        limpiar(pd.DataFrame({"Invoice": ["1"]}))
