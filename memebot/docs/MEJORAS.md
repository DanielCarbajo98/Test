# Registro de mejoras del bot

Cada sesión de mejora (domingo y miércoles) anota aquí qué vio en los datos, qué cambió, con qué evidencia y
qué métrica hay que revisar la vez siguiente.

## 2026-11-27 · Revisión manual (informe de las 23:01)

**Datos**
- Cripto (simulado): saldo 9.259 $ (−7,4 % desde 10.000). 59 operaciones cerradas en los últimos días:
  acierto 37 %, media −7,7 % por operación, −740 $. Salidas: candado de beneficio 19, time stop 12, stop loss ~12
  (todas entre −25 % y −30 %: deslizamiento de 1 a 5 puntos sobre el −25 %), trailing 2.
- Cerebro de cripto: 15.227 ejemplos, 283 tokens. Llegó a promocionar un modelo (v1: +11 %/op, 52 ops de prueba,
  LCB +4,9 %), pero los reentrenamientos recientes no confirman ventaja (mejor aspirante LCB −4 %) → defensivo.
  Venta inteligente estudiada: empeora (−9,9 %/op en test) → desactivada, correcto.
- Acciones: 29 abiertas, 17 cerradas (muchas por el reequilibrio del saldo conjunto). «Retroceso» suspendida por
  la realidad (10 alertas, −3,9 %). Momentum: 5 cerradas, −2,3 %; muestra todavía pequeña.
- Cazador: −133 $ en 68 cerradas (acierto 2,9 %). El estudio ya NO ve ventaja (165 lanzamientos; LCB −9 %):
  se corrigió solo. La regla «con ventaja» de días anteriores era casualidad de una muestra pequeña.
- Copia: +341,78 $ en 126 cerradas (acierto 33 %, media +13,6 %), PERO la mejor operación (19,9x sobre 10 $) aporta
  ~+189 $; sin ella la media es ≈ −1,5 %. Es el patrón «posible suerte». Estudio de copia: solo 9 lanzamientos con
  resultado, sin datos fuera de muestra todavía.
- Traders analizados: 66 bots, 8 pocos datos, 2 pierden. Ninguna «habilidad probable» aún.

**Cambios**
- Fallo: con el cerebro en defensivo pero con un campeón antiguo guardado, la cripto apostaba a tamaño completo.
  Ahora, en defensivo, siempre apuestas de exploración (×0,25).
- Fallo: la revisión por minuto de acciones mostraba la liquidez de la cuenta antigua (−3.089 €) en vez de la
  conjunta; ahora calcula la conjunta y también reequilibra si quedara negativa.
- El informe incluye liquidez, saldo conjunto, riesgo abierto y lo comprometido en acciones, para vigilarlo.
- La sesión programada del miércoles 25 analizó pero no pudo subir (403 de GitHub): la tarea se ejecuta ahora en
  la conversación principal, que sí tiene permiso.

**Qué revisar el domingo**
- Que `stocks.committed_eur` ≤ 70 % del saldo conjunto y que la liquidez conjunta no sea negativa.
- Pérdida media de cripto tras el ×0,25 en defensivo (debería caer en $ aunque el % por operación sea parecido).
- Copia: ¿sigue positiva quitando la mejor operación? Si con >200 cerradas la media sin la mejor sigue ≤ 0, no
  merece más atención.
- Stops de cripto: si el deslizamiento medio sobre −25 % sigue > 3 puntos, estudiar stop más ajustado o salida por
  liquidez/volumen antes del stop (con backtest).
