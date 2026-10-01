# ------------------------------------------------------------------
# LIBRERÍAS --------------------------------------------------------
# ------------------------------------------------------------------

import warnings
warnings.filterwarnings('ignore')

import pandas as pd
from datetime import timedelta, datetime
import datetime
import numpy as np
import scipy as sp

# ------------------------------------------------------------------
# COMUNICACIÓN PI --------------------------------------------------
# ------------------------------------------------------------------

import osisoft.pidevclub.piwebapi
from osisoft.pidevclub.piwebapi.pi_web_api_client import PIWebApiClient
from osisoft.pidevclub.piwebapi.models import PIItemsStreamValues
import requests
from osisoft.pidevclub.piwebapi.models import PITimedValue
import certifi
import urllib3
http = urllib3.PoolManager(cert_reqs='CERT_REQUIRED', ca_certs=certifi.where())

client = PIWebApiClient("https://10.100.0.41/piwebapi/", useKerberos=False,
                         username=".\\DeltaVAdmin", password="EmersonAdmin200", verifySsl=False)

def enviar_valor_PIVISION(valor, tag, date_time):
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = requests.get('https://10.100.0.41', verify=False)

    pi_point8 = client.point.get_by_path(f'\\\\10.112.0.171\\{tag}')

    pi_point_8 = pi_point8.web_id
    timestamp_to_write = date_time + timedelta(hours=5)
    pi_value = PITimedValue(timestamp=timestamp_to_write, value=valor)
    client.stream.update_value(pi_point_8, pi_value)


# ------------------------------------------------------------------
# COMUNICACIÓN MODBUS ----------------------------------------------
# ------------------------------------------------------------------

import socket
from pyModbusTCP.server import ModbusServer
from pymodbus.client import ModbusTcpClient
from pymodbus.constants import Endian
from pymodbus.payload import BinaryPayloadBuilder
import struct

server_ip_address = "10.20.1.53"   # NIC hacia DeltaV (NO usar gethostbyname: máquina multi-NIC)
server_port = 502

client_mb = ModbusTcpClient(server_ip_address, server_port)
if not client_mb.connect():
    raise RuntimeError(f"No se pudo conectar a {server_ip_address}:{server_port}")
print("Cliente Modbus conectado")


def enviar_valor_MODBUS(valor_flotante, registro_inicio):
    datos_bytes = struct.pack('f', valor_flotante)
    registro1, registro2 = struct.unpack('<HH', datos_bytes)
    r = client_mb.write_registers(registro_inicio, [registro1, registro2])
    if r.isError():
        print(f"   !! Error Modbus en registro {registro_inicio}: {r}")
    return r


# ------------------------------------------------------------------
# LECTURA DE HISTÓRICO ---------------------------------------------
# ------------------------------------------------------------------

def funcion_interpolated(start_time=None, end_time=None):

    paths = {
        "pi:\\\\10.112.0.171\\330LIC4009.PV.DCS":     "Nivel_05",
        "pi:\\\\10.112.0.171\\330LIC4009.OUT_SEL.DCS": "Hz_05",
        "pi:\\\\10.112.0.171\\330FI4025.DCS":          "Flujo_FCON",
        "pi:\\\\10.112.0.171\\330HV4020.ST.DCS":       "Cond_FCON",
    }

    data = None
    for i in paths:
        df = client.data.get_interpolated_values(
            i,
            start_time=start_time,
            end_time=end_time,
            interval="10s",
            selected_fields="items.timestamp;items.value",
        )
        df['Timestamp'] = pd.to_datetime(df['Timestamp'], format='%Y-%m-%dT%H:%M:%SZ')
        df.set_index('Timestamp', inplace=True)
        df.rename(columns={'Value': paths[i]}, inplace=True)

        if data is None:
            data = df
        else:
            data = data.merge(df, how='left', left_index=True, right_index=True)

    data.index = data.index - timedelta(hours=5)  # CORREGIR LA HORA
    data['Cond_FCON'] = data.apply(lambda x: x['Cond_FCON']['Value'], axis=1)
    data = data.apply(pd.to_numeric, errors='coerce')
    data = data.dropna(how='any')

    return data


# ------------------------------------------------------------------
# CONFIG -----------------------------------------------------------
# ------------------------------------------------------------------

from dataclasses import dataclass
from collections import deque


