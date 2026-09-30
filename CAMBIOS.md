# Cambios respecto a la versión anterior

## Síntoma en planta
El Hz recomendado oscilaba 2-3 Hz siguiendo la forma del nivel, aunque
el nivel estuviera dentro de banda (46 % en 40-68). El operador esperaba
override puro (flujo primario, nivel solo cuando sale de banda).

## Diagnóstico
El "feedforward por balance de masa" contenía un término
`A_M3_POR_PCT * 60 * (nivel - niv_obj) / T_RETORNO_MIN` que actuaba
**siempre**, no solo fuera de banda. Además `q_in` se calculaba desde
`dNivel/dt`, así que hasta la estimación de alimentación seguía al
nivel. Resultado: cascada permanente con nivel modulando el Hz, no
override.

## Arquitectura nueva
- **Lazo primario** (con override apagado):
  objetivo = **caudal** `FLUJO_SP` (m³/h).
  - Feedforward: `hz_ref = (FLUJO_SP - b) / m + bias`
  - P sobre error de flujo con zona muerta de `ZONA_MUERTA_FLUJO_M3H`
  - Amortiguamiento por pendiente de flujo (igual que antes)
- **Bias integrador**: aprende del **error de flujo** (no del error
  de nivel). Solo se aprende con override apagado y fuera de la
  ventana de transición de válvula. Absorbe cambios lentos de
  alimentación al cajón y de curva de bomba.
- **Override de nivel**: sin cambios funcionales. Sigue disparando
  cuando el nivel sale de la banda de la etapa, con la histéresis
  `HISTERESIS_BANDA_PCT` para no reengancharse en el borde. Cuando
  el override está activo, el lazo primario queda **silenciado**.
- **Maniobra de válvula** (`en_transicion`): SP congelado, como antes.

## Parámetros nuevos a revisar
- `FLUJO_SP = 145.0` m³/h — el caudal deseado en la línea. Cambiar al
  valor que pida la operación. El Hz teórico correspondiente se
  imprime al arrancar.
- `K_FLUJO_ERROR = 0.03` — ganancia P del lazo de flujo. Subir si
  responde lento; bajar si se detectan oscilaciones sostenidas.
- `ZONA_MUERTA_FLUJO_M3H = 3.0` — evita perseguir el ruido de la
  medición de flujo.
- `T_BIAS_MIN = 20.0` — más largo que antes para que el bias no
  intente compensar transitorios.

## Parámetros retirados
- `NIVEL_OBJ_ALIM`, `NIVEL_OBJ_COS`, `T_RETORNO_MIN`, `VENTANA_QIN_MIN`,
  `A_M3_POR_PCT` — ya no se usan (no hay término de tracking de nivel
  en el feedforward). Se pueden borrar del ConfigControl si nunca se
  reactiva el modo cascada.

## Qué esperar del comportamiento
- Con nivel dentro de banda: Hz se queda quieto salvo por
  correcciones pequeñas del flujo. Si la alimentación al cajón cambia
  lentamente, el bias se ajusta y el Hz se acomoda al nuevo punto de
  operación sin que la operación tenga que tocar nada.
- Con nivel fuera de banda: el override actúa igual que antes, con
  histéresis y rate limit adaptativo por pendiente.
- Durante los 2 min posteriores a cada cambio de válvula: SP
  congelado, buffer de flujo vaciado.

## Cómo probar / calibrar
1. Verificar el print de arranque: `Válvula 330HV4020: valor crudo = X
   -> ALIMENTACION/COSECHA`. Si sale invertido, cambiar
   `VALOR_VALVULA_ABIERTA`.
2. Correr una hora con `FLUJO_SP` al valor deseado, con override
   inactivo (nivel entre 45 y 65). El Hz debería oscilar menos de
   ±0.3 Hz alrededor del valor teórico + bias.
3. Si el flujo se estabiliza pero con offset respecto al SP, dejar que
   el bias acumule por 20-40 min: debería cerrar el error.
4. Si el bias se satura en ±3 Hz sin cerrar el error, ajustar la curva
   `CURVA_ALIM` / `CURVA_COS` con puntos reales.
