"""Pruebas de la maquina de estados termica. No requieren hardware."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gabinete_fan.config import Params, ParamStore  # noqa: E402
from gabinete_fan.controller import (  # noqa: E402
    ACTIVO, CPU, CRITICO, FALLO, MANUAL, PURGA, REPOSO, ThermalController,
)
from gabinete_fan.sensors import Reading  # noqa: E402

P = Params(t_on=42.0, t_critical=50.0, t_off=38.0, hysteresis=2.0,
           purge_minutes=3.0, min_duty=40.0, t_cpu=55.0, t_cpu_off=53.0)


def r(vhf, tx, vhf_ok=True, tx_ok=True):
    return {"vhf": Reading("Radio VHF", vhf, vhf_ok),
            "tx10m": Reading("TX 10M", tx, tx_ok)}


@pytest.fixture
def ctl():
    return ThermalController()


# -- arranque y rampa -------------------------------------------------------

def test_reposo_mientras_ambos_radios_estan_frios(ctl):
    d = ctl.update(r(30.0, 31.0), P, 5)
    assert d.state == REPOSO and d.duty == 0.0


def test_arranca_si_cualquier_radio_supera_el_umbral(ctl):
    """El requisito es 'cualquiera de los dos', no el promedio."""
    d = ctl.update(r(30.0, 42.5), P, 5)
    assert d.state == ACTIVO and d.duty >= 40.0


def test_la_rampa_va_de_min_duty_a_cien(ctl):
    assert ctl.update(r(42.0, 30.0), P, 5).duty == pytest.approx(40.0)
    ctl.reset(ACTIVO)
    assert ctl.update(r(46.0, 30.0), P, 5).duty == pytest.approx(70.0)
    ctl.reset(ACTIVO)
    assert ctl.update(r(49.9, 30.0), P, 5).duty > 97.0


# -- critico e histeresis ---------------------------------------------------

def test_critico_pone_el_ventilador_al_maximo(ctl):
    d = ctl.update(r(30.0, 50.0), P, 5)
    assert d.state == CRITICO and d.duty == 100.0


def test_critico_se_sostiene_dentro_de_la_histeresis(ctl):
    ctl.update(r(30.0, 51.0), P, 5)
    d = ctl.update(r(30.0, 48.5), P, 5)  # 50 - 2 = 48.0, aun dentro
    assert d.state == CRITICO and d.duty == 100.0


def test_sale_de_critico_al_cruzar_la_histeresis(ctl):
    ctl.update(r(30.0, 51.0), P, 5)
    d = ctl.update(r(30.0, 47.0), P, 5)  # por debajo de 48.0
    assert d.state == ACTIVO and d.duty < 100.0


# -- purga ------------------------------------------------------------------

def test_al_enfriar_entra_en_purga_al_cien_por_ciento(ctl):
    ctl.update(r(45.0, 45.0), P, 5)
    d = ctl.update(r(37.0, 37.0), P, 5)
    assert d.state == PURGA and d.duty == 100.0
    assert d.purge_remaining == pytest.approx(180.0)


def test_la_purga_dura_el_tiempo_configurado_y_luego_detiene(ctl):
    ctl.update(r(45.0, 45.0), P, 5)
    ctl.update(r(37.0, 37.0), P, 5)
    for _ in range(35):  # 35 * 5 s = 175 s, aun dentro de los 180 s
        d = ctl.update(r(35.0, 35.0), P, 5)
        assert d.state == PURGA and d.duty == 100.0
    d = ctl.update(r(35.0, 35.0), P, 5)   # 180 s
    assert d.state == REPOSO and d.duty == 0.0


def test_el_calor_durante_la_purga_la_cancela(ctl):
    ctl.update(r(45.0, 45.0), P, 5)
    ctl.update(r(37.0, 37.0), P, 5)
    d = ctl.update(r(37.0, 43.0), P, 5)
    assert d.state == ACTIVO and ctl.purge_remaining == 0.0


def test_purga_en_cero_minutos_detiene_de_inmediato(ctl):
    params = Params(**{**P.__dict__, "purge_minutes": 0.0})
    ctl.update(r(45.0, 45.0), params, 5)
    d = ctl.update(r(37.0, 37.0), params, 5)
    assert d.state == REPOSO and d.duty == 0.0


def test_una_segunda_purga_arranca_con_el_temporizador_completo(ctl):
    ctl.update(r(45.0, 45.0), P, 5)
    ctl.update(r(37.0, 37.0), P, 5)
    ctl.update(r(37.0, 43.0), P, 5)          # cancela
    d = ctl.update(r(36.0, 36.0), P, 5)      # vuelve a enfriar
    assert d.state == PURGA and d.purge_remaining == pytest.approx(180.0)


# -- fallos -----------------------------------------------------------------

def test_un_sensor_caido_fuerza_el_ventilador_al_maximo(ctl):
    """Junto a un TX activo no se puede asumir que hace frio."""
    d = ctl.update(r(None, 35.0, vhf_ok=False), P, 5)
    assert d.state == FALLO and d.duty == 100.0


def test_tras_recuperar_el_sensor_se_reevalua(ctl):
    ctl.update(r(None, 35.0, vhf_ok=False), P, 5)
    d = ctl.update(r(33.0, 34.0), P, 5)
    assert d.state == REPOSO and d.duty == 0.0


def test_recuperacion_en_caliente_vuelve_a_activo(ctl):
    ctl.update(r(None, 35.0, vhf_ok=False), P, 5)
    d = ctl.update(r(46.0, 34.0), P, 5)
    assert d.state == ACTIVO and d.duty > 40.0


def test_una_lectura_perdida_aislada_no_dispara_fallo(ctl):
    """ok=True con celsius=None es un reintento dentro de la tolerancia."""
    ctl.update(r(45.0, 45.0), P, 5)
    d = ctl.update(r(None, 45.0, vhf_ok=True), P, 5)
    assert d.state == ACTIVO


# -- modos ------------------------------------------------------------------

def test_min_duty_se_puede_cambiar_en_caliente(ctl):
    """La zona muerta de cada ventilador es distinta; el valor tiene que ser ajustable."""
    alto = Params(**{**P.__dict__, "min_duty": 60.0})
    assert ctl.update(r(42.0, 30.0), alto, 5).duty == pytest.approx(60.0)
    ctl.reset()
    assert ctl.update(r(42.0, 30.0), P, 5).duty == pytest.approx(40.0)


def test_modo_manual_ignora_las_temperaturas(ctl):
    params = Params(**{**P.__dict__, "mode": "manual", "manual_duty": 65.0})
    d = ctl.update(r(20.0, 20.0), params, 5)
    assert d.state == MANUAL and d.duty == 65.0


# -- la CPU como segundo motivo para soplar ---------------------------------

def test_cpu_caliente_arranca_el_ventilador_con_radios_frios(ctl):
    """El caso de la torre: sol sobre el gabinete, nadie transmitiendo.

    Los radios se quedan en 34 C -lejos de t_on- mientras la Pi pasa de 55 C.
    Mirando solo los radios el ventilador nunca arrancaria.
    """
    assert ctl.update(r(34.0, 34.0), P, 5, cpu=50.0).state == REPOSO
    d = ctl.update(r(34.0, 34.0), P, 5, cpu=56.0)
    assert d.state == CPU and d.duty == 100.0


def test_la_cpu_va_al_maximo_y_no_por_rampa(ctl):
    """Entre t_cpu y los 60 C del SoC no hay margen que dosificar.

    Ademas la curva medida de este ventilador tiene zona muerta: a 40% da
    1800 RPM contra un piso de 1725, o sea que los duties bajos casi no
    mueven aire y la temperatura no responde.
    """
    for cpu in (55.0, 56.2, 57.5, 60.0, 75.0):
        ctl.reset()
        assert ctl.update(r(34.0, 34.0), P, 5, cpu=cpu).duty == 100.0


def test_la_cpu_nunca_baja_el_duty_que_pidieron_los_radios(ctl):
    """Es un piso, no un control: solo puede pedir MAS aire."""
    caliente = ctl.update(r(30.0, 50.0), P, 5)                 # critico, 100%
    assert caliente.duty == 100.0
    ctl.reset()
    con_cpu = ctl.update(r(30.0, 50.0), P, 5, cpu=56.0)
    assert con_cpu.state == CRITICO and con_cpu.duty == 100.0


def test_el_paro_de_cpu_es_su_propio_umbral(ctl):
    """Arranca en t_cpu y para en t_cpu_off, no en t_cpu menos la histeresis."""
    assert ctl.update(r(34.0, 34.0), P, 5, cpu=56.0).state == CPU
    assert ctl.update(r(34.0, 34.0), P, 5, cpu=54.0).state == CPU     # arriba del paro
    assert ctl.update(r(34.0, 34.0), P, 5, cpu=52.9).state == REPOSO  # t_cpu_off = 53


def test_el_paro_de_cpu_no_depende_de_la_histeresis_de_los_radios(ctl):
    """Subir la histeresis de los radios no debe mover el paro de la CPU."""
    ancha = Params(**{**P.__dict__, "hysteresis": 8.0})
    assert ctl.update(r(34.0, 34.0), ancha, 5, cpu=56.0).state == CPU
    assert ctl.update(r(34.0, 34.0), ancha, 5, cpu=52.9).state == REPOSO


def test_un_paro_de_cpu_por_arriba_del_arranque_se_rechaza():
    with pytest.raises(ValueError, match="t_cpu_off < t_cpu"):
        Params(t_cpu=55.0, t_cpu_off=56.0).validate()


def test_home_assistant_no_puede_invertir_los_umbrales_de_cpu(tmp_path):
    store = ParamStore({"t_cpu": 55.0, "t_cpu_off": 53.0}, str(tmp_path / "s.json"))
    ok, msg = store.set("t_cpu_off", 57.0)
    assert not ok and "t_cpu_off < t_cpu" in msg
    assert store.params.t_cpu_off == 53.0
    assert store.set("t_cpu_off", 54.0)[0]        # este si cabe


def test_sin_lectura_de_cpu_no_se_inventa_nada(ctl):
    assert ctl.update(r(34.0, 34.0), P, 5, cpu=None).state == REPOSO


def test_el_modo_manual_manda_sobre_la_cpu(ctl):
    """Manual es intencion explicita del operador; no se le pasa por encima."""
    params = Params(**{**P.__dict__, "mode": "manual", "manual_duty": 20.0})
    d = ctl.update(r(34.0, 34.0), params, 5, cpu=59.0)
    assert d.state == MANUAL and d.duty == 20.0


def test_la_purga_sigue_su_curso_por_debajo_de_la_cpu(ctl):
    """El estado de los radios corre igual: la CPU solo cambia la etiqueta."""
    ctl.update(r(45.0, 45.0), P, 5)
    ctl.update(r(37.0, 37.0), P, 5)                      # entra en purga
    d = ctl.update(r(35.0, 35.0), P, 5, cpu=56.0)
    assert ctl.state == PURGA and d.duty == 100.0        # la purga ya pedia 100%


# -- validacion de parametros ----------------------------------------------

def test_se_rechaza_un_orden_de_umbrales_invalido():
    with pytest.raises(ValueError, match="t_off < t_on < t_critical"):
        Params(t_on=42.0, t_critical=40.0, t_off=38.0).validate()


def test_se_rechaza_un_umbral_fuera_de_rango():
    with pytest.raises(ValueError, match="fuera de rango"):
        Params(t_on=200.0).validate()


def test_home_assistant_no_puede_dejar_los_umbrales_inconsistentes(tmp_path):
    store = ParamStore({"t_on": 42.0, "t_critical": 50.0, "t_off": 38.0},
                       str(tmp_path / "state.json"))
    ok, msg = store.set("t_on", 55.0)          # quedaria por encima del critico
    assert not ok and "t_off < t_on < t_critical" in msg
    assert store.params.t_on == 42.0            # se conserva el valor anterior


def test_un_cambio_valido_se_persiste_en_disco(tmp_path):
    path = str(tmp_path / "state.json")
    store = ParamStore({"t_on": 42.0, "t_critical": 50.0, "t_off": 38.0}, path)
    assert store.set("purge_minutes", 5.0)[0]
    assert ParamStore({"t_on": 42.0, "t_critical": 50.0, "t_off": 38.0},
                      path).params.purge_minutes == 5.0


# -- watchdog: la ventana de expiracion contra el periodo de sondeo -----------

def test_la_ventana_del_watchdog_nunca_queda_bajo_el_sondeo(monkeypatch):
    """Con stale_after < poll el mando oscilaria entre la Pi y el respaldo."""
    import types
    falso = types.ModuleType("gpiozero")
    falso.DigitalOutputDevice = lambda *a, **k: types.SimpleNamespace(
        off=lambda: None, close=lambda: None, value=False)
    monkeypatch.setitem(sys.modules, "gpiozero", falso)

    from gabinete_fan.watchdog import Heartbeat
    hb = Heartbeat({"heartbeat_gpio": 25, "stale_after_seconds": 3}, poll_seconds=5.0)
    assert hb.stale_after >= 11.0          # 5 * 2 + 1

    hb = Heartbeat({"heartbeat_gpio": 25, "stale_after_seconds": 30}, poll_seconds=5.0)
    assert hb.stale_after == 30.0          # un valor holgado se respeta


# -- alarma de ventilador trabado --------------------------------------------

from gabinete_fan.fan import StallDetector  # noqa: E402


@pytest.fixture
def stall():
    return StallDetector(min_rpm=300.0, grace_seconds=20.0)


def test_ventilador_girando_no_levanta_alarma(stall):
    for _ in range(10):
        assert not stall.update(duty=60.0, rpm=3400.0, dt=5.0)


def test_ventilador_trabado_se_detecta_tras_la_gracia(stall):
    for _ in range(3):                       # 15 s, aun dentro de la gracia
        assert not stall.update(duty=60.0, rpm=0.0, dt=5.0)
    assert stall.update(duty=60.0, rpm=0.0, dt=5.0)   # 20 s


def test_en_reposo_no_se_vigila(stall):
    """Con duty 0 las RPM legitimas dependen del montaje, no se juzga."""
    for _ in range(10):
        assert not stall.update(duty=0.0, rpm=0.0, dt=5.0)


def test_sin_tacometro_no_se_inventa_una_falla(stall):
    for _ in range(10):
        assert not stall.update(duty=100.0, rpm=None, dt=5.0)


def test_la_alarma_se_limpia_al_volver_a_girar(stall):
    for _ in range(4):
        stall.update(duty=60.0, rpm=0.0, dt=5.0)
    assert stall.stalled
    assert not stall.update(duty=60.0, rpm=3400.0, dt=5.0)
    assert not stall.stalled


def test_un_bache_corto_no_dispara_la_alarma(stall):
    """El arranque desde parado tarda; no hay que acusar al ventilador por eso."""
    for _ in range(2):
        stall.update(duty=25.0, rpm=0.0, dt=5.0)     # 10 s arrancando
    assert not stall.update(duty=25.0, rpm=2040.0, dt=5.0)
    assert stall.quieto_desde == 0.0


# -- estadisticas de transmision de AllStarLink ------------------------------

from gabinete_fan.asl import parse_stats  # noqa: E402

SALIDA_RPT = """
Node                                             : 1001
Tail Time........................................: STANDARD
Time out timer...................................: ENABLED
Timeouts since system initialization..............: 0
Keyups today.....................................: 134
TX time today....................................: 01:01:02:638
TX time since system initialization..............: 01:01:02:638
Uptime...........................................: 02:47:16
"""


def test_se_extrae_el_tiempo_de_tx():
    segundos, keyups = parse_stats(SALIDA_RPT)
    assert segundos == pytest.approx(3662.638)     # 1 h 1 min 2.638 s
    assert keyups == 134


def test_sin_milisegundos_tambien_parsea():
    segundos, _ = parse_stats("TX time today...: 00:05:30")
    assert segundos == pytest.approx(330.0)


def test_salida_inservible_no_revienta():
    assert parse_stats("No such command 'rpt stats 9999'") == (None, None)
    assert parse_stats("") == (None, None)


def test_varios_nodos_con_etiqueta_por_radio():
    from gabinete_fan.asl import AslNodes
    nodos = AslNodes({"nodes": {"vhf": 1001, "tx10m": 1002}})
    assert set(nodos.por_radio) == {"vhf", "tx10m"}
    assert nodos.por_radio["vhf"].node == "1001"
    assert nodos.por_radio["tx10m"].node == "1002"
    # Sin muestrear todavia, el payload trae las claves en None, no falta ninguna.
    p = nodos.payload()
    assert p["tx_seconds_vhf"] is None and p["tx_keyups_tx10m"] is None
    assert set(p) == {
        "tx_seconds_vhf", "tx_duty_vhf", "tx_keyups_vhf",
        "tx_seconds_tx10m", "tx_duty_tx10m", "tx_keyups_tx10m",
    }


def test_la_forma_vieja_de_un_solo_nodo_sigue_funcionando():
    from gabinete_fan.asl import AslNodes
    assert set(AslNodes({"node": 1001}).por_radio) == {"vhf"}


def test_sin_nodos_configurados_no_estorba():
    from gabinete_fan.asl import AslNodes
    vacio = AslNodes({})
    assert not vacio and vacio.payload() == {} and vacio.resumen() == ""
