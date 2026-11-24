# Empezar con 50 € — paso a paso

> Riesgo alto, como pediste: puedes perder buena parte de esos 50 €. El bot tiene un **freno total**: si el saldo
> baja de 30 $, deja de abrir operaciones. Las pérdidas por encima de eso solo pueden venir de un rug en las
> posiciones que ya tenga abiertas.

## 1. Cómo funciona, en 30 segundos
1. **Recoge datos**: cada 15 minutos descarga velas de cientos de memecoins de Solana y registra lanzamientos nuevos.
2. **Aprende** (el cerebro, cada 6 h): estudia esas velas y tus propias operaciones y entrena un modelo. Hace
   competir estrategias con datos que no han visto y se queda con la que demuestra ventaja. Si la realidad le sale
   peor de lo esperado, se corrige.
3. **Opera** (cada 20–60 s): busca tokens, aplica la estrategia del cerebro, descarta los peligrosos (tokens con los
   que pueden imprimir más supply o congelar tu saldo, muy concentrados en pocos holders o que no se pueden vender),
   compra con Jupiter y vende con stop loss, take-profits, trailing stop y time stop.
4. **Se protege**:
   - posiciones de unos 8–10 $ y un máximo de 3 a la vez;
   - stop diario del −15 %;
   - freno total en 30 $;
   - modo defensivo: si el cerebro no encuentra ventaja, no opera en real.
5. **Te lo enseña**: en el panel web (saldo, posiciones, operaciones y cerebro) y por Telegram.

## 2. Preparar el dinero (unos 30 min)
1. **Wallet**: instala **Phantom** (phantom.com; solo desde la web o la tienda oficial) y crea una wallet **nueva**,
   solo para el bot. Apunta en papel las 12 palabras y no las guardes nunca en el móvil ni en la nube.
2. **Compra** en un exchange regulado que opere en España (por ejemplo **Bit2Me**, **Kraken** o **Coinbase**).
   Verifica tu identidad, deposita los 50 € y compra:
   - **~45 € en USDC**: es el capital del bot;
   - **~5 € en SOL**: paga las comisiones de red, de céntimos cada una. Deja siempre al menos 0,05 SOL.
3. **Retira** el USDC y el SOL a la dirección de tu wallet Phantom. Elige la **red Solana**: si eliges otra red,
   pierdes el dinero. Antes de enviarlo todo, prueba con una cantidad pequeña. Fíjate en la comisión de retirada del
   exchange.
4. **Clave privada**: en Phantom → Ajustes → Gestionar cuentas → tu cuenta → *Mostrar clave privada*. Cópiala a
   `WALLET_PRIVATE_KEY` en el archivo `.env`. Ese archivo nunca se comparte ni se sube a ningún sitio.

## 3. Claves gratuitas
- **RPC de Solana**: crea una cuenta gratuita en **Helius** (helius.dev) y pon la URL en `SOLANA_RPC_URL`. El RPC
  público bloquea peticiones, y entonces el bot no puede verificar los tokens y no compra.
- **Jupiter**: crea una clave gratuita en **portal.jup.ag** y ponla en `JUPITER_API_KEY`.
- **Telegram** (opcional): habla con **@BotFather** para crear tu bot y pon el token en `TELEGRAM_BOT_TOKEN`. Para
  saber tu chat id, escribe a **@userinfobot** y ponlo en `TELEGRAM_CHAT_ID`.

## 4. Arrancar
```bash
cd memebot
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env              # rellena las claves
cp config.50eur.yaml config.yaml  # perfil de 50 €, empieza en mode: paper

# En un servidor 24/7, un solo comando:  sudo bash deploy/install.sh
# O a mano en tu ordenador (4 terminales):
python -m research.collect --loop 15                            # datos
python -m bot.brain --data data,data_launches --state state --loop 6   # cerebro
python -m bot.main --config config.yaml                         # bot
python -m bot.dashboard --state state                           # panel: http://127.0.0.1:8050
```

## 5. Paso a dinero real
1. Deja el bot en **paper** hasta que el panel del cerebro muestre una estrategia propia (estado distinto de
   "Aprendiendo") y lleve al menos una semana operando en simulado.
2. En `config.yaml`, cambia `mode: paper` por `mode: live`.
3. Ejecuta `python -m bot.preflight --config config.yaml`. Comprueba la wallet, el saldo de USDC y SOL, Jupiter y tu
   historial de paper. Todo debe salir ✅.
4. Reinicia el bot. En el panel verás **● DINERO REAL**. A partir de ahí es autónomo: el cerebro aplica sus mejoras
   solo.
5. **Cerrar todo** en el panel vende todas las posiciones en menos de 20 segundos, si algún día lo necesitas.

## 6. Encendido 24 horas al día
Con 50 €, un VPS de pago (≈5 €/mes) se come el 10 % del capital cada mes. Usa uno **gratuito**:
1. Crea una cuenta en **Oracle Cloud Free Tier** (cloud.oracle.com; pide tarjeta para verificar, pero la capa
   *Always Free* no cobra).
2. Crea una instancia: **Ubuntu 22.04 o 24.04**, forma **Ampere A1** (gratis, hasta 4 CPU y 24 GB). Guarda la
   clave SSH que te da.
3. Conéctate: `ssh -i tu_clave ubuntu@IP`. Clona el repo, entra en `memebot`, rellena `.env` y ejecuta
   `sudo bash deploy/install.sh`.
4. Desde ese momento el bot, el panel, el cerebro y los recolectores funcionan **24/7** y se reinician solos si
   algo falla o el servidor se reinicia (`restart: always`).
5. Para ver el panel desde casa: `ssh -L 8050:127.0.0.1:8050 -i tu_clave ubuntu@IP` y abre la URL con token que
   muestra el instalador. Telegram te avisa de cada compra y venta en el móvil.

## 7. Impuestos (España)
Cada venta es una ganancia o pérdida patrimonial en el IRPF (base del ahorro). El archivo `state/trades.csv` es tu
registro de operaciones. Si los saldos que tienes fuera de España superan los 50.000 €, aplica el modelo 721.
Consulta a un asesor si el volumen crece.