class ConfigControl:
    # =========================================================
    # ARQUITECTURA (30/09 – REESCRITO)
    # ---------------------------------------------------------
    # Antes: el "feedforward" arrastraba el Hz siguiendo el nivel
    # incluso con el nivel dentro de banda, porque q_obj llevaba
    # un término (nivel - niv_obj)/T_RETORNO_MIN. Eso no era
    # override: era cascada permanente. Resultado en planta: el
    # Hz oscilaba 2-3 Hz siguiendo la forma del nivel (Hz sigue
    # nivel; flujo no se estabiliza).
    #
    # Ahora, override puro:
    #   - En banda (nivel entre BANDA_LO y BANDA_HI):
    #       el lazo primario apunta a un CAUDAL objetivo (FLUJO_SP).
    #       hz_ref = (FLUJO_SP - ordenada)/pendiente + bias
    #       Bias integra el ERROR DE FLUJO (no de nivel), así
    #       absorbe cambios lentos de alimentación al cajón y
    #       de curva de bomba sin arrastrar el Hz con el nivel.
    #       El aporte por pendiente de flujo queda como
    #       amortiguamiento (reacciona a tendencias rápidas).
    #   - Fuera de banda (con histéresis): override de nivel manda.
    # =========================================================

    # --- Setpoint del lazo primario (flujo) ------------------
    # None  = MODO OVERRIDE PURO: dentro de banda el Hz no se mueve.
    #         Solo el override de nivel puede cambiarlo.
    # float = Hay setpoint de flujo. El lazo primario ajusta el Hz
    #         para mantener ese caudal cuando el nivel esta en banda.
    # Recomendado para PRIORIZAR ESTABILIDAD DEL FLUJO: None.
    FLUJO_SP = None    # None -> Hz congelado en banda

    # --- Ganancias del lazo de flujo -------------------------
    # Proporcional lento sobre el error de flujo. La ganancia efectiva
    # se aplica sobre Hz (K_flujo_err / pendiente_curva), y el aporte
    # se acota por el rate limit del paso.
    #   K_FLUJO_ERROR en m3/h de "empuje" por m3/h de error, en ~1 min.
    #   Con 0.03 y curva CURVA_ALIM (10.4 m3/h/Hz), 10 m3/h de error
    #   piden ~0.03 Hz por paso: suave y sin sobreimpulso.
    K_FLUJO_ERROR: float = 0.02           # bajado 0.03 -> 0.02: correccion mas lenta

    # --- Ganancias del lazo de nivel (override) --------------
    GANANCIA_NIVEL_ERROR: float = 0.25   # bajado 0.6 -> 0.25: menos chicoteo     # Hz por unidad de error de nivel
    GANANCIA_NIVEL_VELOC: float = 2.0    # subido 0.8 -> 2.0: mas freno al recuperar     # Hz por cada %/min de recuperación

    # Solo con override activo. 0 para desactivar el freno.

    # --- ETAPAS DEL PROCESO ---------------------------------
    # Alimentación = válvula ABIERTA (15 min). Cosecha = cerrada.
    # VERIFICAR con el print de arranque; invertir este número si
    # el diagnóstico sale al revés.
    VALOR_VALVULA_ABIERTA: int = 1

    # Bandas para el override, por etapa.
    BANDA_ALIM_LO: float = 35.0          # ampliado 40 -> 35: menos disparos falsos
    BANDA_ALIM_HI: float = 78.0          # ampliado 68 -> 78: mas margen antes de override
    BANDA_COS_LO: float  = 40.0
    BANDA_COS_HI: float  = 90.0
    # Respaldo si aún no se identifica etapa
    BANDA_LO: float = 40.0
    BANDA_HI: float = 75.0

    # Histéresis: entra en el borde, suelta al recuperar este margen.
    HISTERESIS_BANDA_PCT: float = 6.0    # subido 4 -> 6: zona muerta mas ancha, sin chatter

    # Límites duros de seguridad (informativos).
    SEG_LO: float = 30.0
    SEG_HI: float = 80.0

    # --- Curva de la bomba por etapa (flujo = m*Hz + b) -----
    # Ajustada sobre 14 h en tramos con Hz estable (R2 0.84 / 0.70).
    # Usada para el feedforward del SP y para dimensionar el bias.
    CURVA_ALIM = (10.4, -414.6)   # a 54 Hz -> 145 m3/h
    CURVA_COS  = (8.9,  -316.2)   # a 54 Hz -> 163 m3/h

    # --- Bias integrador (aprende del error de FLUJO) -------
    # Se aprende únicamente cuando el override está APAGADO,
    # para que no absorba desviaciones de nivel que después haya
    # que deshacer. En cosecha el nivel es transitorio, pero el
    # error de flujo sí es información útil, así que aprende en
    # ambas etapas siempre que el override no esté actuando.
    # Constante de aprendizaje larga para no acoplarse con el
    # transitorio de válvula.
    T_BIAS_MIN: float = 20.0      # min para responder a un error sostenido
    BIAS_MAX_HZ: float = 3.0

    # Congelar el SP durante el CIERRE de la valvula (flujo se cae de golpe).
    # En la APERTURA no se congela: se arranca con rate limit alto para
    # pre-posicionar la bomba al Hz que produce FLUJO_SP sin esperar 2-3 min.
    VENTANA_TRANSICION_SEG: int = 120     # solo cierre: freeze el SP
    VENTANA_ARRANQUE_SEG:   int = 90      # apertura: rate limit un poco mas alto durante 90 s
    RATE_ARRANQUE_HZ:      float = 0.8    # bajado 2.0 -> 0.8: arranque suave, sin brincos

    # --- Detección de tendencia de flujo (amortiguamiento) --
    # Se mantiene como en la versión anterior: solo actúa cuando
    # la pendiente supera el umbral, y el rate limit lo acota.
    UMBRAL_PENDIENTE_FLUJO: float = 25.0
    VENTANA_SEG: int = 120
    HORIZONTE_CORRECCION_MIN: float = 0.5

    # --- Guardas del lazo de flujo --------------------------
    FLUJO_MIN_VALIDO: float = 40.0
    PEND_FLUJO_MAX: float = 60.0
    PASO_SEG: int = 10

    # --- Salto por pendiente de nivel (override rápido) -----
    VENTANA_NIVEL_SEG: int = 90           # subido 30 -> 90: pendiente mucho mas estable
    PEND_NIVEL_SUAVE: float = 2.0
    PEND_NIVEL_AGRESIVA: float = 15.0
    RATE_PENDIENTE_MAX_HZ: float = 1.0    # bajado 3.0 -> 1.0: sin brincos por pendiente nivel

    # --- Anticipacion por velocidad de nivel (NUEVO) --------
    # El override antes solo miraba la POSICION del nivel: si venia
    # subiendo a 15 %/min pero todavia estaba en 65 %, esperaba a
    # cruzar 68 % para disparar, y para entonces el rate limit ya no
    # alcanzaba a devolverlo antes del rebalse.
    # Ahora la decision de override se toma con el nivel PROYECTADO
    # T_ANTICIPACION_MIN minutos adelante, cuando la velocidad supera
    # PEND_NIVEL_ALERTA. Asi:
    #   - nivel 65 %, +15 %/min  -> proyeccion 30 s = 72.5 % -> dispara YA
    #     con rate limit escalado por velocidad (paso ~3 Hz).
    #   - nivel 65 %, estable    -> proyeccion = 65 % -> lazo de flujo manda.
    #   - nivel 68 %, cayendo    -> proyeccion adelantada -> suelta antes.
    # Solo se proyecta la pendiente que supera PEND_NIVEL_ALERTA (deadband
    # de velocidad), para que el ruido de la medicion no dispare overrides.
    T_ANTICIPACION_MIN: float = 0.25  # bajado 0.5 -> 0.25: menos anticipacion agresiva
    PEND_NIVEL_ALERTA:  float = 3.0   # %/min: umbral para activar anticipacion

    # --- Restricciones del actuador -------------------------
    RATE_LIMIT_HZ: float = 0.4
    HZ_MIN: float = 52.0    # ~120 m3/h con CURVA_ALIM (cierre bomba ~47.5 Hz)
    HZ_MAX: float = 60.0

    MODO_OVERRIDE: str = "progresivo"

    # --- Zona muerta del lazo de flujo (NUEVO) --------------
    # Debajo de este error absoluto, el lazo primario NO mueve el Hz.
    # Combate el jitter de la medición de flujo (ruido ~2 m3/h) que
    # antes propagaba pequeñas correcciones cada 10 s.
    ZONA_MUERTA_FLUJO_M3H: float = 6.0    # subido 3 -> 6: ignora ruido y micro variaciones


