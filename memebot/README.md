# MemeBot — bot de trading de memecoins en Solana

> **Aviso honesto antes de nada.** La gran mayoría de las memecoins van a cero, y la mayoría de la gente que las
> tradea, con bot o sin él, pierde dinero. Tus rivales son snipers con infraestructura dedicada, insiders que crean
> el token y bots MEV que se ganan la vida explotando a los bots ingenuos. Este proyecto no promete beneficios. Lo que
> hace es aplicar las estrategias con mejor relación riesgo/beneficio que existen para un particular, con una
> **gestión de riesgo estricta** que limita cuánto puedes perder. Usa solo dinero que puedas perder entero.

---

## 1. Análisis de las estrategias que se usan hoy

| Estrategia | Cómo funciona | Quién gana de verdad | ¿Viable para ti? |
|---|---|---|---|
| **Sniping de lanzamientos** (pump.fun, bloque 0) | Comprar en el primer bloque en que el token es comprable | Equipos con nodos propios, bundles de Jito y acuerdos con los creadores. Muchos lanzamientos están *bundleados*: el creador ya compró el supply con decenas de wallets | ❌ Casi imposible. Llegas tarde por milisegundos y compras el techo que te venden los insiders |
| **Graduación de bonding curve** | Comprar cuando un token de pump.fun está a punto de migrar a PumpSwap/Raydium | Bots rápidos que vigilan el % de la curva | ⚠️ Arriesgado. Muchos tokens se dumpean justo al migrar ("sell the graduation") |
| **Copy trading** (seguir "smart wallets") | Replicar las compras de wallets con buen historial | El wallet copiado. Hay investigación (arXiv 2601.08641) sobre bots que **cazan** a los copy-traders: compran, esperan a que los copien y les venden encima | ⚠️ Solo con mucho filtrado. Los wallets "ganadores" públicos (GMGN, Cielo…) suelen ser cebo o rotan |
| **Momentum de segunda ola** ✅ | Esperar a que el token sobreviva 30 min–72 h, tenga liquidez real y presión compradora sostenida, y entrar en la continuación | Quien filtra bien y corta pérdidas rápido | ✅ **La más razonable para un particular.** Es la que implementa este bot |
| **Mean reversion en memecoins grandes** (WIF, BONK, POPCAT…) | Comprar caídas fuertes en tokens líquidos y vender el rebote | Traders con disciplina de stops | ✅ Menos explosiva y más estable. Se configura subiendo `min_liquidity_usd` y la edad mínima |
| **Sentimiento social** (X/Telegram) | Detectar narrativas antes de que se muevan | Quien tiene acceso a datos de X (API cara) y filtra los shills | ⚠️ Útil como señal extra, no como estrategia sola |
| **Market making / arbitraje** | Proveer liquidez o arbitrar entre DEX | Firmas con capital e infraestructura | ❌ No para empezar |

### Por qué "momentum de segunda ola"
1. **No compites en velocidad.** Entras minutos u horas después del lanzamiento, cuando la carrera de milisegundos
   ya terminó. Unos segundos de latencia no te matan.
2. **Los peores fraudes ya se revelan solos.** Los rugs inmediatos y los tokens 100 % bundleados suelen morir en los
   primeros minutos.
3. **Hay liquidez para salir.** Una pool de más de 40 k USD te permite vender sin hundir el precio, si tu posición es
   pequeña.
4. **Las asimetrías son brutales.** Unas pocas operaciones de +100 % o +300 % pagan muchas pérdidas pequeñas de −25 %.
   Todo el sistema está pensado para **cortar rápido lo malo y dejar correr lo bueno**.

### La realidad matemática
Con un stop del −25 % y take-profits en +100 % y +300 %, el bot puede **acertar solo el 30–35 % de las veces y aun así
ganar**, *siempre que* las pérdidas se mantengan en ~−25 %. El enemigo es el **gap**: un rug te lleva de golpe al −90 %.
Por eso pesan tanto los filtros de seguridad y el tamaño pequeño por operación.

---

## 2. Cómo funciona el bot

```
Cada 30 s:
 1. GESTIONAR POSICIONES (primero proteger lo que ya tienes)
    ├─ precio y liquidez actuales (DexScreener)
    └─ salidas, en orden de prioridad:
        liquidez −40 %        → vender todo (posible rug)
        stop −25 %            → vender todo (tras el primer TP, el stop sube a break-even)
        trailing −30 % desde el pico (se activa a partir de +50 %)
        time stop: 90 min sin llegar a +10 % → vender
        take-profit: +100 % vende el 50 % (recuperas la inversión), +300 % vende otro 25 %
 2. BUSCAR ENTRADAS
    ├─ kill-switch: ¿pérdida diaria ≥ 5 %? ¿4 posiciones abiertas? → no se abre nada
    ├─ candidatos: tokens con perfil o boost recientes en DexScreener (Solana)
    ├─ filtros duros: liquidez, edad, volumen, nº de transacciones, ratio compras/ventas,
    │   cambio de 1 h entre +5 % y +150 % (no perseguir velas parabólicas), sin caída en 5 m, FDV máxima
    ├─ puntuación 0–100: rotación volumen/liquidez, aceleración, presión compradora,
    │   tendencia, liquidez/FDV, participación (el boost pagado resta puntos)
    ├─ seguridad: mint authority revocada, freeze authority revocada, top-10 holders ≤ 35 %
    │   (excluyendo vaults de pools), RugCheck sin riesgos "danger", ruta de venta (anti-honeypot)
    └─ tamaño: riesgo fijo del 1 % del capital ÷ stop → con un tope del 5 % del capital y del 1 % de la pool
```