# ------------------------------------------------------------------
# CONTROLADOR ------------------------------------------------------
# ------------------------------------------------------------------

class ControladorNivelFlujo:
    """
    Override control.
      Lazo primario  : FLUJO con setpoint FLUJO_SP (aporte por error +
                       amortiguamiento por pendiente).
      Lazo override  : NIVEL, solo cuando sale de banda (con histéresis).
    El bias integrador aprende del error de flujo cuando el override
    está apagado; el nivel NO modula el Hz mientras esté dentro de banda.
    """

    def __init__(self, config: ConfigControl, hz_inicial: float,
                 estado_override: int = 0, bias: float = 0.0):
        self.cfg = config
        self.bias = float(bias)
        self.hz_actual = float(hz_inicial)
        self.estado_override = int(estado_override)   # 0 / +1 / -1
        self.sin_descarga = False
        self.abierta = None
        n_puntos = max(2, config.VENTANA_SEG // config.PASO_SEG + 1)
        self._buf_t = deque(maxlen=n_puntos)
        self._buf_flujo = deque(maxlen=n_puntos)
        n_niv = max(2, config.VENTANA_NIVEL_SEG // config.PASO_SEG + 1)
        self._buf_tn = deque(maxlen=n_niv)
        self._buf_nivel = deque(maxlen=n_niv)

    # ---- pendientes (u/min y %/min) ----
    def _pendiente(self, buf_t, buf_y):
        if len(buf_y) < 2:
            return 0.0
        t = np.array(buf_t, dtype=float)
        t = (t - t[0]) / 60.0
        if t.max() == t.min():
            return 0.0
        y = np.array(buf_y, dtype=float)
        return float(np.polyfit(t, y, 1)[0])

    def _pendiente_flujo(self):
        return self._pendiente(self._buf_t, self._buf_flujo)

    def _pendiente_nivel(self):
        return self._pendiente(self._buf_tn, self._buf_nivel)

    # ---- banda vigente según la etapa ----
    def _banda(self):
        cfg = self.cfg
        if self.abierta is None:
            return cfg.BANDA_LO, cfg.BANDA_HI
        if self.abierta:
            return cfg.BANDA_ALIM_LO, cfg.BANDA_ALIM_HI
        return cfg.BANDA_COS_LO, cfg.BANDA_COS_HI

    # ---- pendiente/etapa/curva helpers ----
    def _curva(self):
        return self.cfg.CURVA_ALIM if self.abierta else self.cfg.CURVA_COS

    # ---- proyeccion del nivel a T_ANTICIPACION_MIN ----
    # En MODO OVERRIDE PURO (FLUJO_SP=None) no se proyecta: la filosofia es
    # "no mover Hz hasta que el nivel se salga de verdad". La anticipacion
    # haria lo contrario (predecir y mover antes), y con ventanas cortas
    # de pendiente eso disparaba overrides falsos por ruido -> Hz oscilaba
    # aunque el nivel estuviera estable.
    def _nivel_proyectado(self, nivel, pend_nivel):
        cfg = self.cfg
        if cfg.FLUJO_SP is None:
            return nivel                       # modo puro: solo el valor actual
        if abs(pend_nivel) <= cfg.PEND_NIVEL_ALERTA:
            return nivel
        exceso = pend_nivel - np.sign(pend_nivel) * cfg.PEND_NIVEL_ALERTA
        return nivel + exceso * cfg.T_ANTICIPACION_MIN

    # ---- rate limit adaptativo ----
    def _rate_limit(self, nivel, pend_nivel):
        cfg = self.cfg
        b_lo, b_hi = self._banda()
        n = self._nivel_proyectado(nivel, pend_nivel)   # NUEVO: usar proyeccion
        fuera = n > b_hi or n < b_lo
        empeora = ((n > b_hi and pend_nivel > 0) or
                   (n < b_lo and pend_nivel < 0))
        if not (fuera and empeora):
            return cfg.RATE_LIMIT_HZ
        return float(np.interp(abs(pend_nivel),
                               [cfg.PEND_NIVEL_SUAVE, cfg.PEND_NIVEL_AGRESIVA],
                               [cfg.RATE_LIMIT_HZ, cfg.RATE_PENDIENTE_MAX_HZ]))

    # ---- aporte por PENDIENTE de flujo (amortiguamiento) ----
    def _aporte_flujo_pendiente(self):
        cfg = self.cfg
        pend = self._pendiente_flujo()
        if abs(pend) > cfg.PEND_FLUJO_MAX:
            return 0.0
        exceso = abs(pend) - cfg.UMBRAL_PENDIENTE_FLUJO
        if exceso <= 0:
            return 0.0
        signo = 1.0 if pend > 0 else -1.0
        flujo_excedente = signo * exceso * cfg.HORIZONTE_CORRECCION_MIN
        m, _ = self._curva() if self.abierta is not None else cfg.CURVA_ALIM
        return -flujo_excedente / m

    # ---- aporte por ERROR de flujo (lazo primario) ----
    # Este es el corazón del cambio. Es un P sobre el error de caudal
    # respecto a FLUJO_SP, expresado en Hz. Zona muerta para no
    # perseguir el ruido de la medición.
    def _aporte_flujo_error(self, flujo):
        cfg = self.cfg
        if cfg.FLUJO_SP is None or np.isnan(flujo):
            return 0.0
        err = cfg.FLUJO_SP - flujo         # positivo -> falta caudal -> subir Hz
        if abs(err) <= cfg.ZONA_MUERTA_FLUJO_M3H:
            return 0.0
        m, _ = self._curva() if self.abierta is not None else cfg.CURVA_ALIM
        # convertir "error de m3/h" a Hz sobre la curva local
        return cfg.K_FLUJO_ERROR * err / max(m, 1e-6) * cfg.PASO_SEG   # incremental

    # ---- error de nivel con enganche / histéresis + ANTICIPACION ----
    # Usa el nivel PROYECTADO para decidir enganche y para calcular el
    # error, asi el override arranca temprano cuando el nivel viene
    # rapido, y suelta antes cuando se esta recuperando rapido.
    def _error_nivel(self, nivel, pend_nivel=0.0):
        cfg = self.cfg
        h = cfg.HISTERESIS_BANDA_PCT
        b_lo, b_hi = self._banda()
        n = self._nivel_proyectado(nivel, pend_nivel)

        if self.estado_override == 0:
            if n > b_hi:
                self.estado_override = 1
            elif n < b_lo:
                self.estado_override = -1
            else:
                return 0.0

        if self.estado_override == 1:
            err = n - (b_hi - h)
            if err <= 0.0:
                self.estado_override = 0
                return 0.0
            return err

        err = n - (b_lo + h)
        if err >= 0.0:
            self.estado_override = 0
            return 0.0
        return err

    # ---- aporte del lazo de nivel (override) ----
    # BUG arreglado 01/10: antes el freno podia revertir el signo del aporte
    # cuando el nivel se recuperaba rapido con el override todavia activo.
    # Resultado observado: override ALTO con el nivel bajando a -2 %/min
    # daba aporte = 0.25*7.6 - 2.0*2.0 = -2.1 Hz -> el Hz BAJABA aunque
    # el override pidiera lo contrario. Ahora el freno solo puede reducir
    # el aporte a CERO, no revertirlo. Mientras el override este activo,
    # el Hz nunca va en direccion opuesta a lo que pide (puede quedarse
    # quieto, pero no retroceder).
    def _aporte_nivel(self, nivel, pend_nivel=0.0):
        err = self._error_nivel(nivel, pend_nivel)
        if err == 0.0:
            return 0.0
        if self.cfg.MODO_OVERRIDE == "agresivo":
            return np.sign(err) * self.cfg.RATE_LIMIT_HZ
        freno = 0.0
        if (err > 0 and pend_nivel < 0) or (err < 0 and pend_nivel > 0):
            freno = self.cfg.GANANCIA_NIVEL_VELOC * pend_nivel
        aporte = self.cfg.GANANCIA_NIVEL_ERROR * err + freno
        # Clamp: no cruzar el cero. El freno puede frenar o detener el
        # empuje, nunca invertirlo.
        if err > 0 and aporte < 0.0:
            return 0.0
        if err < 0 and aporte > 0.0:
            return 0.0
        return aporte

    # ---- ciclo de control ----
    def paso(self, timestamp_seg: float, nivel: float, flujo: float,
             abierta=None, en_transicion: bool = False,
             en_arranque_alim: bool = False,
             verbose: bool = True) -> dict:
        cfg = self.cfg
        self.abierta = abierta

        # buffers
        self._buf_tn.append(timestamp_seg)
        self._buf_nivel.append(nivel)
        self.sin_descarga = flujo < cfg.FLUJO_MIN_VALIDO
        if self.sin_descarga:
            self._buf_t.clear()
            self._buf_flujo.clear()
        else:
            self._buf_t.append(timestamp_seg)
            self._buf_flujo.append(flujo)

        # pendientes
        pend_flujo = self._pendiente_flujo()
        pend_nivel = self._pendiente_nivel()

        # 1) OVERRIDE por nivel (prioridad)
        aporte_n = self._aporte_nivel(nivel, pend_nivel)

        # 2) LAZO PRIMARIO de FLUJO -- solo si:
        #    (a) override apagado,
        #    (b) etapa identificada,
        #    (c) hay descarga real o estamos en arranque de alim,
        #    (d) hay setpoint de flujo definido (FLUJO_SP no es None).
        # Con FLUJO_SP=None estamos en modo OVERRIDE PURO: el Hz queda
        # congelado dentro de banda, solo se mueve por override.
        aporte_ref = 0.0
        aporte_f_err = 0.0
        aporte_f_pnd = 0.0
        hz_ref = None
        if (aporte_n == 0.0 and self.abierta is not None
                and cfg.FLUJO_SP is not None
                and (not self.sin_descarga or en_arranque_alim)):
            # feedforward: llevar el Hz "de golpe" cerca del que produce FLUJO_SP
            m, b = self._curva()
            hz_ref = float(np.clip((cfg.FLUJO_SP - b) / m + self.bias,
                                    cfg.HZ_MIN, cfg.HZ_MAX))
            aporte_ref = hz_ref - self.hz_actual

            # P sobre error de flujo (incremental)
            aporte_f_err = self._aporte_flujo_error(flujo)
            # amortiguamiento por pendiente
            aporte_f_pnd = self._aporte_flujo_pendiente()

            # aprender bias: SOLO con override apagado y sin transitorio.
            # dBias apunta a que el bias absorba el sesgo persistente de la
            # curva, sin acoplarse al ruido (zona muerta ya filtró).
            if cfg.T_BIAS_MIN > 0 and not en_transicion:
                err_f = cfg.FLUJO_SP - flujo
                if abs(err_f) > cfg.ZONA_MUERTA_FLUJO_M3H:
                    paso_min = cfg.PASO_SEG / 60.0
                    d_bias = (err_f / max(m, 1e-6)) * paso_min / cfg.T_BIAS_MIN
                    self.bias = float(np.clip(self.bias + d_bias,
                                              -cfg.BIAS_MAX_HZ, cfg.BIAS_MAX_HZ))

        delta_solicitado = aporte_n + aporte_ref + aporte_f_err + aporte_f_pnd

        # rate limit: si estamos en arranque de alimentacion, permitir
        # un paso mucho mas grande para saltar directo al Hz teorico.
        rate_normal = self._rate_limit(nivel, pend_nivel)
        rate = max(rate_normal, cfg.RATE_ARRANQUE_HZ) if en_arranque_alim else rate_normal
        delta = float(np.clip(delta_solicitado, -rate, rate))

        # congelar durante maniobra de válvula
        if en_transicion:
            delta = 0.0

        hz_nuevo = float(np.clip(self.hz_actual + delta, cfg.HZ_MIN, cfg.HZ_MAX))
        delta_real = hz_nuevo - self.hz_actual
        self.hz_actual = hz_nuevo

        alarma = None
        if nivel <= cfg.SEG_LO:
            alarma = "NIVEL_CRITICO_BAJO"
        elif nivel >= cfg.SEG_HI:
            alarma = "NIVEL_CRITICO_ALTO"

        if verbose:
            aporte_f_total = aporte_f_err + aporte_f_pnd + aporte_ref
            print(
                f"Pend flujo: {pend_flujo:7.2f} u/min | "
                f"Nivel: {nivel:6.2f} ({pend_nivel:+5.1f} %/min) | "
                f"Flujo: {flujo:6.1f} (SP {cfg.FLUJO_SP if cfg.FLUJO_SP is not None else '--'}) | "
                f"Hz: {self.hz_actual:6.2f} | "
                f"A.flujo: {aporte_f_total:+.3f} (err {aporte_f_err:+.3f} / "
                f"pnd {aporte_f_pnd:+.3f} / ref {aporte_ref:+.3f}) | "
                f"A.nivel: {aporte_n:+.3f} | "
                f"bias {self.bias:+.2f} | rate {rate:.2f}"
                + ("  [TRANSICION-CONGELADO]" if en_transicion else "")
                + ("  [ARRANQUE-ALIM]" if en_arranque_alim else "")
                + ("" if self.abierta is None else
                   ("  [ALIMENTACION]" if self.abierta else "  [COSECHA]"))
                + ("  [SIN DESCARGA]" if self.sin_descarga else "")
                + ("  [OVR ALTO]" if self.estado_override == 1 else
                   "  [OVR BAJO]" if self.estado_override == -1 else "")
            )

        return {
            "hz": self.hz_actual,
            "delta_hz": delta_real,
            "pendiente_flujo": pend_flujo,
            "aporte_flujo_error": aporte_f_err,
            "aporte_flujo_pendiente": aporte_f_pnd,
            "aporte_ref": aporte_ref,
            "aporte_flujo": aporte_f_err + aporte_f_pnd + aporte_ref,
            "aporte_nivel": aporte_n,
            "delta_solicitado": delta_solicitado,
            "saturado_rate": abs(delta_solicitado) > rate,
            "override_activo": aporte_n != 0.0,
            "hz_ref": hz_ref,
            "en_transicion": en_transicion,
            "alarma": alarma,
        }


# ------------------------------------------------------------------
# ORQUESTACIÓN -----------------------------------------------------
# ------------------------------------------------------------------

from datetime import datetime

now = datetime.now()
now = datetime(now.year, now.month, now.day, now.hour, now.minute, now.second)

# 6 min de historia: la ventana de pendiente de flujo necesita 2 min y
# se aprovechan las últimas muestras válidas para el arranque del bias.
start = now - timedelta(minutes=6)

df = funcion_interpolated(start_time=start, end_time=now)

cfg = ConfigControl()

# recuperar override + bias del ciclo anterior
estado_override_prev = 0
_bias = 0.0
try:
    _prev = pd.read_csv('Last_feed_cycle_parameter.csv')
    if 'Override' in _prev.columns:
        estado_override_prev = int(_prev['Override'].iloc[-1])
    if 'Bias' in _prev.columns:
        _bias = float(_prev['Bias'].iloc[-1])
except Exception:
    pass

ctrl = ControladorNivelFlujo(cfg, hz_inicial=df["Hz_05"].iloc[0],
                              estado_override=estado_override_prev, bias=_bias)

# etapa vigente + ventanas de transicion.
# CIERRE  (alim -> cosecha): flujo se cae -> CONGELAR SP 2 min.
# APERTURA (cosecha -> alim): rate limit alto 60 s para saltar al Hz teorico
# sin esperar la rampa lenta (arreglo del bug "flujo a 0 los primeros 3 min").
val = pd.to_numeric(df["Cond_FCON"], errors="coerce")
abierta = val.eq(cfg.VALOR_VALVULA_ABIERTA)
abierta_prev = abierta.shift(fill_value=False)
# .copy() para que el array sea escribible (en pandas nuevo to_numpy() devuelve vista solo-lectura)
just_closed = ((~abierta) & abierta_prev).to_numpy().copy()
just_opened = (abierta & (~abierta_prev)).to_numpy().copy()
just_closed[0] = False
just_opened[0] = False

seg_desde_cierre   = np.full(len(df), 1e9)
seg_desde_apertura = np.full(len(df), 1e9)
idx_c = np.where(just_closed)[0]
idx_o = np.where(just_opened)[0]
if len(idx_c):
    u = idx_c[-1]
    seg_desde_cierre[u:]   = np.arange(len(df) - u) * cfg.PASO_SEG
if len(idx_o):
    u = idx_o[-1]
    seg_desde_apertura[u:] = np.arange(len(df) - u) * cfg.PASO_SEG

en_transicion    = seg_desde_cierre   < cfg.VENTANA_TRANSICION_SEG   # solo cierre
en_arranque_alim = seg_desde_apertura < cfg.VENTANA_ARRANQUE_SEG     # solo apertura

print(f"\n => Set point de frecuencia enviado a las {now}")
print(f"    Válvula 330HV4020: valor crudo = {val.iloc[-1]} -> "
      f"{'ALIMENTACION (abierta)' if abierta.iloc[-1] else 'COSECHA (cerrada)'}"
      f"   [invertir VALOR_VALVULA_ABIERTA si sale al revés]")
if len(idx_c) or len(idx_o):
    ult_evt = min(seg_desde_cierre[-1], seg_desde_apertura[-1])
    tipo = "APERTURA (arranque de alim)" if seg_desde_apertura[-1] < seg_desde_cierre[-1] else "CIERRE (a cosecha)"
    print(f"    Ultimo cambio de etapa hace {ult_evt:.0f} s -> {tipo}")
if en_arranque_alim[-1]:
    print(f"    >> EN ARRANQUE DE ALIMENTACION: rate limit = {cfg.RATE_ARRANQUE_HZ} Hz/paso")
if cfg.FLUJO_SP is None:
    print("    MODO OVERRIDE PURO: Hz congelado dentro de banda")
    print("    -> Hz solo se mueve por override de nivel (fuera de banda)")
else:
    print(f"    Objetivo del lazo primario: FLUJO_SP = {cfg.FLUJO_SP:.0f} m3/h "
          f"(Hz teorico ~ {(cfg.FLUJO_SP - cfg.CURVA_ALIM[1]) / cfg.CURVA_ALIM[0]:.1f} en alim)")

registros = []
for i, (ts, fila) in enumerate(df.iterrows()):
    # Sincronizar Hz interno con OUT_SEL medido de la fila (evita
    # que la integración quede saturada al recorrer la historia).
    ctrl.hz_actual = float(fila["Hz_05"])
    out = ctrl.paso(
        timestamp_seg=ts.timestamp(),
        nivel=fila["Nivel_05"],
        flujo=fila["Flujo_FCON"],
        abierta=bool(abierta.iloc[i]),
        en_transicion=bool(en_transicion[i]),
        en_arranque_alim=bool(en_arranque_alim[i]),
        verbose=(i >= len(df) - 4),
    )
    out["timestamp"] = ts
    registros.append(out)

log = pd.DataFrame(registros).set_index("timestamp")

ultima = log.iloc[-1]
sp_hz = df["Hz_05"].iloc[-1] + ultima['delta_hz']
sp_hz = round(sp_hz, 2)

print("   -> Pasos con override activo:", int(log["override_activo"].sum()))
print("   -> Pasos congelados por transición de válvula:", int(log["en_transicion"].sum()))
print("   -> Pasos saturados por rate limit:", int(log["saturado_rate"].sum()))
print("   -> Alarmas de seguridad:", log["alarma"].notna().sum())
print(f"   -> Autoajuste acumulado (bias): {ctrl.bias:+.2f} Hz")
print("   -> SP de Frecuencia calculada:", sp_hz, "Hz")

enviar_valor_PIVISION(sp_hz, '330LIC4009.OUT.ML', df.index[-1])
enviar_valor_MODBUS(sp_hz, 0)

# Indicadores de estado del lazo de flujo (usar el aporte agregado)
if ultima['aporte_flujo'] < 0:
    enviar_valor_PIVISION(-1, '330FI4025.STATE_RECOM.ML', df.index[-1])
elif ultima['aporte_flujo'] == 0:
    enviar_valor_PIVISION(0, '330FI4025.STATE_RECOM.ML', df.index[-1])
else:
    enviar_valor_PIVISION(1, '330FI4025.STATE_RECOM.ML', df.index[-1])

if ultima['aporte_nivel'] < 0:
    enviar_valor_PIVISION(-1, '330LIC4009.STATE_RECOM.ML', df.index[-1])
elif ultima['aporte_nivel'] == 0:
    enviar_valor_PIVISION(0, '330LIC4009.STATE_RECOM.ML', df.index[-1])
else:
    enviar_valor_PIVISION(1, '330LIC4009.STATE_RECOM.ML', df.index[-1])

pd.DataFrame({'Timestamp': [df.index[-1]],
              'Hz_05':     [sp_hz],
              'Override':  [ctrl.estado_override],
              'Bias':      [round(ctrl.bias, 3)]}).to_csv(
    'Last_feed_cycle_parameter.csv', index=False)