| Archivo | Qué hace |
|---|---|
| `bot/scanner.py` | Descubre tokens y precios (API pública de DexScreener, sin clave) |
| `bot/strategy.py` | Filtros y puntuación de entrada |
| `bot/safety.py` | Comprobaciones anti-rug on-chain y RugCheck |
| `bot/risk.py` | Tamaño de posición, kill-switch y reglas de salida |
| `bot/executor.py` | `PaperExecutor` (simula impacto, fees y coste de red) y `JupiterExecutor` (swaps reales) |
| `bot/portfolio.py` | Estado persistente (`state/portfolio.json`) y registro de operaciones (`state/trades.csv`) |
| `bot/main.py` | Bucle principal y avisos por Telegram |

---

## 3. Cómo montarlo paso a paso

### Paso 1 — Entorno
```bash
cd memebot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
cp .env.example .env
python -m pytest -q          # deben pasar todos los tests
```

### Paso 2 — RPC privado (gratis para empezar)
El RPC público de Solana limita mucho las peticiones. Crea una cuenta en **Helius**, **QuickNode** o **Triton** y pon la
URL en `SOLANA_RPC_URL`. El plan gratuito basta para este bot.

### Paso 3 — Paper trading (mínimo 2–4 semanas)
```bash
python -m bot.main --config config.yaml          # bucle continuo
python -m bot.main --config config.yaml --once -v   # un ciclo con log detallado
```
Revisa `state/trades.csv`. Antes de pasar a real, comprueba:
- **Win rate** y **ratio ganancia media / pérdida media**. Necesitas `win_rate × ganancia_media > (1 − win_rate) × pérdida_media`.
- **Peor operación**: si ves pérdidas mucho peores que −25 %, sube `min_liquidity_usd` y endurece la seguridad.
- **Drawdown máximo**: la peor racha de equity.

El paper trading es **optimista**: en real habrá transacciones fallidas, más slippage y sandwiches. Si en paper no
gana con claridad, en real perderá.

### Paso 4 — Wallet dedicado
1. Crea un wallet **nuevo** en Phantom o Solflare, solo para el bot. Nunca uses tu wallet principal.
2. Deposita solo el capital del bot en **USDC**, más ~0,1 SOL para comisiones de red.
3. Exporta la clave privada (base58) a `WALLET_PRIVATE_KEY` en `.env`. Ese archivo nunca se sube a git (está en
   `.gitignore`).

### Paso 5 — Jupiter API
Crea una clave gratuita en **portal.jup.ag** y ponla en `JUPITER_API_KEY`. El bot usa la Swap API v2
(`GET /order` → firmar → `POST /execute`). Jupiter enruta por el mejor DEX y se encarga de que la transacción entre
en bloque (priority fees), así no necesitas montar tu propio envío por Jito.
> La API de Jupiter cambia a menudo. Antes de usar dinero real, confirma en developers.jup.ag que los nombres de los
> campos de `bot/executor.py` siguen coincidiendo, y haz primero un swap manual pequeño (por ejemplo 5 USD).

### Paso 6 — Real, con poco capital
```yaml
mode: live
risk:
  starting_equity_usd: 200   # empieza pequeño; sube solo tras meses de datos reales
```

### Paso 7 — Ejecución 24/7
Un VPS barato (Hetzner, DigitalOcean…) cerca de los validadores (EE. UU. o Frankfurt) y un servicio de systemd:
```ini
# /etc/systemd/system/memebot.service
[Service]
WorkingDirectory=/opt/memebot
ExecStart=/opt/memebot/.venv/bin/python -m bot.main --config config.yaml
Restart=always
[Install]
WantedBy=multi-user.target
```
Activa Telegram (`TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`) para recibir cada compra y venta en el móvil.

---

## 4. Cómo ajustar y mejorar

- **Más conservador** (memecoins grandes): `min_liquidity_usd: 500000`, `min_age_minutes: 1440`, `stop_loss_pct: 15`,
  `take_profits: [[30, 50], [80, 25]]`.
- **Más agresivo**: bajar la edad y la liquidez mínimas aumenta el número de rugs. Hazlo solo con datos de paper que lo
  justifiquen.
- **Siguientes mejoras útiles**:
  1. Fuente de datos en tiempo real (WebSockets de Helius o gRPC de Yellowstone) en lugar de sondear cada 30 s.
  2. Detección de lanzamientos *bundleados*: wallets que compraron en el mismo bloque que el creador.
  3. Historial del creador: cuántos tokens ha lanzado y cuántos acabaron en rug.
  4. Copy trading **como señal de confirmación**, no como disparador: subir la puntuación si 2–3 wallets de calidad
     ya están dentro.
  5. Script de backtest sobre `trades.csv` y datos históricos (Birdeye y Bitquery tienen OHLCV).

## 5. Seguridad, riesgo y legal

- **Nunca** compartas `.env` ni la clave privada. No instales "bots de Telegram" que te pidan la clave privada: es el
  timo más común del sector.
- El bot solo opera con el presupuesto `starting_equity_usd` y para solo cuando pierde el 5 % en un día.
- Las salidas protegen contra caídas graduales, **no contra un rug instantáneo**. El tamaño pequeño por operación es
  tu verdadera protección.
- **Impuestos (España)**: cada swap cripto-cripto es una ganancia o pérdida patrimonial que tributa en el IRPF (base
  del ahorro). Guarda `trades.csv`, que es tu registro. Consulta a un asesor fiscal, sobre todo por el volumen de
  operaciones que genera un bot.
